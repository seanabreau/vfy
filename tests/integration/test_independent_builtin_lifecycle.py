"""Compare built-in pytest lifecycle services through real project runs."""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from importlib.metadata import version
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
@pytest.mark.parametrize("warning_action", ["error", "default"])
def test_thread_exceptions_are_processed_before_phase_reports(
    tmp_path: Path, phase: str, warning_action: str
) -> None:
    observed: dict[str, list[tuple[str | None, list[str]]]] = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_case.py",
            f"""import threading
import pytest

def explode():
    raise ValueError("thread failure sentinel")

def fail_thread():
    thread = threading.Thread(target=explode, name="phase-thread")
    thread.start()
    thread.join()

@pytest.fixture
def resource():
    {"fail_thread()" if phase == "setup" else "pass"}
    yield
    {"fail_thread()" if phase == "teardown" else "pass"}

def test_case(resource):
    {"fail_thread()" if phase == "call" else "pass"}
""",
        )
        arguments = (
            "-q",
            "--junitxml=result.xml",
            "-W",
            f"{warning_action}::pytest.PytestUnhandledThreadExceptionWarning",
        )
        result = (
            project.run_pytest(*arguments, load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=1",
                "--no-cov",
                f"--native-result-json={project.root / 'result.json'}",
                *arguments,
            )
        )
        assert result.returncode == (1 if warning_action == "error" else 0), (
            result.stdout + result.stderr
        )
        observed[engine] = [
            (case.get("name"), [child.tag for child in case if child.tag in {"error", "failure"}])
            for case in ET.parse(project.root / "result.xml").iter("testcase")
        ]
        assert "thread failure sentinel" in result.stdout + result.stderr
        if engine == "native" and warning_action == "error":
            record = json.loads((project.root / "result.json").read_text())
            failed_phase = next(
                report for report in record["cases"][0]["phases"] if report["phase"] == phase
            )
            assert failed_phase["outcome"] == ("failed" if phase == "call" else "error")
    assert observed["native"] == observed["pytest"]


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_faulthandler_timeout_covers_each_test_phase(tmp_path: Path, phase: str) -> None:
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nfaulthandler_timeout = 0.05\n")
        project.write(
            "conftest.py",
            """import faulthandler
from pathlib import Path

def pytest_configure():
    original_dump = faulthandler.dump_traceback_later
    original_cancel = faulthandler.cancel_dump_traceback_later

    def dump(*args, **kwargs):
        original_dump(*args, **kwargs)
        with Path("timer-events").open("a") as events:
            events.write("scheduled\\n")

    def cancel():
        with Path("timer-debug").open("a") as events:
            events.write("cancel entered\\n")
        original_cancel()
        with Path("timer-debug").open("a") as events:
            events.write("cancel returned\\n")
        with Path("timer-events").open("a") as events:
            events.write("cancelled\\n")

    faulthandler.dump_traceback_later = dump
    faulthandler.cancel_dump_traceback_later = cancel
""",
        )
        project.write(
            "test_case.py",
            f"""from pathlib import Path
import pytest

def wait_for_timeout():
    with Path("timer-events").open("a") as events:
        events.write("phase\\n")
    with Path("timer-debug").open("a") as events:
        events.write("phase entered\\n")
    with Path("release").open("rb", buffering=0) as gate:
        assert gate.read(1) == b"1"
    with Path("timer-debug").open("a") as events:
        events.write("phase returned\\n")

@pytest.fixture
def resource():
    {"wait_for_timeout()" if phase == "setup" else "pass"}
    yield
    {"wait_for_timeout()" if phase == "teardown" else "pass"}

def test_case(resource):
    {"wait_for_timeout()" if phase == "call" else "pass"}
""",
        )
        release = project.root / "release"
        os.mkfifo(release)
        command = (
            project.start_python("-m", "pytest", "-q")
            if engine == "pytest"
            else project.start_vfy("test", "--engine=native", "--workers=1", "--no-cov", "-q")
        )
        try:
            try:
                with release.open("r+b", buffering=0) as gate:
                    command.wait_for("Timeout (0:00:00.050000)!", timeout=10)
                    # Hold the child in read() until the watchdog prints the entry frame.
                    entry_frame = (
                        "in _run_module_as_main"
                        if engine == "pytest"
                        else 'File "<string>", line 1 in <module>'
                    )
                    command.wait_for(entry_frame, timeout=10)
                    gate.write(b"1")
                    result = command.wait(timeout=project.timeout)
            except (AssertionError, TimeoutError) as error:
                debug = project.root / "timer-debug"
                events = debug.read_text() if debug.exists() else "no debug events"
                raise AssertionError(f"{engine} {phase}: {events}") from error
        finally:
            if command.poll() is None:
                command.kill()
                command.wait(timeout=10)
        assert result.returncode == 0, f"{engine} {phase}: {result.stdout}{result.stderr}"
        assert "Timeout (0:00:00.050000)!" in result.stderr, f"{engine} {phase}: {result.stderr}"
        # The protocol timer can fire before the selected phase under load.
        assert (project.root / "timer-events").read_text().splitlines() == [
            "scheduled",
            "phase",
            "cancelled",
        ], f"{engine} {phase}"


@pytest.mark.skipif(version("pytest") != "9.1.1", reason="fatal timeouts are a pytest 9.1 option")
def test_fatal_faulthandler_timeout_exits_before_test_completion(tmp_path: Path) -> None:
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "pytest.ini",
            "[pytest]\nfaulthandler_timeout = 0.05\nfaulthandler_exit_on_timeout = true\n",
        )
        project.write(
            "test_case.py",
            """import time
from pathlib import Path

def test_case():
    time.sleep(5)
    Path("completed").touch()
""",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--workers=1", "--no-cov", "-q")
        )
        assert result.returncode == 1, f"{engine}: {result.stdout}{result.stderr}"
        assert "Timeout (0:00:00.050000)!" in result.stderr, f"{engine}: {result.stderr}"
        assert not (project.root / "completed").exists(), engine


def test_disabled_capture_runs_without_a_capture_manager(tmp_path: Path) -> None:
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", 'def test_case():\n    print("uncaptured sentinel")\n')
        result = (
            project.run_pytest("-q", "-p", "no:capture", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=native", "--workers=1", "--no-cov", "-q", "-p", "no:capture"
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "uncaptured sentinel" in result.stdout
