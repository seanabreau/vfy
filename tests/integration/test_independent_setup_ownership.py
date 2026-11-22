"""Interrupted protocols preserve the real setup owner until ordinary teardown."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_PLUGIN = """import json,pytest
from pathlib import Path
events=[]
reports=[]
observed={}
def stop():
    if MODE=='ordinary_log':raise RuntimeError('actual ordinary callback')
    if CONTROL=='exit':pytest.exit('actual control',returncode=42)
    raise KeyboardInterrupt('actual control')
def target(item):return item.name=='test_target'
def broad_resource(request,scope):
    events.append('setup '+scope)
    def cleanup():
        events.append('teardown '+scope)
        request.addfinalizer(lambda:events.append('added '+scope))
    request.addfinalizer(cleanup)
@pytest.fixture(scope='session')
def session_resource(request):broad_resource(request,'session')
@pytest.fixture(scope='module')
def module_resource(request,session_resource):broad_resource(request,'module')
@pytest.fixture(scope='class')
def class_resource(request,module_resource):broad_resource(request,'class')
@pytest.fixture(scope='module' if MODE=='broad_last' else 'function')
def resource(request):
    events.append('fixture setup')
    def cleanup():
        events.append('finalizer')
        if MODE.startswith('cross_scope'):
            request.addfinalizer(lambda:events.append('added function'))
        if MODE=='fixture_teardown':stop()
    request.addfinalizer(cleanup)
    if MODE.startswith('cross_scope'):request.getfixturevalue('class_resource')
    if MODE=='fixture_setup':stop()
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    if target(item) and MODE=='setup_before':stop()
@pytest.hookimpl(trylast=True,specname='pytest_runtest_setup')
def pytest_after_setup(item):
    if target(item) and MODE=='setup_after':stop()
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item,nextitem):
    if target(item) and MODE=='teardown_before':stop()
@pytest.hookimpl(trylast=True,specname='pytest_runtest_teardown')
def pytest_after_teardown(item,nextitem):
    if target(item) and MODE=='teardown_after':stop()
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report):
    reports.append(report)
@pytest.hookimpl(trylast=True,specname='pytest_runtest_logreport')
def pytest_after_report(report):
    if not report.nodeid.endswith('::test_target'):return
    if MODE=='ordinary_log' and report.when=='call':stop()
    if MODE=='report_'+report.when:stop()
def pytest_runtest_logfinish(nodeid,location):
    if nodeid.endswith('::test_target') and MODE=='logfinish':stop()
@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item,nextitem):
    try:return (yield)
    except BaseException as error:
        if not target(item):raise
        events.append(['caught',type(error).__name__])
        if MODE=='unswallowed':raise
        return True
@pytest.hookimpl(tryfirst=True,specname='pytest_runtest_protocol')
def pytest_bypass_last(item,nextitem):
    if target(item) and MODE=='broad_last':
        events.append('protocol bypassed')
        return True
def snapshot(report):
    failed_owner=(report.when=='setup' and report.failed and
                  'previous item was not torn down properly' in report.longreprtext)
    return [report.nodeid,report.when,report.outcome,failed_owner]
def pytest_sessionfinish(session,exitstatus):
    events.append('finish')
    observed.update(reports=[snapshot(report) for report in reports],
        testsfailed=session.testsfailed,exitstatus=int(exitstatus))
class LateFinish:
    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self,session,exitstatus):events.append('late finish')
def pytest_configure(config):config.pluginmanager.register(LateFinish(),'late observer')
def pytest_unconfigure():
    observed['events']=events
    Path('observed.json').write_text(json.dumps(observed))
"""

_TEST = """from conftest import MODE,events,stop
def test_prefix():pass
def test_target(resource):
    events.append('body')
    if MODE in {'body','last','unswallowed'}:stop()
def test_later():events.append('later body')
"""

_CLASS_TEST = """from conftest import MODE,events,stop
def test_prefix():pass
class TestOwners:
    def test_target(self,resource):
        events.append('body')
        if MODE=='cross_scope':stop()
    def test_later(self):events.append('later body')
"""

_ACTIVE_OWNER = {
    "setup_after",
    "fixture_setup",
    "body",
    "teardown_before",
    "report_setup",
    "report_call",
    "ordinary_log",
    "cross_scope",
}
_MODES = [
    "setup_before",
    "setup_after",
    "fixture_setup",
    "body",
    "teardown_before",
    "fixture_teardown",
    "teardown_after",
    "report_setup",
    "report_call",
    "report_teardown",
    "logfinish",
    "last",
    "unswallowed",
]
_CASES = [(mode, control) for mode in _MODES for control in ["keyboard", "exit"]] + [
    ("ordinary_log", "keyboard"),
    ("broad_last", "keyboard"),
    ("cross_scope", "keyboard"),
    ("cross_scope", "exit"),
    ("cross_scope_clean", "keyboard"),
]


# Actual hook observers retain heterogeneous phase records and lifecycle events.
def _run(project: VfyProject, engine: str, mode: str, control: str) -> dict[str, Any]:
    expected = None
    if engine == "native":
        expected = _run(project, "stock", mode, control)
        project.write("stock-observed.json", json.dumps(expected))
        (project.root / "observed.json").unlink()
    result = (
        project.run_python("-B", "-m", "pytest", "-q", "-p", "no:cacheprovider")
        if engine == "stock"
        else project.run_vfy(
            "test", "--engine=native", "--no-cov", "-q", "--native-result-json=result.json"
        )
    )
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    project.write(f"{engine}.status", str(result.returncode))
    assert (project.root / "observed.json").exists(), result.stdout + result.stderr
    observed = json.loads((project.root / "observed.json").read_text())
    status = (
        1
        if mode in _ACTIVE_OWNER
        else (42 if control == "exit" else 2)
        if mode == "unswallowed"
        else 0
    )
    assert result.returncode == status, result.stdout + result.stderr
    assert observed["exitstatus"] == status
    assert observed["testsfailed"] == (1 if mode in _ACTIVE_OWNER else 0)
    error_type = (
        "RuntimeError"
        if mode == "ordinary_log"
        else "Exit"
        if control == "exit"
        else "KeyboardInterrupt"
    )
    assert [event for event in observed["events"] if isinstance(event, list)] == (
        [] if mode in {"broad_last", "cross_scope_clean"} else [["caught", error_type]]
    )
    later = [report[1:] for report in observed["reports"] if report[0].endswith("::test_later")]
    if mode in _ACTIVE_OWNER:
        assert later == [["setup", "failed", True], ["teardown", "passed", False]]
        assert "later body" not in observed["events"]
    elif mode in {"last", "unswallowed", "broad_last"}:
        assert later == []
        assert observed["events"].index("finish") < observed["events"].index("finalizer")
        assert observed["events"].index("finalizer") < observed["events"].index("late finish")
    else:
        assert later == [
            ["setup", "passed", False],
            ["call", "passed", False],
            ["teardown", "passed", False],
        ]
        assert "later body" in observed["events"]
    finalizer_count = (
        0 if mode == "setup_before" or (mode == "fixture_setup" and control == "keyboard") else 1
    )
    assert observed["events"].count("finalizer") == finalizer_count
    if mode == "broad_last":
        assert "protocol bypassed" in observed["events"]
        assert all(report[0].endswith("::test_prefix") for report in observed["reports"])
    if mode in _ACTIVE_OWNER and finalizer_count:
        assert observed["events"].index(["caught", error_type]) < observed["events"].index(
            "finalizer"
        )
        assert observed["events"].index("finalizer") < observed["events"].index("finish")
    if mode.startswith("cross_scope"):
        middle = (
            [["caught", error_type], "finalizer", "added function"]
            if mode == "cross_scope"
            else ["finalizer", "added function", "later body"]
        )
        assert observed["events"] == [
            "fixture setup",
            "setup session",
            "setup module",
            "setup class",
            "body",
            *middle,
            "teardown class",
            "added class",
            "teardown module",
            "added module",
            "teardown session",
            "added session",
            "finish",
            "late finish",
        ]
    if engine == "native":
        assert observed == expected
    return observed


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize(("mode", "control"), _CASES)
def test_interrupted_setup_owners_follow_actual_teardown_boundaries(
    tmp_path: Path, engine: str, mode: str, control: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", f"MODE={mode!r}\nCONTROL={control!r}\n" + _PLUGIN)
    tests = _TEST.split("def test_later")[0] if mode in {"last", "broad_last"} else _TEST
    if mode == "broad_last":
        tests = tests.replace("def test_prefix():pass", "def test_prefix(resource):pass")
    if mode.startswith("cross_scope"):
        tests = _CLASS_TEST
    project.write("test_cases.py", tests)
    _run(project, engine, mode, control)
