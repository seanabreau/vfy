"""Report creation hooks receive the completed phase and exception."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_passed_setup_with_wasxfail_still_runs_call_and_fixture_cleanup(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path
reports = []

@pytest.fixture(autouse=True)
def resource():
    yield
    Path('fixture-finalized').touch()

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if call.when == 'setup':
        assert report.passed
        report.wasxfail = 'setup expected-failure metadata'
    return report

def pytest_runtest_logreport(report):
    reports.append([report.when, report.outcome, report.passed,
                    getattr(report, 'wasxfail', None)])

def pytest_unconfigure():
    Path('observed.json').write_text(json.dumps({
        'body': Path('body-ran').exists(),
        'fixture_finalized': Path('fixture-finalized').exists(),
        'reports': reports,
    }))
""",
    )
    project.write(
        "test_body.py",
        "from pathlib import Path\ndef test_body(): Path('body-ran').touch()\n",
    )
    oracle = project.run_engine("stock", "-q")
    oracle.expect(0)
    expected = json.loads((tmp_path / "observed.json").read_text())
    assert expected == {
        "body": True,
        "fixture_finalized": True,
        "reports": [
            ["setup", "passed", True, "setup expected-failure metadata"],
            ["call", "passed", True, None],
            ["teardown", "passed", True, None],
        ],
    }
    for name in ("body-ran", "fixture-finalized", "observed.json"):
        (tmp_path / name).unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "--workers=1", "-q")
    assert candidate.returncode == oracle.returncode, candidate.stdout + candidate.stderr
    assert json.loads((tmp_path / "observed.json").read_text()) == expected


def test_session_report_consumer_has_its_actual_callable_signature(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import inspect, json
from pathlib import Path

def pytest_sessionstart(session):
    parameters = inspect.signature(session.pytest_runtest_logreport).parameters.values()
    Path('signature.json').write_text(json.dumps([
        [parameter.name, parameter.kind.name,
         parameter.default is inspect.Parameter.empty] for parameter in parameters
    ]))
""",
    )
    project.write("test_body.py", "def test_body():\n    pass\n")
    stock = project.run_engine("stock", "-q")
    stock.expect(0)
    expected = json.loads((tmp_path / "signature.json").read_text())
    assert expected == [["report", "POSITIONAL_OR_KEYWORD", True]]
    (tmp_path / "signature.json").unlink()
    candidate = project.run_engine("native")
    candidate.expect(0)
    assert json.loads((tmp_path / "signature.json").read_text()) == expected


@pytest.mark.parametrize("stage", ["pass", "setup", "call", "teardown"])
@pytest.mark.parametrize("style", ["wrapper", "hookwrapper"])
def test_makereport_receives_real_phase_exceptions_and_retains_live_reports(
    tmp_path: Path, stage: str, style: str
) -> None:
    project = VfyProject(tmp_path)
    result = "outcome.get_result()" if style == "hookwrapper" else "outcome"
    project.write(
        "report_plugin.py",
        f"""import json, pytest
from pathlib import Path
events = []
calls = []
reports = []

@pytest.hookimpl({style}=True)
def pytest_runtest_makereport(item, call):
    assert call.when in ('setup', 'call', 'teardown')
    assert call.duration >= 0 and call.stop >= call.start > 0
    events.append(['before', call.when])
    call.custom = {{'phase': call.when}}
    info = call.excinfo
    if info is None:
        assert call.result is None
    else:
        assert info.type is RuntimeError
        assert info.value.marker is not None
        last = info.tb
        while last.tb_next is not None:
            last = last.tb_next
        assert last.tb_frame.f_locals['error'] is info.value
        assert last.tb_frame.f_locals['sentinel'] is info.value.marker
        with pytest.raises(AttributeError, match='has no valid result'):
            _ = call.result
        info.value.args = ('edited by report hook',)
    calls.append(call)
    outcome = yield
    report = {result}
    assert report.when == call.when
    assert report.duration == call.duration
    assert report.start == call.start and report.stop == call.stop
    report.sections.append(('makereport', call.when))
    reports.append(report)
    events.append(['after', call.when])
    return report

def pytest_runtest_logreport(report):
    assert report is reports[-1]
    events.append(['log', report.when])

def pytest_unconfigure():
    data = []
    for call, report in zip(calls, reports, strict=True):
        assert call.custom == {{'phase': call.when}}
        assert (call.excinfo is None) == report.passed
        data.append([report.when, report.outcome, report.sections,
                     'edited by report hook' in report.longreprtext])
    Path('makereport.json').write_text(json.dumps([events, data]))
""",
    )
    project.write(
        "conftest.py",
        f"""import pytest
stage = {stage!r}

def fail_phase():
    sentinel = object()
    error = RuntimeError('original phase exception')
    error.marker = sentinel
    raise error

@pytest.fixture(autouse=True)
def phases():
    if stage == 'setup':
        fail_phase()
    yield
    if stage == 'teardown':
        fail_phase()
""",
    )
    project.write(
        "test_body.py",
        """from conftest import fail_phase, stage
def test_body():
    if stage == 'call':
        fail_phase()
""",
    )
    oracle = project.run_engine("stock", "-q", "-p", "report_plugin")
    assert oracle.returncode == (0 if stage == "pass" else 1), oracle.stdout + oracle.stderr
    expected = json.loads((tmp_path / "makereport.json").read_text())
    (tmp_path / "makereport.json").unlink()
    candidate = project.run_engine("native", "-p", "report_plugin")
    assert candidate.returncode == oracle.returncode, candidate.stdout + candidate.stderr
    assert json.loads((tmp_path / "makereport.json").read_text()) == expected


def test_exception_info_keeps_the_original_traceback_after_value_mutation(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path
rows = []

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    if call.excinfo is not None:
        info = call.excinfo
        original = info.tb
        info.value.__traceback__ = None
        assert info.tb is original
    report = yield
    if call.excinfo is not None:
        assert "raise ValueError('snapshot witness')" in report.longreprtext
        rows.append([report.when, call.excinfo.tb is original,
                     call.excinfo.value.__traceback__ is None])
    return report

def pytest_unconfigure():
    Path('traceback.json').write_text(json.dumps(rows))
""",
    )
    project.write(
        "test_body.py",
        """import pytest

def test_context():
    with pytest.raises(ValueError) as info:
        raise ValueError('context witness')
    original = info.tb
    info.value.__traceback__ = None
    assert info.tb is original

def test_report():
    raise ValueError('snapshot witness')
""",
    )
    stock = project.run_engine("stock", "-q")
    stock.expect(1)
    expected = json.loads((tmp_path / "traceback.json").read_text())
    assert expected == [["call", True, True]]
    (tmp_path / "traceback.json").unlink()
    candidate = project.run_engine("native")
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert json.loads((tmp_path / "traceback.json").read_text()) == expected


@pytest.mark.parametrize("runxfail", [False, True])
def test_runxfail_preserves_helper_identity_and_configuration_cleanup(
    tmp_path: Path, runxfail: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest
from pathlib import Path

def pytest_configure(config):
    original = pytest.xfail
    config.add_cleanup(lambda: Path('restored').write_text(str(pytest.xfail is original)))
""",
    )
    project.write(
        "test_body.py",
        """import pytest
from pathlib import Path

@pytest.mark.xfail(run=False, reason='marked test')
def test_mark():
    Path('mark').touch()

def test_helper():
    pytest.xfail('helper test')
    Path('helper').touch()

def test_exception():
    raise pytest.xfail.Exception('literal exception')
""",
    )
    arguments = ["--runxfail"] if runxfail else []
    stock = project.run_engine("stock", "-q", *arguments)
    assert stock.returncode == int(runxfail), stock.stdout + stock.stderr
    expected = [(name, (tmp_path / name).exists()) for name in ("mark", "helper")]
    assert (tmp_path / "restored").read_text() == "True"
    for name, _exists in expected:
        (tmp_path / name).unlink(missing_ok=True)
    (tmp_path / "restored").unlink()
    candidate = project.run_engine("native", *arguments)
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert [(name, (tmp_path / name).exists()) for name, _exists in expected] == expected
    assert (tmp_path / "restored").read_text() == "True"


def test_runner_makereport_is_registered_and_accepts_direct_calls(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path


def pytest_collection_modifyitems(session, items):
    item = items[0]
    hook = item.ihook.pytest_runtest_makereport
    implementations = hook.get_hookimpls()
    call = pytest.CallInfo.from_call(lambda: None, when='call')
    report = hook(item=item, call=call)
    Path('direct-report.json').write_text(json.dumps({
        'runner': any(impl.plugin_name == 'runner' for impl in implementations),
        'nodeid': report.nodeid,
        'when': report.when,
        'outcome': report.outcome,
        'duration': report.duration == call.duration,
    }))
""",
    )
    project.write("test_direct.py", "def test_direct(): pass\n")
    stock = project.run_engine("stock", "-q")
    stock.expect(0)
    expected = json.loads((tmp_path / "direct-report.json").read_text())
    assert expected == {
        "runner": True,
        "nodeid": "test_direct.py::test_direct",
        "when": "call",
        "outcome": "passed",
        "duration": True,
    }
    (tmp_path / "direct-report.json").unlink()
    candidate = project.run_engine("native", "-q")
    candidate.expect(0)
    assert json.loads((tmp_path / "direct-report.json").read_text()) == expected


def test_makereport_monitor_sees_registered_runner(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

rows = []

def pytest_configure(config):
    def before(name, methods, kwargs):
        if name == 'pytest_runtest_makereport':
            rows.append([kwargs['call'].when, [m.plugin_name for m in methods].count('runner')])
    def after(outcome, name, methods, kwargs):
        pass
    config.pluginmanager.add_hookcall_monitoring(before, after)

def pytest_unconfigure():
    Path('monitor.json').write_text(json.dumps(rows))
""",
    )
    project.write("test_body.py", "def test_body(): pass\n")
    stock = project.run_engine("stock", "-q")
    stock.expect(0)
    expected = json.loads((tmp_path / "monitor.json").read_text())
    assert expected == [["setup", 1], ["call", 1], ["teardown", 1]]
    (tmp_path / "monitor.json").unlink()
    candidate = project.run_engine("native", "-q")
    candidate.expect(0)
    assert json.loads((tmp_path / "monitor.json").read_text()) == expected


def test_firstresult_hook_can_call_registered_runner(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path

rows = []

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_makereport(item, call):
    runner = item.config.pluginmanager.get_plugin('runner')
    report = runner.pytest_runtest_makereport(item, call)
    report.sections.append(('custom', call.when))
    return report

def pytest_runtest_logreport(report):
    rows.append([report.when, report.outcome, list(report.sections)])

def pytest_unconfigure():
    Path('runner-results.json').write_text(json.dumps(rows))
""",
    )
    project.write("test_body.py", "def test_body(): pass\n")
    stock = project.run_engine("stock", "-q")
    stock.expect(0)
    expected = json.loads((tmp_path / "runner-results.json").read_text())
    assert expected == [
        ["setup", "passed", [["custom", "setup"]]],
        ["call", "passed", [["custom", "call"]]],
        ["teardown", "passed", [["custom", "teardown"]]],
    ]
    (tmp_path / "runner-results.json").unlink()
    candidate = project.run_engine("native", "-q")
    candidate.expect(0)
    assert json.loads((tmp_path / "runner-results.json").read_text()) == expected


def test_runner_can_be_called_without_short_circuiting_makereport(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path
rows = []
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_makereport(item, call):
    runner = item.config.pluginmanager.get_plugin('runner')
    report = runner.pytest_runtest_makereport(item, call)
    rows.append(['probe', report.when, report.outcome, report.capstdout])

def pytest_runtest_logreport(report):
    rows.append(['logged', report.when, report.outcome, report.capstdout])

def pytest_unconfigure():
    Path('runner-repeat.json').write_text(json.dumps(rows))
""",
    )
    project.write("test_body.py", "def test_body(): print('captured witness')\n")
    stock = project.run_engine("stock", "-q")
    stock.expect(0)
    expected = json.loads((tmp_path / "runner-repeat.json").read_text())
    assert expected == [
        ["probe", "setup", "passed", ""],
        ["logged", "setup", "passed", ""],
        ["probe", "call", "passed", "captured witness\n"],
        ["logged", "call", "passed", "captured witness\n"],
        ["probe", "teardown", "passed", "captured witness\n"],
        ["logged", "teardown", "passed", "captured witness\n"],
    ]
    (tmp_path / "runner-repeat.json").unlink()
    candidate = project.run_engine("native", "-q")
    candidate.expect(0)
    assert json.loads((tmp_path / "runner-repeat.json").read_text()) == expected
