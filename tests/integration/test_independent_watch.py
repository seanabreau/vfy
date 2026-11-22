"""Independent watch publishes or preserves accepted artifacts for every generation."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from tests.integration.test_watch import _wait_for_watch_event, _wait_for_watch_path
from tests.support import VfyProject
from tests.support.history import read_history
from tests.support.independent_private_aliases import PRIVATE_ALIASES
from tests.support.isolated_package import isolated_vfy_path

_ROOT = Path(__file__).resolve().parents[2]
_ALIAS_CONFTEST = """from private_aliases import assert_private_aliases

def pytest_configure(config):
    assert_private_aliases()
"""
_REPORT_ARGUMENTS = (
    "--junitxml=reports/junit.xml",
    "--cov-report=json:reports/coverage.json",
    "--native-result-json=reports/native.json",
)
_ACCEPTED = (
    ".coverage",
    ".vfy/test-history.sqlite3",
    ".vfy/replay.coverage",
    ".vfy/test-run.json",
    "reports/junit.xml",
    "reports/coverage.json",
    "reports/native.json",
)


def _project(tmp_path: Path) -> VfyProject:
    project = VfyProject(tmp_path)
    project.write("private_aliases.py", PRIVATE_ALIASES)
    project.write("conftest.py", _ALIAS_CONFTEST)
    project.write("subject.py", "def value():\n    return 1\n")
    project.write("reports/.keep", "")
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            """import sys
import time
from pathlib import Path
from subject import value

def test_value():
    assert not any(name.split('.')[0] in {'pluggy', 'packaging'} for name in sys.modules)
    observed = value()
    if observed in (2, 3):
        Path('.vfy/blocked-' + str(observed)).touch()
        while not Path('.vfy/release-' + str(observed)).exists():
            time.sleep(.01)
    assert observed in (1, 3)
""",
        )
    return project


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
@pytest.mark.parametrize(
    ("workers", "report_source"), [(1, "configuration"), (2, "environment"), (2, "cli")]
)
def test_public_watch_preserves_obsolete_artifacts_across_generations(
    tmp_path: Path, workers: int, report_source: str
) -> None:
    project = _project(tmp_path)
    arguments = list(_REPORT_ARGUMENTS) if report_source == "cli" else []
    project.write(
        "pytest.ini",
        "[pytest]\n"
        + (
            "addopts = " + " ".join(_REPORT_ARGUMENTS) + "\n"
            if report_source == "configuration"
            else ""
        ),
    )
    environment = {"PYTHONPATH": isolated_vfy_path()}
    if report_source == "environment":
        environment["PYTEST_ADDOPTS"] = " ".join(_REPORT_ARGUMENTS)
    command = project.start_vfy(
        "--verbose",
        "test",
        "--watch",
        f"--workers={workers}",
        "--source=subject.py",
        *arguments,
        env=environment,
    )
    try:
        baseline_run = _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        accepted = {name: (tmp_path / name).read_bytes() for name in _ACCEPTED}
        project.write("subject.py", "def value():\n    return 2\n")
        _wait_for_watch_path(command, project, ".vfy/blocked-2")
        project.write("subject.py", "def value():\n    return 3\n")
        project.write(".vfy/release-2", "")
        obsolete_run = _wait_for_watch_event(
            command, after_run=baseline_run, expected="obsolete; queued changes"
        )
        _wait_for_watch_path(command, project, ".vfy/blocked-3")
        assert {name: (tmp_path / name).read_bytes() for name in _ACCEPTED} == accepted
        project.write(".vfy/release-3", "")
        _wait_for_watch_event(command, after_run=obsolete_run, expected="pytest exited 0")
        native = json.loads((tmp_path / "reports/native.json").read_text())
        assert native["engine"] == "vfy-independent-v1"
        assert native["collected"] == 2
        assert native["counts"]["passed"] == 2
        assert native["execution_topology"]["requested_workers"] == workers
        # Every watch generation runs the complete suite.
        assert (
            command.text().count("Note: the requested workflow requires a complete test run") == 3
        )
        history = read_history(tmp_path / ".vfy/test-history.sqlite3")["tests"]
        assert set(history) == {"test_a.py::test_value", "test_b.py::test_value"}
        assert all(len(test["observations"]) == 2 for test in history.values())
        data = project.data()
        contexts = data.contexts_by_lineno(str(tmp_path / "subject.py"))
        assert set(contexts[2]) == {"test_a.py::test_value|run", "test_b.py::test_value|run"}
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=15)
    result.expect(2)


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
@pytest.mark.parametrize("fault", ["publish", "save"])
def test_watch_coverage_failure_keeps_accepted_artifacts_until_repair(
    tmp_path: Path, fault: str
) -> None:
    project = _project(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = " + " ".join(_REPORT_ARGUMENTS) + "\n")
    project.write(
        "conftest.py",
        _ALIAS_CONFTEST
        + """import os
from pathlib import Path
from vfy import Coverage
from vfy.data import CoverageData

if Path('.vfy/fault').exists():
    def fail(*args, **kwargs):
        raise RuntimeError('intentional coverage boundary failure')
    Path('.vfy/loaded-' + str(os.getpid())).touch()
    boundary = {'publish': (CoverageData, '_publish'), 'save': (Coverage, 'save')}
    setattr(*boundary[Path('.vfy/fault').read_text()], fail)
""",
    )
    command = project.start_vfy(
        "test",
        "--engine=native",
        "--watch",
        "--workers=2",
        "--source=subject.py",
        env={"PYTHONPATH": isolated_vfy_path()},
    )
    try:
        baseline_run = _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        accepted = {name: (tmp_path / name).read_bytes() for name in _ACCEPTED}
        project.write(".vfy/fault", fault)
        project.write(".vfy/release-3", "")
        project.write("subject.py", "def value():\n    return 3\n")
        failed_run = _wait_for_watch_event(
            command, after_run=baseline_run, expected="execution error:"
        )
        assert {name: (tmp_path / name).read_bytes() for name in _ACCEPTED} == accepted
        (tmp_path / ".vfy/fault").unlink()
        project.write("subject.py", "def value():\n    return 1\n")
        _wait_for_watch_event(command, after_run=failed_run, expected="pytest exited 0")
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=15)
    result.expect(2)


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
def test_complete_current_failure_publishes_red_reports_and_preserves_green_marker(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = " + " ".join(_REPORT_ARGUMENTS) + "\n")
    command = project.start_vfy(
        "test",
        "--engine=native",
        "--watch",
        "--workers=2",
        "--source=subject.py",
        env={"PYTHONPATH": isolated_vfy_path()},
    )
    try:
        baseline_run = _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        green = (tmp_path / ".vfy/test-run.json").read_bytes()
        replay = (tmp_path / ".vfy/replay.coverage").read_bytes()
        project.write(".vfy/release-2", "")
        project.write("subject.py", "def value():\n    return 2\n")
        failed_run = _wait_for_watch_event(
            command, after_run=baseline_run, expected="pytest exited 1"
        )
        assert (tmp_path / ".vfy/test-run.json").read_bytes() == green
        assert (tmp_path / ".vfy/replay.coverage").read_bytes() == replay
        native = json.loads((tmp_path / "reports/native.json").read_text())
        assert native["counts"]["failed"] == 2
        assert native["exitstatus"] == 1
        history = read_history(tmp_path / ".vfy/test-history.sqlite3")["tests"]
        assert all(test["observations"][-1]["outcome"] == "failed" for test in history.values())
        project.write("subject.py", "def value():\n    return 1\n")
        _wait_for_watch_event(command, after_run=failed_run, expected="pytest exited 0")
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=15)
    result.expect(2)


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
def test_publication_failure_preserves_all_accepted_bytes_until_repair(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = " + " ".join(_REPORT_ARGUMENTS) + "\n")
    project.write("blocked", "output parent is a file")
    project.write(".vfy/staging/.keep", "")
    command = project.start_vfy(
        "test",
        "--engine=native",
        "--watch",
        "--workers=2",
        "--source=subject.py",
        env={"PYTHONPATH": isolated_vfy_path(), "TMPDIR": str(tmp_path / ".vfy/staging")},
    )
    try:
        baseline_run = _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        accepted = {name: (tmp_path / name).read_bytes() for name in _ACCEPTED}
        project.write(
            "pytest.ini",
            "[pytest]\naddopts = "
            + " ".join(_REPORT_ARGUMENTS)
            + " --cov-report=xml:blocked/coverage.xml\n",
        )
        failed_run = _wait_for_watch_event(
            command, after_run=baseline_run, expected="publication error:"
        )
        assert {name: (tmp_path / name).read_bytes() for name in _ACCEPTED} == accepted
        assert not list((tmp_path / ".vfy/staging").glob("vfy-watch-run-*"))
        (tmp_path / "blocked").unlink()
        (tmp_path / "blocked").mkdir()
        _wait_for_watch_event(command, after_run=failed_run, expected="pytest exited 0")
        assert (tmp_path / "blocked/coverage.xml").is_file()
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=15)
    result.expect(2)


@pytest.mark.skipif(os.name != "posix", reason="worker descendant locks require POSIX")
def test_watch_interrupt_stops_workers_and_descendants_without_publication(tmp_path: Path) -> None:
    import fcntl  # noqa: PLC0415 - POSIX-only process-lifetime proof

    project = _project(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = " + " ".join(_REPORT_ARGUMENTS) + "\n")
    project.write(
        "descendant.py",
        "import fcntl, signal, sys\n"
        "with open(sys.argv[1], 'w') as locked:\n"
        "    fcntl.flock(locked, fcntl.LOCK_EX)\n"
        "    print('ready', flush=True)\n"
        "    signal.pause()\n",
    )
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            "import fcntl, signal, subprocess, sys\nfrom pathlib import Path\n"
            "from subject import value\n"
            "def test_value():\n"
            "    assert value() > 0\n"
            "    if not Path('.vfy/interrupt').exists():\n        return\n"
            f"    with open('.vfy/worker-{name}.lock', 'w') as locked:\n"
            "        fcntl.flock(locked, fcntl.LOCK_EX)\n"
            "        child = subprocess.Popen([sys.executable, 'descendant.py', "
            f"'.vfy/descendant-{name}.lock'], stdout=subprocess.PIPE, text=True)\n"
            "        assert child.stdout.readline().strip() == 'ready'\n"
            f"        Path('.vfy/ready-{name}').touch()\n"
            "        signal.pause()\n",
        )
    command = project.start_vfy(
        "test",
        "--engine=native",
        "--watch",
        "--workers=2",
        "--source=subject.py",
        env={"PYTHONPATH": isolated_vfy_path()},
    )
    try:
        _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        accepted = {name: (tmp_path / name).read_bytes() for name in _ACCEPTED}
        project.write(".vfy/interrupt", "")
        project.write("subject.py", "def value():\n    return 3\n")
        for name in ("a", "b"):
            _wait_for_watch_path(command, project, f".vfy/ready-{name}")
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=15)
        result.expect(2)
        assert {name: (tmp_path / name).read_bytes() for name in _ACCEPTED} == accepted
        for name in ("a", "b"):
            for kind in ("worker", "descendant"):
                with (tmp_path / f".vfy/{kind}-{name}.lock").open() as locked:
                    fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if command.poll() is None:
            command.send_signal(signal.SIGINT)
            command.wait(timeout=15)


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
@pytest.mark.parametrize(
    ("workers", "option_source"),
    [(1, "configuration"), (2, "configuration"), (1, "environment"), (2, "cli")],
)
def test_collection_only_watch_preserves_execution_artifacts_and_recovers(
    tmp_path: Path, workers: int, option_source: str
) -> None:
    project = _project(tmp_path)
    normal_config = "[pytest]\naddopts = " + " ".join(_REPORT_ARGUMENTS) + "\n"
    project.write("pytest.ini", normal_config)
    arguments = (
        "test",
        "--engine=native",
        "--watch",
        f"--workers={workers}",
        "--source=subject.py",
    )
    environment = {"PYTHONPATH": isolated_vfy_path()}
    command = project.start_vfy(*arguments, env=environment)
    try:
        baseline = _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        accepted = {name: (tmp_path / name).read_bytes() for name in _ACCEPTED}
        if option_source == "configuration":
            project.write(
                "pytest.ini", normal_config.replace("addopts = ", "addopts = --collect-only ")
            )
            after_run = baseline
        else:
            command.send_signal(signal.SIGINT)
            assert command.wait(timeout=15).returncode == 2
            collection_environment = dict(environment)
            collection_arguments = list(arguments)
            if option_source == "environment":
                collection_environment["PYTEST_ADDOPTS"] = "--collect-only"
            else:
                collection_arguments.append("--collect-only")
            command = project.start_vfy(*collection_arguments, env=collection_environment)
            after_run = 0
        rejected = _wait_for_watch_event(command, after_run=after_run, expected="execution error:")
        assert "2 tests collected" in command.text("stdout")
        assert {name: (tmp_path / name).read_bytes() for name in _ACCEPTED} == accepted
        if option_source == "configuration":
            project.write("pytest.ini", normal_config)
            after_run = rejected
        else:
            command.send_signal(signal.SIGINT)
            assert command.wait(timeout=15).returncode == 2
            command = project.start_vfy(*arguments, env=environment)
            after_run = 0
        _wait_for_watch_event(command, after_run=after_run, expected="pytest exited 0")
        native = json.loads((tmp_path / "reports/native.json").read_text())
        assert native["collected"] == len(native["cases"]) == 2
        assert native["counts"]["passed"] == 2
        history = read_history(tmp_path / ".vfy/test-history.sqlite3")["tests"]
        assert all(len(test["observations"]) == 2 for test in history.values())
    finally:
        if command.poll() is None:
            command.send_signal(signal.SIGINT)
        result = command.wait(timeout=15)
    result.expect(2)


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
@pytest.mark.parametrize("workers", [1, 2])
def test_completed_skips_xfails_and_deselection_remain_publishable(
    tmp_path: Path, workers: int
) -> None:
    project = _project(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = " + " ".join(_REPORT_ARGUMENTS) + "\n")
    project.write(
        "test_a.py",
        "import pytest\nfrom subject import value\n"
        "def test_pass():\n    assert value() == 1\n"
        "@pytest.mark.skip(reason='valid skipped setup')\n"
        "def test_skip():\n    raise AssertionError('skipped body executed')\n",
    )
    project.write(
        "test_b.py",
        "import pytest\n"
        "@pytest.mark.xfail(reason='valid xfailed setup', run=False)\n"
        "def test_xfail():\n    raise AssertionError('xfailed body executed')\n"
        "def test_omitted():\n    raise AssertionError('deselected body executed')\n",
    )
    command = project.start_vfy(
        "test",
        "--engine=native",
        "--watch",
        f"--workers={workers}",
        "--source=subject.py",
        "-k",
        "not omitted",
        env={"PYTHONPATH": isolated_vfy_path()},
    )
    try:
        _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        native = json.loads((tmp_path / "reports/native.json").read_text())
        assert native["collected"] == len(native["cases"]) == 3
        assert native["deselected_nodeids"] == ["test_b.py::test_omitted"]
        assert {name: native["counts"][name] for name in ("passed", "skipped", "xfailed")} == {
            "passed": 1,
            "skipped": 1,
            "xfailed": 1,
        }
        history = read_history(tmp_path / ".vfy/test-history.sqlite3")["tests"]
        assert {name: test["observations"][-1]["outcome"] for name, test in history.items()} == {
            "test_a.py::test_pass": "passed",
            "test_a.py::test_skip": "skipped",
            "test_b.py::test_xfail": "skipped",
        }
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=15)
    result.expect(2)
