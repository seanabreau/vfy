"""Builtin subtests retain their own reports, captures, and failure budget."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import native_engines

_OBSERVER = """import json
from pathlib import Path
import pytest

reports = []
calls = []
events = []

def pytest_runtest_makereport(item, call):
    calls.append([item.name, call.when,
                  None if call.excinfo is None else type(call.excinfo.value).__name__])

def pytest_runtest_logreport(report):
    reports.append(report)

def snapshot(report):
    context = getattr(report, 'context', None)
    return {
        'nodeid': report.nodeid, 'when': report.when, 'outcome': report.outcome,
        'subtest_type': isinstance(report, pytest.SubtestReport),
        'context': None if context is None else {
            'msg': context.msg, 'kwargs': list(context.kwargs.items()),
        },
        'wasxfail': getattr(report, 'wasxfail', None),
        'sections': list(report.sections),
        'stdout': report.capstdout, 'stderr': report.capstderr, 'log': report.caplog,
        'longrepr': str(report.longrepr),
    }

def pytest_sessionfinish(session, exitstatus):
    terminal = session.config.pluginmanager.get_plugin('terminalreporter')
    Path('subtests-observations.json').write_text(json.dumps({
        'reports': [snapshot(report) for report in reports],
        'calls': calls, 'events': events, 'testsfailed': session.testsfailed,
        'collected': session.testscollected, 'exitstatus': int(exitstatus),
        'stats': {key: len(value) for key, value in terminal.stats.items()},
    }, indent=2))

def pytest_unconfigure(config):
    Path('subtests-cleanup.json').write_text(json.dumps(events))
"""


def _project(tmp_path: Path, source: str, *, fixtures: str = "") -> VfyProject:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", _OBSERVER + fixtures)
    project.write("test_subtests.py", source)
    return project


def _run(project: VfyProject, engine: str, *arguments: str) -> CommandResult:
    result = project.run_engine(engine, "-q", *arguments)
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    project.write(f"{engine}.status", str(result.returncode))
    assert (project.root / "subtests-observations.json").is_file(), result.stdout + result.stderr
    data = json.loads((project.root / "subtests-observations.json").read_text())
    assert all(
        report["subtest_type"] is (report["context"] is not None) for report in data["reports"]
    )
    return result


@native_engines
def test_subtests_fixture_is_function_scoped_and_uses_real_provider(
    tmp_path: Path, engine: str
) -> None:
    project = _project(
        tmp_path,
        """import pytest
from conftest import events

def test_first(subtests, request):
    assert isinstance(subtests, pytest.Subtests)
    assert request.getfixturevalue('subtests') is subtests
    assert subtests._request.scope == 'function'
    assert subtests._request.node is request.node
    with subtests.test() as entered:
        assert entered is None
    events.append('first')

def test_second(subtests):
    events.append('second')
""",
        fixtures="""
owners = []
@pytest.fixture
def subtests(subtests, request):
    definition = request._fixture_defs['subtests']
    assert definition.scope == 'function'
    assert definition.func.__name__ == 'subtests'
    assert request.config.pluginmanager.hasplugin('subtests')
    provider = request.config.pluginmanager.get_plugin('subtests')
    assert provider.subtests._get_wrapped_function() is definition.func
    assert all(previous is not subtests for previous in owners)
    owners.append(subtests)
    events.append('setup:' + request.node.name)
    yield subtests
    events.append('teardown:' + request.node.name)
""",
    )
    result = _run(project, engine)
    result.expect(0)
    data = json.loads((tmp_path / "subtests-observations.json").read_text())
    assert data["events"] == [
        "setup:test_first",
        "first",
        "teardown:test_first",
        "setup:test_second",
        "second",
        "teardown:test_second",
    ]
    assert data["collected"] == 2
    assert data["stats"]["subtests passed"] == 1


@native_engines
def test_subtest_outcomes_continue_and_parent_failure_is_reported_separately(
    tmp_path: Path, engine: str
) -> None:
    project = _project(
        tmp_path,
        """import pytest
from conftest import events

def test_outcomes(subtests):
    for label in ('pass', 'fail', 'skip', 'xfail', 'error'):
        try:
            with subtests.test(label, value=label):
                events.append('enter:' + label)
                if label == 'fail': assert False, 'subtest assertion'
                if label == 'skip': pytest.skip('subtest skip')
                if label == 'xfail': pytest.xfail('subtest xfail')
                if label == 'error': raise ValueError('subtest error')
        finally:
            events.append('cleanup:' + label)
    events.append('after')
""",
    )
    result = _run(project, engine)
    result.expect(1)
    data = json.loads((tmp_path / "subtests-observations.json").read_text())
    reports = data["reports"]
    assert [(report["when"], report["outcome"]) for report in reports] == [
        ("setup", "passed"),
        ("call", "passed"),
        ("call", "failed"),
        ("call", "skipped"),
        ("call", "skipped"),
        ("call", "failed"),
        ("call", "failed"),
        ("teardown", "passed"),
    ]
    assert [report["context"]["msg"] for report in reports if report["context"]] == [
        "pass",
        "fail",
        "skip",
        "xfail",
        "error",
    ]
    assert reports[4]["wasxfail"] == "subtest xfail"
    assert reports[-2]["longrepr"] == "contains 2 failed subtests"
    assert data["events"] == [
        "enter:pass",
        "cleanup:pass",
        "enter:fail",
        "cleanup:fail",
        "enter:skip",
        "cleanup:skip",
        "enter:xfail",
        "cleanup:xfail",
        "enter:error",
        "cleanup:error",
        "after",
    ]
    assert data["testsfailed"] == 2
    assert data["stats"]["failed"] == 3
    assert data["exitstatus"] == 1


@native_engines
def test_teardown_subtest_does_not_reclassify_completed_parent_call(
    tmp_path: Path, engine: str
) -> None:
    project = _project(
        tmp_path,
        "def test_body(resource): pass\n",
        fixtures="""
@pytest.fixture
def resource(subtests):
    yield
    with subtests.test('teardown'):
        assert False, 'teardown subtest'
""",
    )
    result = _run(project, engine)
    result.expect(1)
    data = json.loads((tmp_path / "subtests-observations.json").read_text())
    assert [
        (report["when"], report["outcome"], report["context"] is not None)
        for report in data["reports"]
    ] == [
        ("setup", "passed", False),
        ("call", "passed", False),
        ("call", "failed", True),
        ("teardown", "passed", False),
    ]
    assert data["stats"]["passed"] == data["stats"]["failed"] == 1
    assert data["testsfailed"] == 1


@native_engines
@pytest.mark.parametrize("capture", ["fd", "sys", "capsys", "capfd", "tee-sys", "no"])
def test_subtest_context_repr_capture_and_logs_are_local(
    tmp_path: Path, engine: str, capture: str
) -> None:
    fixture = ", " + capture if capture in {"capsys", "capfd"} else ""
    project = _project(
        tmp_path,
        """import logging, sys

class Value:
    def __repr__(self): return '<value>'

def test_capture(subtestsFIXTURE):
    print('parent before')
    value = ['before']
    with subtests.test('context', second=Value(), first='text', third=value):
        value.append('inside')
        print('inside stdout')
        print('inside stderr', file=sys.stderr)
        logging.warning('inside log')
    print('parent after')
""".replace("FIXTURE", fixture),
    )
    result = _run(
        project, engine, "--capture=" + ("fd" if capture in {"capsys", "capfd"} else capture)
    )
    result.expect(0)
    data = json.loads((tmp_path / "subtests-observations.json").read_text())
    subreport = data["reports"][1]
    parent = data["reports"][2]
    assert subreport["context"] == {
        "msg": "context",
        "kwargs": [
            ["second", "<value>"],
            ["first", "'text'"],
            ["third", "['before', 'inside']"],
        ],
    }
    assert subreport["log"].endswith("inside log\n")
    assert parent["log"].endswith("inside log")
    assert not parent["log"].endswith("\n")
    if capture == "no":
        assert subreport["stdout"] == subreport["stderr"] == ""
        assert parent["stdout"] == parent["stderr"] == ""
        assert result.stdout.startswith("parent before\ninside stdout\nuparent after\n.")
        assert "inside stderr\n" in result.stderr
    elif capture in {"capsys", "capfd", "tee-sys"}:
        assert subreport["stdout"] == subreport["stderr"] == ""
        assert parent["stdout"] == "parent before\ninside stdout\nparent after\n"
        assert parent["stderr"] == "inside stderr\n"
    else:
        assert subreport["stdout"] == "inside stdout\n"
        assert subreport["stderr"] == "inside stderr\n"
        assert parent["stdout"] == "parent before\nparent after\n"
        assert parent["stderr"] == ""


@native_engines
@pytest.mark.parametrize("capture", ["fd", "sys"])
def test_nested_subtest_capture_resumes_global_output_after_inner_report(
    tmp_path: Path, engine: str, capture: str
) -> None:
    project = _project(
        tmp_path,
        """import logging
def test_nested_capture(subtests):
    print('parent before')
    with subtests.test('outer'):
        print('outer before')
        with subtests.test('inner'):
            print('inner')
            logging.warning('inner log')
        print('outer after')
    print('parent after')
""",
    )
    result = _run(project, engine, "--capture=" + capture)
    result.expect(0)
    data = json.loads((tmp_path / "subtests-observations.json").read_text())
    inner, outer, parent = data["reports"][1:4]
    assert [inner["stdout"], outer["stdout"], parent["stdout"]] == [
        "inner\n",
        "outer before\n",
        "parent before\nouter after\nparent after\n",
    ]
    assert [inner["context"]["msg"], outer["context"]["msg"]] == ["inner", "outer"]
    assert all(report["log"].endswith("inner log\n") for report in (inner, outer))
    assert parent["log"].endswith("inner log")
    assert not parent["log"].endswith("\n")


@native_engines
def test_nested_subtests_report_inner_before_outer_and_continue(
    tmp_path: Path, engine: str
) -> None:
    project = _project(
        tmp_path,
        """from conftest import events
def test_nested(subtests):
    with subtests.test('outer', outer=1):
        with subtests.test('inner', inner=2):
            assert False, 'inner failure'
        events.append('after inner')
    events.append('after outer')
""",
    )
    result = _run(project, engine)
    result.expect(1)
    data = json.loads((tmp_path / "subtests-observations.json").read_text())
    assert [
        (report["context"]["msg"], report["outcome"])
        for report in data["reports"]
        if report["context"]
    ] == [
        ("inner", "failed"),
        ("outer", "passed"),
    ]
    assert data["events"] == ["after inner", "after outer"]
    assert data["testsfailed"] == 1


@native_engines
def test_exitfirst_propagates_failure_after_subreport_and_runs_cleanup(
    tmp_path: Path, engine: str
) -> None:
    project = _project(
        tmp_path,
        """from conftest import events
def test_abort(subtests, resource):
    try:
        with subtests.test('abort'):
            assert False, 'stop failure'
        events.append('after')
    finally:
        events.append('finally')
def test_unreached():
    raise AssertionError('must not run')
""",
        fixtures="""
@pytest.fixture
def resource():
    yield
    events.append('fixture cleanup')
""",
    )
    result = _run(project, engine, "-x")
    result.expect(1)
    data = json.loads((tmp_path / "subtests-observations.json").read_text())
    assert data["events"] == ["finally", "fixture cleanup"]
    assert [(report["when"], report["outcome"]) for report in data["reports"]] == [
        ("setup", "passed"),
        ("call", "failed"),
        ("call", "failed"),
        ("teardown", "passed"),
    ]
    assert data["testsfailed"] == 2
    assert data["collected"] == 2


@native_engines
def test_keyboard_interrupt_is_reported_then_propagates_with_cleanup(
    tmp_path: Path, engine: str
) -> None:
    project = _project(
        tmp_path,
        """from conftest import events
def test_interrupt(subtests, resource):
    try:
        with subtests.test('interrupt'):
            raise KeyboardInterrupt('subtest interrupt')
        events.append('after')
    finally:
        events.append('finally')
""",
        fixtures="""
@pytest.fixture
def resource():
    yield
    events.append('fixture cleanup')
""",
    )
    result = _run(project, engine)
    result.expect(2)
    data = json.loads((tmp_path / "subtests-observations.json").read_text())
    assert data["events"] == ["finally"]
    assert json.loads((tmp_path / "subtests-cleanup.json").read_text()) == [
        "finally",
        "fixture cleanup",
    ]
    assert [(report["when"], report["outcome"]) for report in data["reports"]] == [
        ("setup", "passed"),
        ("call", "failed"),
    ]
    assert data["testsfailed"] == 1
    assert data["exitstatus"] == 2


@native_engines
@pytest.mark.parametrize("parent", ["skip", "xfail", "fail"])
def test_failed_subtest_preserves_nonpassing_parent_outcome(
    tmp_path: Path, engine: str, parent: str
) -> None:
    project = _project(
        tmp_path,
        """import pytest
def test_parent(subtests):
    with subtests.test('child'):
        assert False, 'child failure'
    pytest.PARENT('parent outcome')
""".replace("PARENT", parent),
    )
    result = _run(project, engine)
    result.expect(1)
    data = json.loads((tmp_path / "subtests-observations.json").read_text())
    subreport, parent_report = data["reports"][1:3]
    assert subreport["outcome"] == "failed"
    assert subreport["context"]["msg"] == "child"
    assert parent_report["context"] is None
    assert parent_report["outcome"] == ("failed" if parent == "fail" else "skipped")
    assert parent_report["wasxfail"] == ("parent outcome" if parent == "xfail" else None)
    assert data["testsfailed"] == (2 if parent == "fail" else 1)


@native_engines
def test_failed_subtest_reconstructs_required_structured_traceback_graph(
    tmp_path: Path, engine: str
) -> None:
    project = _project(
        tmp_path,
        """from conftest import events
def test_graph(subtests):
    with subtests.test('graph'):
        assert False, 'required assertion failure'
    events.append('continued after child')
""",
        fixtures="""
bases = []
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    bases.append(report)
    return report
@pytest.hookimpl(trylast=True)
def pytest_runtest_logreport(report):
    if isinstance(report, pytest.SubtestReport):
        original = bases[-1].longrepr
        graph = report.longrepr
        assert report.failed
        assert type(graph).__name__ == 'ExceptionChainRepr'
        assert type(graph.reprtraceback).__name__ == 'ReprTraceback'
        assert type(graph.reprcrash).__name__ == 'ReprFileLocation'
        assert graph is not original
        assert graph.reprtraceback is not original.reprtraceback
        assert graph.reprcrash is not original.reprcrash
        assert graph.reprtraceback.reprentries
        assert graph.reprcrash.path.endswith('test_subtests.py')
        assert graph.reprcrash.lineno == 4
        assert 'required assertion failure' in graph.reprcrash.message
        assert len(graph.chain) == 1
        assert graph.chain[0][0] is graph.reprtraceback
        assert graph.chain[0][1] is graph.reprcrash
        events.append('distinct structured graph published')
""",
    )
    result = _run(project, engine)
    result.expect(1)
    data = json.loads((tmp_path / "subtests-observations.json").read_text())
    assert data["events"] == [
        "distinct structured graph published",
        "continued after child",
    ]
    assert data["testsfailed"] == 1
