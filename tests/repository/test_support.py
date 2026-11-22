from __future__ import annotations

import contextlib
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import process_tree

from tests.support.vfy_project import (
    VfyProject,
    started_commands,
    stop_commands_started_since,
)


@pytest.mark.skipif(
    not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"),
    reason="process-exit observation requires Linux pidfds",
)
@pytest.mark.parametrize("parent_exits", [False, True])
@pytest.mark.parametrize("cleanup", ["project", "registry"])
def test_project_cleanup_stops_descendants_that_ignore_termination(
    tmp_path: Path, parent_exits: bool, cleanup: str
) -> None:
    if sys.platform != "linux":
        pytest.skip("process-exit observation requires Linux pidfds")
    project = VfyProject(tmp_path)
    project.write(
        "child.py",
        "import os, signal, threading\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print(f'child-ready={os.getpid()}', flush=True)\n"
        "threading.Event().wait()\n",
    )
    project.write(
        "parent.py",
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, 'child.py'])\n"
        + ("" if parent_exits else "child.wait()\n"),
    )
    before = started_commands()
    command = project.start_python("parent.py")
    descriptor: int | None = None
    try:
        command.wait_for("child-ready=", stream="stdout")
        match = re.search(r"child-ready=(\d+)", command.text("stdout"))
        assert match is not None, command.text("stdout")
        descriptor = os.pidfd_open(int(match.group(1)))
        if parent_exits:
            assert command.process.wait(timeout=5) == 0
        if cleanup == "project":
            project.close()
        else:
            stop_commands_started_since(before)
        assert select.select([descriptor], [], [], 5)[0], "cleanup left its descendant alive"
        command.wait(timeout=5).expect(0 if parent_exits else -signal.SIGTERM)
    finally:
        if descriptor is not None:
            if not select.select([descriptor], [], [], 0)[0]:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            os.close(descriptor)
        command.kill()
        command.wait(timeout=5)


def test_vfy_project_rejects_unknown_engine_before_running_a_command(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)

    with pytest.raises(ValueError, match="unknown test engine: 'invalid'"):
        project.run_engine("invalid")


def test_vfy_project_controls_environment_without_mutating_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "example.py",
        "import os\n"
        "print(os.environ.get('VFY_HARNESS_CHILD', 'missing'))\n"
        "print(os.environ.get('VFY_HARNESS_PARENT', 'missing'))\n",
    )
    monkeypatch.setenv("VFY_HARNESS_PARENT", "kept")

    result = project.run_python(
        "example.py",
        env={"VFY_HARNESS_CHILD": "set", "VFY_HARNESS_PARENT": None},
    )

    result.expect(0)
    assert result.stdout.splitlines() == ["set", "missing"]
    assert os.environ["VFY_HARNESS_PARENT"] == "kept"


def test_explicit_interpreter_does_not_inherit_the_oracle_installation(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(candidate)], check=True)
    interpreter = candidate / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    project = VfyProject(tmp_path / "project", interpreter=interpreter)
    project.write("example.py", "value = 1\n")

    result = project.run_python(
        "-c",
        "import importlib.util,json,sys; "
        "print(json.dumps({'prefix':sys.prefix, 'pytest':importlib.util.find_spec('pytest'), "
        "'vfy':importlib.util.find_spec('vfy')}))",
    )

    result.expect(0)
    assert json.loads(result.stdout) == {"prefix": str(candidate), "pytest": None, "vfy": None}
    missing = project.run_vfy("--version")
    assert missing.returncode != 0
    assert "No module named vfy" in missing.stderr
    with pytest.raises(RuntimeError, match="console entry point is not installed"):
        project.run_vfy_console("--version")


def test_vfy_project_runs_public_command_and_reads_schema_seven_lines(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    source = project.write("example.py", "value = 1\nvalue += 1\n")

    result = project.run_vfy("coverage", "run", "--data-file", ".coverage-harness", "example.py")

    result.expect(0)
    assert project.lines(source, ".coverage-harness") == [1, 2]


def test_vfy_project_runs_the_coverage_py_oracle(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    source = project.write("example.py", "value = 1\nvalue += 1\n")

    result = project.run_coverage_py("run", "--data-file", ".coverage-reference", "example.py")

    result.expect(0)
    assert project.lines(source, ".coverage-reference") == [1, 2]


def test_vfy_project_creates_a_git_baseline(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("src/value.py", "VALUE = 1\n")

    project.git_init()

    result = project.run_python(
        "-c",
        "import subprocess; "
        "print(subprocess.check_output("
        "['git', 'rev-parse', '--verify', 'HEAD'], text=True).strip())",
    )
    result.expect(0)
    assert len(result.stdout.strip()) == 40


def test_process_tree_kill_uses_taskkill_without_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    process = subprocess.Popen([sys.executable, "-c", "import threading; threading.Event().wait()"])
    taskkill_commands: list[list[str]] = []

    def record_taskkill(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        taskkill_commands.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(process_tree, "sys", SimpleNamespace(platform="win32"))
            scoped.setattr(
                process_tree,
                "signal",
                SimpleNamespace(SIGTERM=process_tree.signal.SIGTERM),
            )
            scoped.setattr(process_tree.subprocess, "run", record_taskkill)

            process_tree.kill_process_tree(process)

        assert taskkill_commands == [["taskkill", "/F", "/T", "/PID", str(process.pid)]]
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_project_ignores_ambient_configuration_but_allows_explicit_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "--invalid-ambient-option")
    monkeypatch.setenv("COVERAGE_PROCESS_START", str(tmp_path / "missing-coverage-config"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    project = VfyProject(tmp_path)
    project.write("test_app.py", "def test_passes():\n    assert True\n")
    project.git_init()
    assert project.run_pytest("-q").returncode == 0
    result = project.run_python(
        "-c",
        "import json, os; print(json.dumps([os.getenv('COVERAGE_PROCESS_START'), "
        "os.getenv('PYTEST_ADDOPTS')]))",
        env={"PYTEST_ADDOPTS": "--verbose"},
    )
    result.expect(0)
    assert json.loads(result.stdout) == [None, "--verbose"]


@pytest.mark.parametrize(
    "parent_exits",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="taskkill requires a live parent"
            ),
        ),
    ],
)
def test_timeout_preserves_output_and_terminates_descendants(
    tmp_path: Path, parent_exits: bool
) -> None:
    deadline = 5 if sys.platform == "win32" else 2
    project = VfyProject(tmp_path, timeout=deadline)
    project.write(
        "child.py",
        "import socket, time\n"
        "listener = socket.socket()\n"
        "listener.bind(('127.0.0.1', 0))\n"
        "listener.listen()\n"
        "print(f'child-port={listener.getsockname()[1]}', flush=True)\n"
        "time.sleep(30)\n",
    )
    project.write(
        "parent.py",
        "import subprocess, sys\n"
        "print('parent-started', flush=True)\n"
        "child = subprocess.Popen([sys.executable, 'child.py'])\n"
        + ("" if parent_exits else "child.wait()\n"),
    )
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=f"timed out after {deadline}s") as failure:
        project.run_python("parent.py")
    assert time.monotonic() - started < deadline + 15, "waited for the child's natural exit"
    message = str(failure.value)
    assert "parent-started" in message
    assert str(tmp_path) in message
    match = re.search(r"child-port=(\d+)", message)
    assert match is not None, message
    with (
        pytest.raises(ConnectionRefusedError),
        socket.create_connection(("127.0.0.1", int(match.group(1))), timeout=1),
    ):
        pytest.fail("timed-out descendant still accepts connections")


@pytest.mark.skipif(
    not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"),
    reason="process-exit observation requires Linux pidfds",
)
def test_run_captured_timeout_allows_detached_session_cleanup(tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("process-exit observation requires Linux pidfds")
    project = VfyProject(tmp_path)
    project.write(
        "worker.py",
        "import threading\nprint('worker-ready', flush=True)\nthreading.Event().wait()\n",
    )
    project.write(
        "controller.py",
        """import signal, socket, subprocess, sys, threading
worker = subprocess.Popen([sys.executable, 'worker.py'], start_new_session=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
def terminate(signum, frame):
    worker.terminate()
    worker.wait(timeout=5)
    print('controller-cleaned-worker', flush=True)
    print('controller-terminated', file=sys.stderr, flush=True)
    raise SystemExit(143)
signal.signal(signal.SIGTERM, terminate)
assert worker.stdout.readline().strip() == 'worker-ready'
print('controller-ready', flush=True)
print('controller-stderr', file=sys.stderr, flush=True)
with socket.create_connection(('127.0.0.1', int(sys.argv[1]))) as connection:
    connection.sendall(str(worker.pid).encode())
threading.Event().wait()
""",
    )
    descriptors: list[int] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(5)

        def ready(pid: int) -> None:
            descriptors.append(os.pidfd_open(pid))
            with listener.accept()[0] as connection:
                descriptors.append(os.pidfd_open(int(connection.recv(64))))

        try:
            with pytest.raises(subprocess.TimeoutExpired) as failure:
                process_tree.run_captured(
                    [sys.executable, "controller.py", str(listener.getsockname()[1])],
                    cwd=project.root,
                    env=dict(os.environ),
                    timeout=0.2,
                    on_start=ready,
                )
            assert len(descriptors) == 2
            assert all(select.select([fd], [], [], 5)[0] for fd in descriptors)
            stdout, stderr = failure.value.stdout, failure.value.stderr
            assert stdout is not None
            assert stderr is not None
            assert b"controller-ready\n" in stdout
            assert b"controller-cleaned-worker\n" in stdout
            assert b"controller-stderr\n" in stderr
            assert b"controller-terminated\n" in stderr
        finally:
            for descriptor in descriptors:
                with contextlib.suppress(ProcessLookupError):
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                os.close(descriptor)


@pytest.mark.skipif(
    not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"),
    reason="process-exit observation requires Linux pidfds",
)
def test_run_captured_timeout_force_kills_ignored_term_and_retains_output(tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("process-exit observation requires Linux pidfds")
    project = VfyProject(tmp_path)
    project.write(
        "ignore.py",
        """import signal, socket, sys, threading
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('partial-stdout', flush=True)
print('partial-stderr', file=sys.stderr, flush=True)
with socket.create_connection(('127.0.0.1', int(sys.argv[1]))) as connection:
    connection.sendall(b'ready')
threading.Event().wait()
""",
    )
    descriptors: list[int] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(5)

        def ready(pid: int) -> None:
            descriptors.append(os.pidfd_open(pid))
            with listener.accept()[0] as connection:
                assert connection.recv(64) == b"ready"

        try:
            with pytest.raises(subprocess.TimeoutExpired) as failure:
                process_tree.run_captured(
                    [sys.executable, "ignore.py", str(listener.getsockname()[1])],
                    cwd=project.root,
                    env=dict(os.environ),
                    timeout=0.2,
                    on_start=ready,
                )
            assert len(descriptors) == 1
            assert select.select(descriptors, [], [], 5)[0] == descriptors
            assert failure.value.stdout == b"partial-stdout\n"
            assert failure.value.stderr == b"partial-stderr\n"
        finally:
            for descriptor in descriptors:
                with contextlib.suppress(ProcessLookupError):
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                os.close(descriptor)


@pytest.mark.skipif(
    not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"),
    reason="process-exit observation requires Linux pidfds",
)
def test_run_captured_preserves_start_callback_failure_and_reaps_child(tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("process-exit observation requires Linux pidfds")
    descriptors: list[int] = []
    failure = RuntimeError("start callback failed")

    def reject(pid: int) -> None:
        descriptors.append(os.pidfd_open(pid))
        raise failure

    try:
        with pytest.raises(RuntimeError) as actual:
            process_tree.run_captured(
                [sys.executable, "-c", "import threading; threading.Event().wait()"],
                cwd=tmp_path,
                env=dict(os.environ),
                timeout=5,
                on_start=reject,
            )
        assert actual.value is failure
        assert len(descriptors) == 1
        assert select.select(descriptors, [], [], 5)[0] == descriptors
    finally:
        for descriptor in descriptors:
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            os.close(descriptor)
