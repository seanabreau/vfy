"""Interrupted native items retain real reports without becoming setup-only cases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PLUGIN = """import json,pytest
from pathlib import Path
reports=[]
made=[]
events=[]
observed={}
def stop():
    if CONTROL=='exit':pytest.exit('actual control',returncode=42)
    raise KeyboardInterrupt('actual control')
@pytest.fixture
def resource():
    events.append('setup')
    if MODE=='setup':stop()
    if MODE=='log_setup_skipped':pytest.skip('actual setup skip')
    if MODE=='log_setup_failed':assert False,'actual setup failure'
    yield
    events.append('finalizer')
    if MODE=='teardown':stop()
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item,call):
    report=yield
    made.append(report)
    return report
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report):reports.append(report)
@pytest.hookimpl(trylast=True,specname='pytest_runtest_logreport')
def pytest_log_failure(report):
    if not report.nodeid.endswith('::test_target'):return
    if MODE.startswith('log_') and report.when==MODE.split('_')[1]:
        if MODE.endswith('ordinary'):raise RuntimeError('actual ordinary log failure')
        stop()
@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item,nextitem):
    try:result=yield
    except BaseException as error:
        if item.name=='test_target':
            events.append(['protocol caught',type(error).__name__])
            if MODE=='protocol_replace':raise RuntimeError('actual replacement')
            if MODE=='protocol_swallow':return True
        raise
    if item.name=='test_target' and MODE=='protocol_complete_error':
        raise RuntimeError('actual completed wrapper error')
    return result

def snapshot(report):
    context=getattr(report,'context',None)
    return [report.nodeid,report.when,report.outcome,
            None if context is None else context.msg]
def pytest_sessionfinish(session,exitstatus):
    observed.update(reports=[snapshot(report) for report in reports],
        testsfailed=session.testsfailed,exitstatus=int(exitstatus))
def pytest_unconfigure():
    observed['events']=events
    Path('observed.json').write_text(json.dumps(observed))
"""

_TEST = """from conftest import MODE,stop

def test_prefix():pass

def test_target(resource,subtests):
    if MODE=='child_pass':
        with subtests.test('retained passed child'):pass
    elif MODE=='child_fail':
        with subtests.test('retained failed child'):assert False,'child failure'
    elif MODE=='child_control':
        with subtests.test('retained control child'):stop()
    elif MODE=='child_nested':
        with subtests.test('outer'):
            with subtests.test('inner'):stop()
    if MODE in {'call','child_pass','child_fail','protocol_replace','protocol_swallow'}:stop()

def test_later():pass
"""

_CASES = [
    (mode, control)
    for mode in [
        "setup",
        "call",
        "teardown",
        "child_pass",
        "child_fail",
        "child_control",
        "child_nested",
        "protocol_replace",
        "protocol_swallow",
    ]
    for control in ["keyboard", "exit"]
] + [
    (mode, "keyboard")
    for mode in [
        "log_setup_ordinary",
        "log_call_ordinary",
        "log_teardown_ordinary",
        "log_setup_skipped",
        "log_setup_failed",
        "protocol_complete_error",
    ]
]


def _observe(project: VfyProject, engine: str, mode: str, control: str) -> dict[str, Any]:
    result = (
        project.run_python("-B", "-m", "pytest", "-q", "-p", "no:cacheprovider")
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    )
    assert (project.root / "observed.json").exists(), result.stdout + result.stderr
    observed = json.loads((project.root / "observed.json").read_text())
    status = (
        3
        if mode.endswith("ordinary") or mode in {"protocol_replace", "protocol_complete_error"}
        else 1
        if mode == "protocol_swallow"
        else 42
        if control == "exit"
        else 2
    )
    assert result.returncode == status, result.stdout + result.stderr
    assert observed["exitstatus"] == status
    assert any(report[0] == "test_cases.py::test_prefix" for report in observed["reports"])
    return observed


@pytest.mark.parametrize(("mode", "control"), _CASES)
def test_interrupted_callback_case_boundaries_preserve_actual_report_history(
    tmp_path: Path, mode: str, control: str
) -> None:
    observed = {}
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", f"MODE={mode!r}\nCONTROL={control!r}\n" + _PLUGIN)
        project.write("test_cases.py", _TEST)
        observed[engine] = _observe(project, engine, mode, control)
    assert observed["native"] == observed["stock"]
    target = [
        report for report in observed["stock"]["reports"] if report[0].endswith("::test_target")
    ]
    if mode == "setup":
        assert target == []
    elif mode in {"call", "protocol_replace", "protocol_swallow", "log_setup_ordinary"}:
        assert [report[1:4] for report in target] == [["setup", "passed", None]]
    elif mode.startswith("child_"):
        assert target[0][1:4] == ["setup", "passed", None]
        assert len(target) == (3 if mode == "child_nested" else 2)
        assert all(report[1] == "call" and report[3] is not None for report in target[1:])
    elif mode.startswith("log_setup_"):
        assert target[0][1:4] == [
            "setup",
            "skipped" if mode.endswith("skipped") else "failed",
            None,
        ]
    else:
        assert target[1][1:4] == ["call", "passed", None]
