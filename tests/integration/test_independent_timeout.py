"""Signal timeouts interrupt owned phases and release their process resources."""

from __future__ import annotations

import json
import signal
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="POSIX signal timers")


def _failures(project: VfyProject) -> dict[str, list[tuple[str, str | None]]]:
    return {
        str(case.get("name")): [
            (problem.tag, problem.get("message"))
            for problem in case
            if problem.tag in {"failure", "error"}
        ]
        for case in ET.parse(project.root / "result.xml").iter("testcase")
    }


@pytest.mark.parametrize("workers", [1, 2])
def test_signal_timeouts_match_pinned_phase_and_fixture_outcomes(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write("pytest.ini", "[pytest]\ntimeout = 0.1\n")
    fixtures = """import time
import pytest
@pytest.fixture
def slow_setup(): time.sleep(2)
@pytest.fixture
def slow_teardown():
    yield
    time.sleep(2)
@pytest.fixture
def excluded_fixture():
    time.sleep(0.2)
    yield
    time.sleep(0.2)
"""
    cases = {
        "call": "@pytest.mark.timeout(0.1)\ndef test_call(): time.sleep(2)\n",
        "setup": "@pytest.mark.timeout(0.1)\ndef test_setup(slow_setup): pass\n",
        "teardown": "@pytest.mark.timeout(0.1)\ndef test_teardown(slow_teardown): pass\n",
        "func_only": "@pytest.mark.timeout(0.1, func_only=True)\n"
        "def test_func_only(excluded_fixture): time.sleep(2)\n",
        "excluded": "@pytest.mark.timeout(0.1, func_only=True)\n"
        "def test_excluded(excluded_fixture): pass\n",
        "disabled": "@pytest.mark.timeout(0)\ndef test_disabled(): time.sleep(0.2)\n",
        "default": "def test_default(): time.sleep(2)\n",
        "debugger": "import sys\n"
        "@pytest.fixture\ndef active_debugger():\n"
        "    sys.monitoring.use_tool_id(sys.monitoring.DEBUGGER_ID, 'test-debugger')\n"
        "    try: yield\n"
        "    finally: sys.monitoring.free_tool_id(sys.monitoring.DEBUGGER_ID)\n"
        "@pytest.mark.timeout(0.1, func_only=True)\n"
        "def test_debugger(active_debugger): time.sleep(0.2)\n",
    }
    for name, source in cases.items():
        project.write(f"test_{name}.py", fixtures + source)
    oracle = project.run_pytest("-p", "pytest_timeout", "--junitxml=result.xml", load_xdist=False)
    oracle.expect(1)
    expected = _failures(project)
    (tmp_path / "result.xml").unlink()
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.independent.timeout",
        f"--workers={workers}",
        "--junitxml=result.xml",
        env={"VFY_WORKER_START": "exec"},
    )
    actual.expect(1)
    observed = _failures(project)
    assert {name: [tag for tag, _ in failures] for name, failures in observed.items()} == {
        name: [tag for tag, _ in failures] for name, failures in expected.items()
    }
    assert {name for name, failures in expected.items() if failures} == {
        "test_call",
        "test_setup",
        "test_teardown",
        "test_func_only",
        "test_default",
    }
    for engine, reports in [("pytest-timeout", expected), ("vfy", observed)]:
        assert all(
            f"Timeout (>0.1s) from {engine}." in (message or "")
            for failures in reports.values()
            for _, message in failures
        )


@pytest.mark.parametrize("source", ["ini", "environment", "command"])
def test_timeout_configuration_precedence_matches_pinned_plugin(
    tmp_path: Path, source: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", f"[pytest]\ntimeout = {0.1 if source == 'ini' else 2}\n")
    project.write("test_timer.py", "import time\ndef test_timer(): time.sleep(2)\n")
    environment = {"PYTEST_TIMEOUT": None if source == "ini" else "0.1"}
    arguments = ["--timeout=0.1"] if source == "command" else []
    if source == "command":
        environment["PYTEST_TIMEOUT"] = "3"
    oracle = project.run_pytest(
        "-p", "pytest_timeout", *arguments, env=environment, load_xdist=False
    )
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.independent.timeout",
        *arguments,
        env=environment,
    )
    for engine, result in [("pytest-timeout", oracle), ("vfy", actual)]:
        result.expect(1)
        assert f"Timeout (>0.1s) from {engine}." in result.stdout + result.stderr


@pytest.mark.parametrize("outcome", ["setup", "teardown", "skip", "interrupt", "exit", "timeout"])
def test_timeout_restores_handler_and_cancels_timer_after_abnormal_phases(
    tmp_path: Path, outcome: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
import signal
from pathlib import Path
def previous(signum, frame): pass
def pytest_sessionstart(session): signal.signal(signal.SIGALRM, previous)
def pytest_sessionfinish(session, exitstatus):
    Path('signals.json').write_text(json.dumps([
        signal.getsignal(signal.SIGALRM) is previous,
        signal.getitimer(signal.ITIMER_REAL),
    ]))
""",
    )
    source = {
        "setup": "@pytest.fixture\ndef resource(): raise RuntimeError('setup')\n"
        "def test_body(resource): pass\n",
        "teardown": "@pytest.fixture\ndef resource():\n    yield\n"
        "    raise RuntimeError('teardown')\ndef test_body(resource): pass\n",
        "skip": "def test_body(): pytest.skip('skip')\n",
        "interrupt": "def test_body(): raise KeyboardInterrupt\n",
        "exit": "def test_body(): pytest.exit('exit', returncode=2)\n",
        "timeout": "import time\ndef test_body(): time.sleep(2)\n",
    }[outcome]
    project.write("test_timer.py", "import pytest\n" + source)
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.independent.timeout",
        "--timeout=0.1" if outcome == "timeout" else "--timeout=10",
    )
    assert (
        result.returncode
        == {"setup": 1, "teardown": 1, "skip": 0, "interrupt": 2, "exit": 2, "timeout": 1}[outcome]
    ), result.stdout + result.stderr
    assert json.loads((tmp_path / "signals.json").read_text()) == [True, [0.0, 0.0]]


@pytest.mark.parametrize("stage", ["protocol", "collection"])
def test_timeout_preserves_an_external_deadline(tmp_path: Path, stage: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
import signal
from pathlib import Path
def previous(signum, frame): pass
def pytest_sessionstart(session):
    signal.signal(signal.SIGALRM, previous)
    signal.setitimer(signal.ITIMER_REAL, 60)
def pytest_sessionfinish(session, exitstatus):
    Path('signals.json').write_text(json.dumps([
        signal.getsignal(signal.SIGALRM) is previous,
        signal.getitimer(signal.ITIMER_REAL)[0] > 30,
    ]))
    signal.setitimer(signal.ITIMER_REAL, 0)
""",
    )
    project.write(
        "test_timer.py",
        "from pathlib import Path\ndef test_body(): Path('ran').touch()\n"
        if stage == "protocol"
        else "raise RuntimeError('collection failed')\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.independent.timeout",
        "--timeout=1" if stage == "protocol" else "--timeout=0",
    )
    assert result.returncode == (4 if stage == "protocol" else 2), result.stdout + result.stderr
    diagnostic = (
        "cannot replace an active ITIMER_REAL deadline"
        if stage == "protocol"
        else "collection failed"
    )
    assert diagnostic in result.stdout + result.stderr
    assert not (tmp_path / "ran").exists()
    assert json.loads((tmp_path / "signals.json").read_text()) == [True, True]


@pytest.mark.parametrize("stage", ["collection_finish", "earlier_test"])
def test_late_timeout_markers_match_pinned_runtime_evaluation(tmp_path: Path, stage: str) -> None:
    project = VfyProject(tmp_path, timeout=5)
    if stage == "collection_finish":
        project.write(
            "conftest.py",
            """import pytest
def pytest_collection_finish(session):
    session.items[0].add_marker(pytest.mark.timeout(0.05))
""",
        )
    first_test = (
        ""
        if stage == "collection_finish"
        else """import pytest
def test_first(request):
    request.session.items[1].add_marker(pytest.mark.timeout(0.05))
"""
    )
    project.write(
        "test_late.py",
        "import time\nfrom pathlib import Path\n"
        + first_test
        + "def test_late():\n    time.sleep(0.2)\n    Path('ran').touch()\n",
    )
    oracle = project.run_pytest("-p", "pytest_timeout", load_xdist=False)
    oracle.expect(1)
    assert "Timeout (>0.05s) from pytest-timeout." in oracle.stdout + oracle.stderr
    assert not (tmp_path / "ran").exists()
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.independent.timeout",
    )
    actual.expect(1)
    assert "Timeout (>0.05s) from vfy." in actual.stdout + actual.stderr
    assert not (tmp_path / "ran").exists()


@pytest.mark.parametrize("stage", ["earlier_test", "fixture"])
def test_dynamic_unsupported_method_is_rejected_and_releases_timer(
    tmp_path: Path, stage: str
) -> None:
    project = VfyProject(tmp_path, timeout=5)
    project.write(
        "conftest.py",
        """import json
import signal
from pathlib import Path
def previous(signum, frame): pass
def pytest_sessionstart(session): signal.signal(signal.SIGALRM, previous)
def pytest_sessionfinish(session, exitstatus):
    Path('signals.json').write_text(json.dumps([
        signal.getsignal(signal.SIGALRM) is previous,
        signal.getitimer(signal.ITIMER_REAL),
    ]))
""",
    )
    mutation = (
        "def test_first(request):\n"
        "    request.session.items[1].add_marker(pytest.mark.timeout(0.1, method='thread'))\n"
        "def test_late(): Path('ran').touch()\n"
        if stage == "earlier_test"
        else "@pytest.fixture\ndef resource(request):\n"
        "    request.applymarker(pytest.mark.timeout(0.1, method='thread', func_only=True))\n"
        "def test_late(resource): Path('ran').touch()\n"
    )
    project.write(
        "test_late.py",
        "import pytest\nfrom pathlib import Path\npytestmark = pytest.mark.timeout(10)\n"
        + mutation,
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.independent.timeout",
    )
    assert result.returncode == (4 if stage == "earlier_test" else 1), result.stdout + result.stderr
    assert "thread timers require pytest-timeout" in result.stdout + result.stderr
    assert not (tmp_path / "ran").exists()
    assert json.loads((tmp_path / "signals.json").read_text()) == [True, [0.0, 0.0]]


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize(
    ("outcome", "dynamic_marker"),
    [
        ("pass", True),
        ("call_timeout", True),
        ("setup_error", True),
        ("teardown_error", True),
        ("setup_error", False),
    ],
)
def test_runtime_call_only_marker_replaces_owned_protocol_timer(
    tmp_path: Path, workers: int, outcome: str, dynamic_marker: bool
) -> None:
    project = VfyProject(tmp_path, timeout=10)
    project.write("pytest.ini", "[pytest]\ntimeout = 0.3\n")
    project.write(
        "conftest.py",
        """import json
import signal
from pathlib import Path
import pytest
def previous(signum, frame): pass
def pytest_sessionstart(session): signal.signal(signal.SIGALRM, previous)
@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item):
    try:
        return (yield)
    finally:
        Path('state-' + item.name + '.json').write_text(json.dumps([
            signal.getsignal(signal.SIGALRM) is previous,
            signal.getitimer(signal.ITIMER_REAL),
        ]))
""",
    )
    project.write(
        "test_dynamic.py",
        f"""import json
import signal
import time
from pathlib import Path
import pytest
outcome = {outcome!r}
dynamic_marker = {dynamic_marker!r}
@pytest.fixture
def switch(request):
    if dynamic_marker: request.applymarker(pytest.mark.timeout(0.05, func_only=True))
    def finish():
        Path('teardown-timer.json').write_text(json.dumps(
            signal.getitimer(signal.ITIMER_REAL)[0] > 0
        ))
        time.sleep(0.4)
        if outcome == 'teardown_error': raise RuntimeError('teardown failed')
    request.addfinalizer(finish)
    if outcome == 'setup_error': raise RuntimeError('setup failed')
def test_dynamic(switch):
    if outcome == 'call_timeout': time.sleep(2)
""",
    )
    project.write("test_other.py", "def test_other(): pass\n")
    oracle = project.run_pytest("-p", "pytest_timeout", "--junitxml=result.xml", load_xdist=False)
    assert oracle.returncode == int(outcome != "pass"), oracle.stdout + oracle.stderr
    expected = _failures(project)
    assert json.loads((tmp_path / "teardown-timer.json").read_text()) is False
    for name in [
        "result.xml",
        "teardown-timer.json",
        "state-test_dynamic.json",
        "state-test_other.json",
    ]:
        (tmp_path / name).unlink()
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.independent.timeout",
        f"--workers={workers}",
        "--junitxml=result.xml",
        env={"VFY_WORKER_START": "exec"},
    )
    assert actual.returncode == oracle.returncode, actual.stdout + actual.stderr
    observed = _failures(project)
    assert {name: [tag for tag, _ in failures] for name, failures in observed.items()} == {
        name: [tag for tag, _ in failures] for name, failures in expected.items()
    }
    for engine, reports in [("pytest-timeout", expected), ("vfy", observed)]:
        if outcome == "call_timeout":
            assert f"Timeout (>0.05s) from {engine}." in str(reports["test_dynamic"])
        elif outcome in {"setup_error", "teardown_error"}:
            assert outcome.replace("_error", " failed") in str(reports["test_dynamic"])
    assert json.loads((tmp_path / "teardown-timer.json").read_text()) is False
    for name in ["test_dynamic", "test_other"]:
        assert json.loads((tmp_path / f"state-{name}.json").read_text()) == [True, [0.0, 0.0]]
