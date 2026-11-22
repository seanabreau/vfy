"""Dynamic xfail decisions follow the real setup, call, and report lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CASES = [
    "condition_timing",
    "fixture_mark",
    "call_mark",
    "call_finally_mark",
    "run_false",
    "strict_xpass",
    "raises_type_match",
    "raises_type_mismatch",
    "raises_tuple_match",
    "raises_matcher_match",
    "raises_matcher_mismatch",
]
_TRACE = """import json
from pathlib import Path
def note(event, **values):
    with Path('events.jsonl').open('a') as output:
        output.write(json.dumps({'event': event, **values}) + '\\n')
"""


def _project(path: Path, case: str) -> VfyProject:
    project = VfyProject(path)
    project.write("trace_events.py", _TRACE)
    project.write(
        "conftest.py",
        f"""import pytest
from trace_events import note

@pytest.fixture
def resource(request):
    note('fixture')
    if {case!r} == 'fixture_mark':
        request.applymarker(pytest.mark.xfail(reason='fixture marker', raises=ValueError))

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    note('setup enter')
    try:
        return (yield)
    finally:
        note('setup exit')

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    note('call enter')
    try:
        return (yield)
    finally:
        note('call exit')

@pytest.hookimpl(wrapper=True, trylast=True, specname='pytest_runtest_call')
def pytest_call_inner(item):
    note('inner call enter')
    try:
        return (yield)
    finally:
        if {case!r} == 'call_finally_mark':
            item.add_marker(pytest.mark.xfail(reason='inner finally marker', raises=ValueError))
            note('finally marker')
        note('inner call exit')

def record_report(priority, report, call):
    note('report ' + priority, when=report.when, outcome=report.outcome,
         wasxfail=getattr(report, 'wasxfail', None),
         error=None if call.excinfo is None else call.excinfo.type.__name__)

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    record_report('first', report, call)
    return report

@pytest.hookimpl(wrapper=True, specname='pytest_runtest_makereport')
def pytest_report_normal(item, call):
    report = yield
    record_report('normal', report, call)
    return report

@pytest.hookimpl(wrapper=True, trylast=True, specname='pytest_runtest_makereport')
def pytest_report_last(item, call):
    report = yield
    record_report('last', report, call)
    return report

def pytest_runtest_logreport(report):
    note('logreport', when=report.when, outcome=report.outcome,
         wasxfail=getattr(report, 'wasxfail', None))
""",
    )
    declarations = ""
    mark = ""
    body = "    raise ValueError('needle')\n"
    arguments = "resource, request"
    if case == "condition_timing":
        declarations = """class Condition:
    def __bool__(self):
        note('condition')
        return Path('active').exists()
"""
        mark = "@pytest.mark.xfail(Condition(), reason='dynamic condition')\n"
        body = "    Path('active').touch()\n" + body
    elif case == "call_mark":
        body = (
            "    request.applymarker(pytest.mark.xfail(reason='call marker', raises=ValueError))\n"
            + body
        )
    elif case == "run_false":
        mark = "@pytest.mark.xfail(run=False, reason='do not run')\n"
        body = "    raise AssertionError('unreachable body')\n"
    elif case == "strict_xpass":
        mark = "@pytest.mark.xfail(strict=True, reason='strict pass')\n"
        body = "    pass\n"
    elif case.startswith("raises_"):
        expected = "ValueError"
        message = "needle"
        exception = "ValueError"
        if case == "raises_type_mismatch":
            exception = "TypeError"
        elif case == "raises_tuple_match":
            expected = "(ValueError, KeyError)"
            exception = "KeyError"
        elif case.startswith("raises_matcher"):
            expected = "pytest.RaisesExc(ValueError, match='needle')"
            if case.endswith("mismatch"):
                message = "other"
        mark = f"@pytest.mark.xfail(reason='matching rule', raises={expected})\n"
        body = f"    raise {exception}({message!r})\n"
    project.write(
        "test_case.py",
        "import pytest\nfrom pathlib import Path\nfrom trace_events import note\n"
        + declarations
        + mark
        + f"def test_case({arguments}):\n    note('body')\n"
        + body,
    )
    return project


def _events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("case", _CASES)
def test_xfail_matches_stock_phase_and_wrapper_observations(tmp_path: Path, case: str) -> None:
    project = _project(tmp_path, case)
    oracle = project.run_python("-m", "pytest", "-q")
    assert oracle.returncode in (0, 1), oracle.stdout + oracle.stderr
    expected = _events(tmp_path)
    assert any(event["event"] == "report first" for event in expected)
    (tmp_path / "events.jsonl").unlink()
    (tmp_path / "active").unlink(missing_ok=True)
    candidate = project.run_python("-m", "vfy.independent.entry")
    assert candidate.returncode == oracle.returncode, candidate.stdout + candidate.stderr
    assert _events(tmp_path) == expected


def test_xfail_reason_release_allows_reentrant_setup(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, weakref
from pathlib import Path
import pytest

events = []
active_item = None
provider = None

class Reason:
    def __str__(self):
        return 'expected failure'
    def __del__(self):
        events.append('released')
        provider.pytest_runtest_setup(active_item)
        events.append('reentered')

reason = Reason()
reason_ref = weakref.ref(reason)

def pytest_collection_modifyitems(items):
    items[0].add_marker(pytest.mark.xfail(reason=reason))

@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    global active_item, provider
    yield
    active_item = item
    provider = item.config.pluginmanager.getplugin('skipping')
    for marker in item.iter_markers('xfail'):
        marker.kwargs.clear()
    item.own_markers[:] = [
        marker for marker in item.own_markers if marker.name != 'xfail'
    ]
    del globals()['reason']
    assert reason_ref() is not None
    provider.pytest_runtest_setup(item)
    assert reason_ref() is None
    assert events == ['released', 'reentered']
    Path('reason-events.json').write_text(json.dumps(events))
""",
    )
    project.write("test_reason.py", "def test_body():\n    pass\n")
    stock = project.run_python("-m", "pytest", "-q")
    stock.expect(0)
    expected = json.loads((tmp_path / "reason-events.json").read_text())
    (tmp_path / "reason-events.json").unlink()
    candidate = project.run_python("-m", "vfy.independent.entry", "-q")
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert json.loads((tmp_path / "reason-events.json").read_text()) == expected
