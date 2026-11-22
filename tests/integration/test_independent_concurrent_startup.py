"""Concurrent independent workers retain ownership through collection and shutdown."""

from __future__ import annotations

import json
import os
import select
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import ManagedCommand

pytestmark = [
    requires_native_pytest,
    pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="worker exit proof needs pidfds"),
]


@contextmanager
def _run_with_workers(
    project: VfyProject, *arguments: str
) -> Iterator[tuple[ManagedCommand, list[int]]]:
    if sys.platform != "linux":
        pytest.skip("process-exit observation requires Linux pidfds")
    command = project.start_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=3",
        "-q",
        *arguments,
        env={"VFY_WORKER_START": "exec"},
    )
    handles: list[int] = []
    try:
        for index in range(3):
            path = project.wait_for_path(f"worker-{index}.pid", timeout=15)
            handles.append(os.pidfd_open(int(path.read_text())))
        (project.root / "release-collection").touch()
        yield command, handles
    finally:
        # Each worker has its own process group. Clean up even if an assertion fails.
        for path in project.root.glob("worker-*.pid"):
            with suppress(ProcessLookupError):
                os.killpg(int(path.read_text()), signal.SIGKILL)
        command.kill()
        with suppress(Exception):
            command.wait(timeout=5)
        for handle in handles:
            os.close(handle)


def _assert_exited(handles: list[int]) -> None:
    poller = select.poll()
    for handle in handles:
        poller.register(handle, select.POLLIN)
    pending = set(handles)
    deadline = time.monotonic() + 15
    while pending and time.monotonic() < deadline:
        ready = {fd for fd, _ in poller.poll(max(0, int((deadline - time.monotonic()) * 1000)))}
        for fd in ready:
            poller.unregister(fd)
        pending -= ready
    assert not pending, f"workers still alive: {pending}"


def _write_project(project: VfyProject, scenario: str, *, empty: bool = False) -> None:
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        f"""import os, sys, time, warnings
from pathlib import Path
import pytest

INDEX = os.environ.get("VFY_NATIVE_PARALLEL_INDEX")
SCENARIO = {scenario!r}
original_interrupt = None

def pytest_sessionstart(session):
    global original_interrupt
    if INDEX is None:
        if SCENARIO == "interrupt":
            original_interrupt = KeyboardInterrupt("surplus shutdown interrupt")
            def interrupt_parent(signum, frame):
                raise original_interrupt
            import signal
            signal.signal(signal.SIGINT, interrupt_parent)
            previous_stdout = sys.stdout
            class ReplayStream:
                def __getattr__(self, name):
                    return getattr(previous_stdout, name)
                def write(self, text):
                    result = previous_stdout.write(text)
                    if "ACTIVE-REPLAY-MARKER" in text:
                        Path("active-replayed").touch()
                    return result
            sys.stdout = ReplayStream()
        return
    pid_tmp = Path(f"worker-{{INDEX}}.tmp")
    pid_tmp.write_text(str(os.getpid()))
    pid_tmp.replace(f"worker-{{INDEX}}.pid")

def pytest_collection_modifyitems(session, config, items):
    if INDEX is None:
        return
    deadline = time.monotonic() + 15
    while not Path("release-collection").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("workers did not start concurrently")
        time.sleep(0.01)
    if INDEX == "0" and SCENARIO == "marker":
        (Path(os.environ["VFY_NATIVE_PARALLEL_DIR"]) / "surplus.1").mkdir()
        Path("marker-injected").touch()
    if INDEX == "1" and SCENARIO == "exit":
        Path("failure-injected").touch()
        pytest.exit("surplus collection exit", returncode=7)

def pytest_collection_finish(session):
    if SCENARIO == "evidence" and INDEX is not None:
        print(f"WORKER-COLLECTION:{{INDEX}}")
        print(f"WORKER-COLLECTION:{{INDEX}}", file=sys.stderr)

def pytest_terminal_summary(terminalreporter):
    if SCENARIO == "evidence" and INDEX is not None:
        terminalreporter.write_line(f"WORKER-SUMMARY:{{INDEX}}")
        print(f"WORKER-SUMMARY:{{INDEX}}", file=sys.stderr)

def pytest_sessionfinish(session, exitstatus):
    if SCENARIO == "evidence" and INDEX == "1":
        warnings.warn("surplus sessionfinish warning", UserWarning)
    if INDEX == "1" and SCENARIO == "status":
        Path("failure-injected").touch()
        session.exitstatus = 5
    if INDEX == "1" and SCENARIO == "finish":
        Path("failure-injected").touch()
        raise RuntimeError("surplus sessionfinish failure")
    if INDEX == "1" and SCENARIO in ("interrupt", "wait"):
        Path("shutdown-blocked").touch()
        while not Path("release-shutdown").exists():
            time.sleep(0.05)
    if INDEX is not None:
        Path(f"finish-{{INDEX}}").touch()

def pytest_unconfigure(config):
    if SCENARIO == "interrupt" and INDEX == "0":
        print("ACTIVE-REPLAY-MARKER", flush=True)
    if SCENARIO == "evidence" and INDEX is not None:
        print(f"WORKER-UNCONFIGURE:{{INDEX}}")
        print(f"WORKER-UNCONFIGURE:{{INDEX}}", file=sys.stderr)
        if INDEX == "0":
            print("WORKER-TAIL:0", end="", flush=True)
            print("WORKER-TAIL:0", end="", file=sys.stderr, flush=True)
    if INDEX == "1" and SCENARIO == "unconfigure":
        Path("failure-injected").touch()
        raise RuntimeError("surplus unconfigure failure")
    if INDEX is not None:
        Path(f"unconfigure-{{INDEX}}").touch()

def pytest_keyboard_interrupt(excinfo):
    if SCENARIO == "interrupt" and INDEX is None:
        assert excinfo.value is original_interrupt
        Path("original-interrupt").touch()
""",
    )
    if not empty:
        project.write(
            "test_single.py",
            f"""import os
from pathlib import Path
if os.environ.get("VFY_NATIVE_PARALLEL_INDEX") == "1" and {scenario!r} == "collection":
    Path("failure-injected").touch()
    raise RuntimeError("surplus collection failure")
def test_single():
    Path("test-complete").touch()
""",
        )


def test_workers_collect_concurrently_and_finish_empty_suite(tmp_path: Path) -> None:
    project = VfyProject(tmp_path, timeout=30)
    _write_project(project, "empty", empty=True)
    with _run_with_workers(project) as (command, handles):
        result = command.wait(timeout=30)
        result.expect(5)
        assert all(
            (tmp_path / f"{hook}-{i}").exists()
            for hook in ("finish", "unconfigure")
            for i in range(3)
        )
        _assert_exited(handles)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [("collection", 3), ("exit", 7), ("finish", 3), ("status", 3), ("unconfigure", 3)],
)
def test_surplus_worker_failure_cannot_report_success(
    tmp_path: Path, scenario: str, expected: int
) -> None:
    project = VfyProject(tmp_path, timeout=30)
    _write_project(project, scenario)
    with _run_with_workers(project) as (command, handles):
        result = command.wait(timeout=30)
        assert (tmp_path / "failure-injected").exists(), (result.stdout, result.stderr)
        assert result.returncode == expected, (result.stdout, result.stderr)
        if scenario == "collection":
            assert "surplus collection failure" in result.stdout + result.stderr
        _assert_exited(handles)


def test_surplus_marker_write_failure_reaps_all_workers(tmp_path: Path) -> None:
    project = VfyProject(tmp_path, timeout=30)
    _write_project(project, "marker")
    with _run_with_workers(project) as (command, handles):
        result = command.wait(timeout=30)
        assert (tmp_path / "marker-injected").exists(), (result.stdout, result.stderr)
        assert result.returncode != 0, (result.stdout, result.stderr)
        _assert_exited(handles)


def test_interrupt_during_surplus_shutdown_reaps_workers(tmp_path: Path) -> None:
    project = VfyProject(tmp_path, timeout=30)
    _write_project(project, "interrupt")
    with _run_with_workers(project, "-s", "--native-result-json=result.json") as (command, handles):
        project.wait_for_path("shutdown-blocked", timeout=15)
        project.wait_for_path("active-replayed", timeout=15)
        # No fixture-visible signal marks the controller's next poll of the blocked surplus
        # worker, which is the state the interrupt must land in; the marker above only proves
        # the active replay has been written.
        time.sleep(0.2)
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=30)
        assert result.returncode in (2, 130, -signal.SIGINT), (result.stdout, result.stderr)
        assert (tmp_path / "original-interrupt").exists(), (result.stdout, result.stderr)
        record = json.loads((tmp_path / "result.json").read_text())
        assert record["exitstatus"] == 2
        assert [case["nodeid"] for case in record["cases"]] == ["test_single.py::test_single"]
        _assert_exited(handles)


def test_surplus_shutdown_waits_for_hook_completion(tmp_path: Path) -> None:
    project = VfyProject(tmp_path, timeout=30)
    _write_project(project, "wait")
    with _run_with_workers(project) as (command, handles):
        project.wait_for_path("shutdown-blocked", timeout=15)
        project.wait_for_path("test-complete", timeout=15)
        deadline = time.monotonic() + 11
        while time.monotonic() < deadline:
            assert command.poll() is None
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        assert command.poll() is None
        (tmp_path / "release-shutdown").touch()
        result = command.wait(timeout=30)
        result.expect(0)
        assert (tmp_path / "finish-1").exists()
        assert (tmp_path / "unconfigure-1").exists()
        _assert_exited(handles)


@pytest.mark.parametrize("scenario", ["finish", "unconfigure"])
def test_collect_only_reports_surplus_shutdown_failure(tmp_path: Path, scenario: str) -> None:
    project = VfyProject(tmp_path, timeout=30)
    _write_project(project, scenario)
    with _run_with_workers(project, "--collect-only") as (command, handles):
        result = command.wait(timeout=30)
        assert (tmp_path / "failure-injected").exists(), (result.stdout, result.stderr)
        result.expect(3)
        _assert_exited(handles)


@pytest.mark.parametrize("collect_only", [False, True])
def test_surplus_worker_output_and_warnings_are_preserved(
    tmp_path: Path, collect_only: bool
) -> None:
    project = VfyProject(tmp_path, timeout=30)
    _write_project(project, "evidence")
    arguments = ["-s", "--native-result-json=result.json"]
    if collect_only:
        arguments.append("--collect-only")
    with _run_with_workers(project, *arguments) as (command, handles):
        result = command.wait(timeout=30)
        _assert_exited(handles)
        diagnostic = result.stdout + result.stderr
        assert result.returncode == 0, diagnostic
        record = json.loads((tmp_path / "result.json").read_text())
        observed = {
            (stream, phase, index): output.splitlines().count(f"WORKER-{phase}:{index}")
            for stream, output in (("stdout", result.stdout), ("stderr", result.stderr))
            for phase in ("COLLECTION", "SUMMARY", "UNCONFIGURE")
            for index in range(3)
        }
        assert all(count == 1 for count in observed.values()), (observed, diagnostic)
        for output in (result.stdout, result.stderr):
            assert output.splitlines().count("WORKER-TAIL:0") == 1, diagnostic
        assert (
            sum(
                "surplus sessionfinish warning" in warning["message"]
                for warning in record["warnings"]
            )
            == 1
        ), (record, diagnostic)
        assert record["execution_topology"]["active_workers"] == 1, (record, diagnostic)
        assert record["collected"] == 1, (record, diagnostic)
        assert record["counts"]["passed"] == int(not collect_only), (record, diagnostic)
        if collect_only:
            assert result.stdout.splitlines().count("test_single.py::test_single") == 1, diagnostic
