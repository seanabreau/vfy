"""Public-process coverage for test watching."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest
from coverage import CoverageData

from tests.support import ManagedCommand, VfyProject

_WATCH_TERMINAL_PATTERN = (
    r"Watch run (?P<number>\d+): "
    r"(?P<event>obsolete; queued changes|pytest exited \d+|[^\r\n]+ error:)"
)


def _wait_for_watch_event(
    command: ManagedCommand,
    *,
    after_run: int,
    expected: str,
    timeout: float = 30.0,
) -> int:
    """Wait for an expected current result, advancing past obsolete runs."""

    deadline = time.monotonic() + timeout
    output_offset = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"timed out waiting for watch event {expected!r} after run {after_run}\n"
                f"stdout:\n{command.text('stdout')}\nstderr:\n{command.text()}"
            )
        match = command.wait_for_match(
            _WATCH_TERMINAL_PATTERN,
            after=output_offset,
            timeout=remaining,
        )
        output_offset = match.end()
        run_number = int(match.group("number"))
        event = match.group("event")
        if run_number <= after_run:
            continue
        if event == "obsolete; queued changes":
            if expected == event:
                return run_number
            after_run = run_number
            continue
        if event.startswith(expected):
            return run_number
        raise AssertionError(
            f"watch run {run_number} ended with {event!r}, expected {expected!r}\n"
            f"stdout:\n{command.text('stdout')}\nstderr:\n{command.text()}"
        )


def _wait_for_watch_path(
    command: ManagedCommand,
    project: VfyProject,
    relative: str,
    *,
    timeout: float = 30.0,
) -> Path:
    """Wait for a child marker and retain the watch output if it is missing."""

    try:
        return project.wait_for_path(relative, timeout)
    except TimeoutError as error:
        raise TimeoutError(
            f"{error}\nwatch stdout:\n{command.text('stdout')}\nwatch stderr:\n{command.text()}"
        ) from error


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
def test_watch_publishes_tmp_path_results_with_bare_cov_before_basetemp(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_case.py",
        'def test_case(tmp_path):\n    (tmp_path / "generated.txt").write_text("generated")\n',
    )
    command = project.start_vfy(
        "test",
        "--engine=native",
        "--watch",
        "--cov",
        "--basetemp",
        "generated",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": None},
    )
    try:
        _wait_for_watch_event(command, after_run=0, expected="pytest exited 0", timeout=10)
        assert (tmp_path / "generated/test_case0/generated.txt").read_text() == "generated"
        data = CoverageData(basename=str(tmp_path / ".coverage"))
        data.read()
        assert data.lines(str(tmp_path / "test_case.py")) == [1, 2]
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    assert result.returncode == 2


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
def test_watch_resolves_coverage_options_again_after_pytest_config_changes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "VALUE = 1\n")
    project.write(
        "test_app.py", "from app import VALUE\ndef test_value():\n    assert VALUE == 1\n"
    )
    project.write("pytest.ini", "[pytest]\naddopts = --no-cov\n")
    command = project.start_vfy(
        "test",
        "--engine=native",
        "--watch",
        "-j1",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    try:
        accepted_run = _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        assert not (tmp_path / ".coverage").exists()
        project.write("pytest.ini", "[pytest]\naddopts = --cov-report=json:coverage.json\n")
        _wait_for_watch_event(command, after_run=accepted_run, expected="pytest exited 0")
        report = json.loads((tmp_path / "coverage.json").read_text(encoding="utf-8"))
        assert report["files"]["app.py"]["executed_lines"] == [1]
        assert (tmp_path / ".coverage").is_file()
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    assert result.returncode == 2


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
@pytest.mark.parametrize("primed", [False, True], ids=["watch-only", "after-earlier-invocation"])
def test_watch_publishes_the_current_installed_dependency_identity(
    tmp_path: Path, primed: bool
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    project = VfyProject(root)
    dependencies = tmp_path / "dependencies"
    metadata = dependencies / "example-1.0.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Name: example\nVersion: 1.0\n", encoding="utf-8")
    project.write("app.py", "def left():\n    return 1\n")
    project.write("other.py", "def right():\n    return 2\n")
    project.write(
        "test_left.py",
        "from app import left\ndef test_left():\n"
        "    print(f'LEFT={left()}')\n    assert left() > 0\n",
    )
    project.write(
        "test_right.py",
        "from other import right\ndef test_right():\n    assert right() == 2\n",
    )
    if primed:
        project.write(
            "prime_watch.py",
            "import sys\n"
            "from vfy.entry import main\n"
            "sys.argv = ['vfy', 'test', '--engine=pytest', '-j1', '-q', '-s']\n"
            "if main() != 0: raise SystemExit('initial test invocation failed')\n"
            "sys.argv = ['vfy', 'test', '--engine=pytest', '--watch', '-j1', '-q', '-s']\n"
            "raise SystemExit(main())\n",
        )
    project.git_init()
    environment = {"PYTHONPATH": str(dependencies), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    command = (
        project.start_python("prime_watch.py", env=environment)
        if primed
        else project.start_vfy(
            "test", "--engine=pytest", "--watch", "-j1", "-q", "-s", env=environment
        )
    )
    try:
        accepted_run = _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        metadata.write_text("Name: example\nVersion: 2.0\n", encoding="utf-8")
        project.write("app.py", "def left():\n    return 3\n")
        # Only a clean full run can replace the saved passing baseline.
        assert project.run("git", "add", "app.py").returncode == 0
        assert project.run("git", "commit", "-qm", "Change app").returncode == 0
        command.wait_for("LEFT=3", stream="stdout", timeout=30)
        _wait_for_watch_event(command, after_run=accepted_run, expected="pytest exited 0")
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    assert result.returncode == 2
    project.write("app.py", "def left():\n    return 5\n")
    command = project.start_vfy(
        "test", "--engine=pytest", "--watch", "-j1", "-q", "-s", env=environment
    )
    try:
        _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        assert "Selected 1 / 2 tests" in command.text(), command.text()
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    assert result.returncode == 2


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
def test_watch_queues_an_edit_and_uses_fresh_pytest_processes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "VALUE = 1\n")
    project.write(
        "tests/test_slow.py",
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        "from app import VALUE\n\n"
        "def test_slow():\n"
        "    Path('.vfy').mkdir(exist_ok=True)\n"
        "    with Path('.vfy/pids').open('a', encoding='utf-8') as stream:\n"
        "        stream.write(f'{os.getpid()}:{VALUE}\\n')\n"
        "    Path('.vfy/started').write_text('ready', encoding='utf-8')\n"
        "    while not Path('.vfy/release').exists():\n"
        "        time.sleep(0.01)\n",
    )
    command = project.start_vfy("test", "--engine=pytest", "--watch", "-j1", "-q")
    result = None
    try:
        command.wait_for("Watch run 1: starting")
        project.wait_for_path(".vfy/started")
        project.write("app.py", "VALUE = 2\n")
        project.write(".vfy/release", "ready")
        obsolete_run = _wait_for_watch_event(
            command, after_run=0, expected="obsolete; queued changes"
        )
        _wait_for_watch_event(command, after_run=obsolete_run, expected="pytest exited 0")
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    finally:
        if command.poll() is None:
            command.send_signal(signal.SIGINT)
            command.wait(timeout=10)

    assert result is not None
    assert result.returncode == 2
    records = (tmp_path / ".vfy/pids").read_text(encoding="utf-8").splitlines()
    assert len(records) >= 2
    assert len({record.split(":", 1)[0] for record in records}) >= 2
    assert records[-1].endswith(":2")


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
@pytest.mark.parametrize("workers", [1, 2])
def test_obsolete_failure_preserves_accepted_evidence_until_full_recovery(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "VALUE = 1\n")
    project.write(".vfy/release-1", "ready")
    project.write(
        "tests/test_app.py",
        "from pathlib import Path\n"
        "import time\n"
        "from app import VALUE\n\n"
        "def test_value():\n"
        "    Path(f'.vfy/started-{VALUE}').write_text('ready')\n"
        "    while not Path(f'.vfy/release-{VALUE}').exists():\n"
        "        time.sleep(0.01)\n"
        "    assert VALUE != 2, 'PRIMARY FAILURE VALUE=2'\n",
    )
    command = project.start_vfy("--verbose", "test", "--engine=pytest", "--watch", f"-j{workers}")
    try:
        accepted_run = _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        accepted = {
            name: (tmp_path / name).read_bytes()
            for name in (
                ".coverage",
                ".vfy/test-history.sqlite3",
                ".vfy/replay.coverage",
                ".vfy/test-run.json",
            )
        }
        project.write("app.py", "VALUE = 2\n")
        # The marker proves the child imported the edited source even if the
        # filesystem backend missed its notification and idle reconciliation
        # started the run without a reported path.
        _wait_for_watch_path(command, project, ".vfy/started-2")
        project.write("app.py", "VALUE = 3\n")
        project.write(".vfy/release-2", "ready")
        obsolete_run = _wait_for_watch_event(
            command, after_run=accepted_run, expected="obsolete; queued changes"
        )
        _wait_for_watch_path(command, project, ".vfy/started-3")
        assert {name: (tmp_path / name).read_bytes() for name in accepted} == accepted
        assert "PRIMARY FAILURE VALUE=2" in command.text("stdout")
        project.write(".vfy/release-3", "ready")
        _wait_for_watch_event(command, after_run=obsolete_run, expected="pytest exited 0")
        assert "watch recovery requires a full run" in command.text()

        data = CoverageData(basename=str(tmp_path / ".coverage"))
        data.read()
        assert data.lines(str(tmp_path / "app.py")) == [1]
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    assert result.returncode == 2


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
@pytest.mark.parametrize("workers", [1, 2])
def test_native_watch_discards_obsolete_evidence_and_publishes_recovery(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)

    def write_app(value: int) -> None:
        project.write(
            "app.py",
            f"VALUE = {value}\n\n"
            "def value():\n    return VALUE\n\n"
            "def recovered():\n    return 'recovered'\n",
        )

    project.write(
        "conftest.py",
        "import _pytest.runner\n"
        "def _forbid_stock_protocol(*args, **kwargs):\n"
        "    raise AssertionError('stock pytest runtestprotocol was called')\n"
        "_pytest.runner.runtestprotocol = _forbid_stock_protocol\n",
    )
    write_app(1)
    project.write("tests/test_other.py", "def test_other():\n    assert True\n")
    project.write(".vfy/release-1", "ready")
    project.write(
        "tests/test_watch_app.py",
        "import time\n"
        "from pathlib import Path\n"
        "from app import VALUE, recovered, value\n\n"
        "def test_value():\n"
        "    Path(f'.vfy/started-{VALUE}').write_text('ready', encoding='utf-8')\n"
        "    while not Path(f'.vfy/release-{VALUE}').exists():\n"
        "        time.sleep(0.01)\n"
        "    assert value() == VALUE\n"
        "    if VALUE == 3:\n        assert recovered() == 'recovered'\n"
        "    assert VALUE != 2, 'obsolete native run must fail before recovery'\n",
    )
    command = project.start_vfy(
        "--verbose",
        "test",
        "--engine=pytest-native",
        "--watch",
        f"-j{workers}",
        "--cov=app",
        "--cov-report=json:coverage.json",
    )
    try:
        accepted_run = _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        accepted_names = (
            ".coverage",
            ".vfy/test-history.sqlite3",
            ".vfy/replay.coverage",
            ".vfy/test-run.json",
            "coverage.json",
        )
        accepted = {name: (tmp_path / name).read_bytes() for name in accepted_names}
        original_report = json.loads(accepted["coverage.json"])
        original_file = next(
            report
            for path, report in original_report["files"].items()
            if Path(path).name == "app.py"
        )
        assert 7 in original_file["missing_lines"]

        write_app(2)
        _wait_for_watch_path(command, project, ".vfy/started-2")
        write_app(3)
        project.write(".vfy/release-2", "ready")
        obsolete_run = _wait_for_watch_event(
            command, after_run=accepted_run, expected="obsolete; queued changes"
        )
        _wait_for_watch_path(command, project, ".vfy/started-3")
        assert {name: (tmp_path / name).read_bytes() for name in accepted_names} == accepted
        assert "obsolete native run must fail before recovery" in command.text("stdout")

        project.write(".vfy/release-3", "ready")
        _wait_for_watch_event(command, after_run=obsolete_run, expected="pytest exited 0")

        data = CoverageData(basename=str(tmp_path / ".coverage"))
        data.read()
        assert data.lines(str(tmp_path / "app.py")) == [1, 3, 4, 6, 7]
        recovered_report = json.loads((tmp_path / "coverage.json").read_text(encoding="utf-8"))
        recovered_file = next(
            report
            for path, report in recovered_report["files"].items()
            if Path(path).name == "app.py"
        )
        assert 7 in recovered_file["executed_lines"]
        assert (tmp_path / "coverage.json").read_bytes() != accepted["coverage.json"]
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    assert result.returncode == 2


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
@pytest.mark.parametrize("report_source", ["cli", "argument-file"])
def test_obsolete_success_preserves_replayed_reports_until_recovery(
    tmp_path: Path, report_source: str
) -> None:
    project = VfyProject(tmp_path)

    def write_app(value: int) -> None:
        project.write(
            "app.py",
            f"VALUE = {value}\n\ndef value():\n"
            "    if VALUE == 2:\n        return 2\n    return VALUE\n",
        )

    write_app(1)
    project.write("other.py", "def other(): return 1\n")
    project.write(
        "test_other.py", "from other import other\ndef test_other(): assert other() == 1\n"
    )
    project.write(
        "test_app.py",
        "import time\nfrom pathlib import Path\nfrom app import VALUE, value\n"
        "def test_value():\n"
        "    Path(f'.vfy/started-{VALUE}').write_text('ready')\n"
        "    while not Path(f'.vfy/release-{VALUE}').exists():\n"
        "        time.sleep(0.01)\n"
        "    assert value() == VALUE\n",
    )
    project.write(".vfy/release-1", "ready")
    report_arguments = ["--cov-report=json:coverage.json"]
    if report_source == "argument-file":
        project.write("args.txt", "--cov-report=json:coverage.json\n")
        report_arguments = ["@args.txt"]
    project.git_init()
    arguments = ["--engine=pytest", "-j1", "--cov=app", *report_arguments]
    project.run_vfy("test", "--all", *arguments).expect(0)
    accepted_names = (".coverage", ".vfy/test-history.sqlite3", "coverage.json")
    accepted = {name: (tmp_path / name).read_bytes() for name in accepted_names}
    # Start watch with an edit already present so the first run exercises replay
    # independently of how the filesystem coalesces the fixture's creation events.
    write_app(2)
    command = project.start_vfy("test", "--watch", *arguments)
    try:
        _wait_for_watch_path(command, project, ".vfy/started-2")
        write_app(3)
        project.write(".vfy/release-2", "ready")
        obsolete_run = _wait_for_watch_event(
            command, after_run=0, expected="obsolete; queued changes"
        )
        _wait_for_watch_path(command, project, ".vfy/started-3")
        assert "Selected 1 / 2 tests" in command.text(), command.text()
        assert {name: (tmp_path / name).read_bytes() for name in accepted_names} == accepted
        project.write(".vfy/release-3", "ready")
        _wait_for_watch_event(command, after_run=obsolete_run, expected="pytest exited 0")
        assert (tmp_path / "coverage.json").read_bytes() != accepted["coverage.json"]
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    assert result.returncode == 2


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
def test_watch_stages_addopts_reports_and_observes_coveragerc(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    project = VfyProject(root)
    project.write("app.py", "VALUE = 1\n")
    project.write(
        "test_app.py", "from app import VALUE\ndef test_value():\n    assert VALUE == 1\n"
    )
    project.write(".coveragerc", "[run]\nbranch = false\n")
    project.write(
        "pytest.ini",
        "[pytest]\naddopts = --cov-report=json:../coverage.json --junitxml=../junit.xml\n"
        "cache_dir = custom-cache\n",
    )
    command = project.start_vfy("test", "--engine=pytest", "--watch", "-j1")
    try:
        successful_run = _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        assert (tmp_path / "coverage.json").is_file()
        assert (tmp_path / "junit.xml").is_file()
        project.write(".coveragerc", "[run]\nbranch = true\n")
        successful_run = _wait_for_watch_event(
            command, after_run=successful_run, expected="pytest exited 0"
        )

        data = CoverageData(basename=str(root / ".coverage"))
        data.read()
        assert data.has_arcs()
        accepted = (root / ".coverage").read_bytes()
        project.write(".coveragerc", "[run\nbranch = true\n")
        error_run = _wait_for_watch_event(
            command, after_run=successful_run, expected="configuration error:"
        )
        assert (root / ".coverage").read_bytes() == accepted
        project.write(".coveragerc", "[run]\nbranch = true\n")
        _wait_for_watch_event(command, after_run=error_run, expected="pytest exited 0")
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    assert result.returncode == 2


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
def test_watch_reuses_baseline_with_staged_report_destinations(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def left():\n    return 1\n")
    project.write("other.py", "def right():\n    return 2\n")
    project.write("test_left.py", "from app import left\ndef test_left():\n    assert left() > 0\n")
    project.write(
        "test_right.py", "from other import right\ndef test_right():\n    assert right() > 0\n"
    )
    project.git_init()
    command = project.start_vfy(
        "test", "--engine=pytest", "--watch", "-j1", "--cov-report=json:coverage.json"
    )
    try:
        _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        assert (tmp_path / "coverage.json").is_file()
        command.send_signal(signal.SIGINT)
        assert command.wait(timeout=10).returncode == 2
        project.write("app.py", "def left():\n    return 3\n")
        # Restarting watch also changes its staging directory. Its persisted
        # baseline must retain the identity of the user's report arguments.
        command = project.start_vfy(
            "test",
            "--engine=pytest",
            "--watch",
            "-j1",
            "--cov-report=json:coverage.json",
        )
        _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        assert (tmp_path / "coverage.json").is_file()
        assert "Selected 1 / 2 tests" in command.text(), command.text()
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    assert result.returncode == 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup contract")
@pytest.mark.parametrize("workers", [1, 2])
def test_ctrl_c_reaps_active_pytest_and_discards_unfinished_evidence(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_blocked.py",
        "import os\nimport time\nfrom pathlib import Path\n"
        "def test_blocked():\n"
        "    Path('.vfy').mkdir(exist_ok=True)\n"
        "    Path('.vfy/child-pid').write_text(str(os.getpid()))\n"
        "    while not Path('.vfy/release').exists():\n"
        "        time.sleep(0.01)\n",
    )
    command = project.start_vfy("test", "--engine=pytest", "--watch", f"-j{workers}")
    try:
        project.wait_for_path(".vfy/child-pid")
        child = int((tmp_path / ".vfy/child-pid").read_text())
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
        assert result.returncode == 2
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
        assert not (tmp_path / ".coverage").exists()
        assert not (tmp_path / ".vfy/test-history.sqlite3").exists()
    finally:
        if command.poll() is None:
            command.send_signal(signal.SIGINT)
            command.wait(timeout=10)


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
def test_threshold_failure_requires_full_recovery_without_confirmation(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "VALUE = 1\ndef missed():\n    return 5\n")
    project.write(
        "test_app.py", "from app import VALUE\ndef test_value():\n    assert VALUE == 1\n"
    )
    command = project.start_vfy(
        "test",
        "--engine=pytest",
        "--watch",
        "-j1",
        "--confirm-failures",
        "--cov=app",
        "--cov-fail-under=100",
    )
    try:
        failed_run = _wait_for_watch_event(command, after_run=0, expected="pytest exited 1")
        assert not (tmp_path / ".vfy/replay.coverage").exists()
        assert not (tmp_path / ".vfy/mutation-baseline.json").exists()
        project.write("app.py", "VALUE = 1\n")
        _wait_for_watch_event(command, after_run=failed_run, expected="pytest exited 0")
        assert "watch recovery requires a full run" in command.text()
        assert "Confirming" not in command.text()
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    assert result.returncode == 2


@pytest.mark.parametrize("policy", ["balanced", "aggressive"])
def test_all_watch_rejects_unsafe_configured_policy_before_running(
    tmp_path: Path, policy: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pyproject.toml", f'[tool.vfy.test]\nselection = "{policy}"\n')
    command = project.start_vfy("test", "--engine=pytest", "--all", "--watch", "-j1")
    try:
        result = command.wait(timeout=10)
        assert result.returncode == 1
        assert "--watch supports only the safe selection policy" in result.stderr
        assert "Watch run" not in result.stderr
        assert not (tmp_path / ".vfy").exists()
    finally:
        if command.poll() is None:
            command.send_signal(signal.SIGINT)
            command.wait(timeout=10)


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
def test_publication_error_cleans_staging_and_waits_for_a_correcting_save(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "VALUE = 1\n")
    project.write(
        "test_app.py", "from app import VALUE\ndef test_value():\n    assert VALUE == 1\n"
    )
    project.write("reports", "blocked output directory")
    project.write(".vfy/staging/.keep", "")
    staging = tmp_path / ".vfy/staging"
    command = project.start_vfy(
        "test",
        "--engine=pytest",
        "--watch",
        "-j1",
        "--cov-report=json:reports/coverage.json",
        env={"TMPDIR": str(staging)},
    )
    try:
        error_run = _wait_for_watch_event(command, after_run=0, expected="publication error:")
        assert not (tmp_path / ".coverage").exists()
        assert not (tmp_path / ".vfy/test-history.sqlite3").exists()
        assert not list(staging.glob("vfy-watch-run-*"))
        (tmp_path / "reports").unlink()
        (tmp_path / "reports").mkdir()
        _wait_for_watch_event(command, after_run=error_run, expected="pytest exited 0")
        assert (tmp_path / "reports/coverage.json").is_file()
        assert not list(staging.glob("vfy-watch-run-*"))
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    assert result.returncode == 2


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
def test_watch_recovers_from_invalid_initial_config_with_default_workers(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pyproject.toml", '[tool.vfy.test]\nworkers = "0"\n')
    project.write("test_app.py", "def test_ok():\n    assert True\n")
    command = project.start_vfy("--verbose", "test", "--engine=pytest", "--watch")
    try:
        error_run = _wait_for_watch_event(command, after_run=0, expected="configuration error:")
        assert not (tmp_path / ".coverage").exists()
        project.write("pyproject.toml", '[tool.vfy.test]\nworkers = "1"\n')
        _wait_for_watch_event(command, after_run=error_run, expected="pytest exited 0")
        assert (tmp_path / ".coverage").is_file()
        assert (tmp_path / ".vfy/test-history.sqlite3").is_file()
    finally:
        if command.poll() is None:
            command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    assert result.returncode == 2


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
@pytest.mark.parametrize("engine", ["pytest-native", "pytest"])
def test_watch_recovers_from_malformed_initial_pytest_config(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest\naddopts = -q\n")
    project.write("test_app.py", "def test_ok():\n    assert True\n")
    command = project.start_vfy("--verbose", "test", f"--engine={engine}", "--workers=1", "--watch")
    try:
        error_run = _wait_for_watch_event(command, after_run=0, expected="configuration error:")
        assert not (tmp_path / ".coverage").exists()
        assert not (tmp_path / ".vfy/test-history.sqlite3").exists()
        project.write("pytest.ini", "[pytest]\naddopts = -q --junitxml=reports/tests.xml\n")
        _wait_for_watch_event(command, after_run=error_run, expected="pytest exited 0")
        assert (tmp_path / ".coverage").is_file()
        assert (tmp_path / ".vfy/test-history.sqlite3").is_file()
        assert (tmp_path / "reports/tests.xml").is_file()
    finally:
        if command.poll() is None:
            command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    assert result.returncode == 2


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
def test_watch_reports_an_invalid_config_once_until_the_next_change(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pyproject.toml", "[tool.coverage.paths]\nbogus = 1\n")
    project.write("test_app.py", "def test_ok():\n    assert True\n")
    command = project.start_vfy("test", "--engine=native", "--watch", "-j1")
    try:
        error_run = _wait_for_watch_event(command, after_run=0, expected="configuration error:")
        # An idle watcher must stay quiet; a busy loop would start many more runs.
        quiet_until = time.monotonic() + 3.0
        while time.monotonic() < quiet_until and command.poll() is None:
            time.sleep(0.1)
        assert command.poll() is None
        assert command.text().count("Watch run") == 2, command.text()
        assert "Watch fallback" not in command.text()
        project.write("pyproject.toml", "[tool.coverage.run]\nbranch = false\n")
        _wait_for_watch_event(command, after_run=error_run, expected="pytest exited 0")
    finally:
        if command.poll() is None:
            command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    assert result.returncode == 2
