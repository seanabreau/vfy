"""Parent cancellation waits for workers and keeps bounded process ownership."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest

from tests.integration.test_independent_watch import (
    _ACCEPTED,
    _REPORT_ARGUMENTS,
    _project,
)
from tests.integration.test_watch import _wait_for_watch_event, _wait_for_watch_path
from tests.support import ManagedCommand, VfyProject
from tests.support.isolated_package import isolated_vfy_path


def _wait_for_child_path(project: VfyProject, command: ManagedCommand, relative: str) -> Path:
    path = project.root / relative
    deadline = time.monotonic() + 10
    while not path.exists():
        exited = command.poll() is not None
        timed_out = time.monotonic() >= deadline
        if exited or timed_out:
            if not exited:
                command.kill()
            result = command.wait(timeout=10)
            reason = "child exited" if exited else "timed out; child terminated"
            raise AssertionError(
                f"{reason} before fixture path {path}; status {result.returncode}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        time.sleep(0.02)
    return path


def _assert_locks_released(root: Path) -> None:
    import fcntl  # noqa: PLC0415 - POSIX process ownership proof

    for path in sorted((root / ".vfy").glob("*.lock")):
        with path.open() as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _cancellation_project(tmp_path: Path, mode: str) -> VfyProject:
    project = _project(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = " + " ".join(_REPORT_ARGUMENTS) + "\n")
    project.write(
        "descendant.py",
        "import fcntl, signal, sys\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "with open(sys.argv[1], 'w') as locked:\n"
        "    fcntl.flock(locked, fcntl.LOCK_EX)\n"
        "    print('ready', flush=True)\n"
        "    signal.pause()\n",
    )
    project.write(
        "conftest.py",
        f"""import os, pytest
from pathlib import Path

def pytest_sessionfinish():
    if {mode!r} == 'finish-error' and Path('.vfy/interrupt').exists():
        raise ValueError('actual late worker finish failure')

def pytest_collection_modifyitems(items):
    # Demand's first two leases must both enter test_wait before parent SIGINT.
    items.sort(key=lambda item: item.name != 'test_wait')
""",
    )
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            f"""import fcntl, os, signal, subprocess, sys
from pathlib import Path
from subject import value

def test_prefix():
    assert value() > 0

def test_wait():
    if not Path('.vfy/interrupt').exists():
        return
    def interrupted(signum, frame):
        with Path('.vfy/signals-{name}').open('a') as output:
            output.write('INT\\n')
        if {mode!r} == 'hard-exit':
            os._exit(2)
        if {mode!r} != 'ignored':
            raise KeyboardInterrupt('actual worker signal')
    signal.signal(signal.SIGINT, interrupted)
    with open('.vfy/worker-{name}.lock', 'w') as locked:
        fcntl.flock(locked, fcntl.LOCK_EX)
        child = subprocess.Popen([sys.executable, 'descendant.py', '.vfy/descendant-{name}.lock'],
                                 stdout=subprocess.PIPE, text=True)
        assert child.stdout.readline().strip() == 'ready'
        try:
            Path('.vfy/ready-{name}').touch()
            while True:
                signal.pause()
        finally:
            Path('.vfy/finalized-{name}').touch()

def test_never():
    if Path('.vfy/interrupt').exists():
        Path('.vfy/never-{name}').touch()
""",
        )
    return project


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups and flock")
@pytest.mark.parametrize("mode", ["complete", "ignored", "hard-exit", "finish-error"])
def test_watch_parent_interrupt_waits_for_worker_completion(tmp_path: Path, mode: str) -> None:
    project = _cancellation_project(tmp_path, mode)
    command = project.start_vfy(
        "--verbose",
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
        result = command.wait(timeout=20)
        result.expect(2)
        assert {name: (tmp_path / name).read_bytes() for name in _ACCEPTED} == accepted
        _assert_locks_released(tmp_path)
        if mode == "complete":
            for name in ("a", "b"):
                assert (tmp_path / f".vfy/finalized-{name}").exists()
                assert not (tmp_path / f".vfy/never-{name}").exists()
        if mode == "finish-error":
            assert "actual late worker finish failure" in result.stdout + result.stderr
        for name in ("a", "b"):
            # A hard exit can kill its peer before that handler flushes its signal record.
            path = tmp_path / f".vfy/signals-{name}"
            if path.exists():
                deliveries = path.read_text().splitlines()
                assert deliveries == ["INT"] or (mode == "hard-exit" and deliveries == [])
    finally:
        if command.poll() is None:
            command.kill()
            command.wait(timeout=10)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX signals and process groups")
@pytest.mark.parametrize("release", [True, False])
def test_worker_control_then_parent_interrupt_does_not_signal_peer_twice(
    tmp_path: Path, release: bool
) -> None:
    project = _cancellation_project(tmp_path, "complete")
    project.write(
        "conftest.py",
        """import os, signal
from pathlib import Path

original = KeyboardInterrupt('original parent after authenticated worker control')

def pytest_sessionstart():
    if 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ:
        def interrupt_parent(signum, frame):
            Path('.vfy/parent-int').touch()
            raise original
        signal.signal(signal.SIGINT, interrupt_parent)

def pytest_keyboard_interrupt(excinfo):
    if 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ:
        assert excinfo.value is original
        Path('.vfy/parent-original').touch()

def pytest_collection_modifyitems(items):
    # Both sides of the readiness handshake need their own initial worker lease.
    items.sort(key=lambda item: item.name not in {'test_control', 'test_wait'})
""",
    )
    project.write(
        "test_a.py",
        """import pytest, time
from pathlib import Path
from subject import value

def test_prefix():
    assert value() > 0

def test_control():
    if not Path('.vfy/interrupt').exists():
        return
    while not Path('.vfy/ready-b').exists():
        time.sleep(.01)
    pytest.exit('worker control precedes parent', returncode=42)
""",
    )
    worker = (
        (tmp_path / "test_b.py")
        .read_text()
        .replace(
            "Path('.vfy/finalized-b').touch()",
            "Path('.vfy/cleanup-b').touch()\n"
            "            while not Path('.vfy/release-cleanup').exists():\n"
            "                __import__('time').sleep(.01)\n"
            "            Path('.vfy/finalized-b').touch()",
        )
    )
    worker = worker.replace(
        "output.write('INT\\n')",
        "output.write('INT\\n')\n"
        "        Path('.vfy/first-int-time').write_text(str(__import__('time').monotonic()))",
    )
    project.write("test_b.py", worker)
    command = project.start_vfy(
        "--verbose",
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
        # The cleanup marker shows a worker control already delivered INT.
        _wait_for_watch_path(command, project, ".vfy/cleanup-b")
        first_interrupt = float((tmp_path / ".vfy/first-int-time").read_text())
        if not release:
            # Delay parent delivery inside the existing worker deadline to expose a reset.
            while time.monotonic() - first_interrupt < 3:
                time.sleep(0.01)
        command.send_signal(signal.SIGINT)
        _wait_for_watch_path(command, project, ".vfy/parent-int")
        if release:
            project.write(".vfy/release-cleanup", "")
        result = command.wait(timeout=20)
        result.expect(2)
        if release:
            assert (tmp_path / ".vfy/parent-original").exists()
        assert (tmp_path / ".vfy/signals-b").read_text().splitlines() == ["INT"]
        assert {name: (tmp_path / name).read_bytes() for name in _ACCEPTED} == accepted
        _assert_locks_released(tmp_path)
        if not release:
            assert "drain deadline" in result.stdout + result.stderr
            assert time.monotonic() - first_interrupt < 7
    finally:
        if command.poll() is None:
            command.kill()
            command.wait(timeout=10)


@pytest.mark.skipif(os.name != "posix", reason="requires actual parent SIGINT")
@pytest.mark.parametrize(
    ("mode", "status"),
    [
        ("parent", 2),
        ("replace-interrupted", 2),
        ("replace-runtime", 3),
        ("finish-override", 77),
    ],
)
@pytest.mark.parametrize("delivery", ["poll", "progress", "replay"])
def test_parent_control_identity_traceback_and_finish_priority_match_pytest(
    tmp_path: Path, mode: str, status: int, delivery: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"""import json, os, signal, pytest
from pathlib import Path

original = None
replacement = None
events = []

def pytest_sessionstart(session):
    global original
    if 'VFY_NATIVE_PARALLEL_INDEX' in os.environ:
        return
    class ParentInterrupt(pytest.Session.Interrupted):
        pass
    original = ParentInterrupt('original parent interrupt')
    def interrupt_parent(signum, frame):
        raise original
    signal.signal(signal.SIGINT, interrupt_parent)
    if os.environ.get('PARENT_PROGRESS_INTERRUPT'):
        writer = session.config.pluginmanager.getplugin('terminalreporter')._tw
        previous_flush = writer.flush
        armed = True
        def progress_flush():
            nonlocal armed
            if armed and all(Path('prefix-ready-' + name).exists() for name in ('a', 'b')):
                import select
                armed = False
                read, write = os.pipe()
                os.set_blocking(write, False)
                previous = signal.set_wakeup_fd(write)
                try:
                    Path('progress-ready').touch()
                    select.select([read], [], [])
                    raise AssertionError('progress signal wait returned without interruption')
                finally:
                    signal.set_wakeup_fd(previous)
                    os.close(write)
                    os.close(read)
            previous_flush()
        writer.flush = progress_flush
    if os.environ.get('PARENT_REPLAY_INTERRUPT'):
        import sys
        previous_stdout = sys.stdout
        replay_armed = True
        class ReplayStream:
            def __getattr__(self, name):
                return getattr(previous_stdout, name)
            def write(self, text):
                nonlocal replay_armed
                if 'worker-replay-marker' in text:
                    with Path('replay-writes').open('a') as calls:
                        calls.write('write\\n')
                    if replay_armed:
                        replay_armed = False
                        import select
                        read, write = os.pipe()
                        os.set_blocking(write, False)
                        previous = signal.set_wakeup_fd(write)
                        try:
                            Path('replay-ready').touch()
                            select.select([read], [], [])
                            raise AssertionError('replay signal wait returned without interruption')
                        finally:
                            signal.set_wakeup_fd(previous)
                            os.close(write)
                            os.close(read)
                return previous_stdout.write(text)
        sys.stdout = ReplayStream()

@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session):
    global replacement
    try:
        result = yield
    except KeyboardInterrupt as error:
        if 'VFY_NATIVE_PARALLEL_INDEX' in os.environ:
            raise
        assert error is original
        events.append(['wrapper', error is original])
        if {mode!r} == 'replace-interrupted':
            replacement = pytest.Session.Interrupted('distinct wrapper replacement')
            raise replacement
        if {mode!r} == 'replace-runtime':
            replacement = RuntimeError('ordinary wrapper replacement')
            raise replacement
        raise
    return result

def pytest_keyboard_interrupt(excinfo):
    if 'VFY_NATIVE_PARALLEL_INDEX' in os.environ:
        return
    wanted = replacement if replacement is not None else original
    assert excinfo.value is wanted
    frames, trace = [], excinfo.value.__traceback__
    while trace is not None:
        frames.append(trace.tb_frame.f_code.co_name)
        trace = trace.tb_next
    events.append(['interrupt', excinfo.value is wanted, 'interrupt_parent' in frames])

def pytest_internalerror(excinfo):
    if 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ:
        events.append(['internal', type(excinfo.value).__name__, excinfo.value is replacement])

def pytest_sessionfinish(session, exitstatus):
    if 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ:
        events.append(['finish', int(exitstatus)])
        if {mode!r} == 'finish-override':
            session.exitstatus = 77
        if {mode!r} == 'finish-error':
            raise ValueError('late coordinator finish failure')

def pytest_unconfigure():
    if 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ:
        Path('parent-events.json').write_text(json.dumps(events))
""",
    )
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            "import os, select, signal\nfrom pathlib import Path\n"
            "def test_prefix():\n"
            "    if os.environ.get('PARENT_REPLAY_INTERRUPT'):\n"
            "        print('worker-replay-marker', flush=True)\n"
            "    if os.environ.get('PARENT_PROGRESS_INTERRUPT'):\n"
            f"        release = os.open('prefix-release-{name}', os.O_RDWR)\n"
            "        try:\n"
            f"            Path('prefix-ready-{name}').touch()\n"
            "            assert os.read(release, 2) == b'go'\n"
            "        finally:\n"
            "            os.close(release)\n"
            "def test_wait():\n"
            "    if os.environ.get('PARENT_REPLAY_INTERRUPT'):\n"
            "        return\n"
            "    read, write = os.pipe()\n"
            "    os.set_blocking(write, False)\n"
            "    previous = signal.set_wakeup_fd(write)\n"
            "    try:\n"
            # A signal between readiness and the blocking syscall must remain readable.
            f"        Path('ready-{name}').touch()\n"
            "        select.select([read], [], [])\n"
            "        raise AssertionError('signal wait returned without raising interrupt')\n"
            "    finally:\n"
            "        signal.set_wakeup_fd(previous)\n"
            "        os.close(write)\n"
            "        os.close(read)\n",
        )
    stock = project.start_python("-m", "pytest", "-q", "test_a.py")
    try:
        _wait_for_child_path(project, stock, "ready-a")
        stock.send_signal(signal.SIGINT)
        reference = stock.wait(timeout=15)
    finally:
        if stock.poll() is None:
            stock.kill()
            stock.wait(timeout=10)
    assert reference.returncode == status, reference.stdout + reference.stderr
    expected = json.loads((tmp_path / "parent-events.json").read_text())
    (tmp_path / "parent-events.json").unlink()
    (tmp_path / "ready-a").unlink()
    if delivery == "progress":
        for name in ("a", "b"):
            os.mkfifo(tmp_path / f"prefix-release-{name}")
    candidate = project.start_python(
        "-m",
        "vfy.independent.entry",
        "--workers=2",
        "--native-result-json=result.json",
        *(["-s"] if delivery == "replay" else []),
        env={
            "PARENT_PROGRESS_INTERRUPT": "1" if delivery == "progress" else None,
            "PARENT_REPLAY_INTERRUPT": "1" if delivery == "replay" else None,
        },
    )
    try:
        if delivery == "progress":
            for name in ("a", "b"):
                _wait_for_child_path(project, candidate, f"prefix-ready-{name}")
            for name in ("a", "b"):
                release = os.open(tmp_path / f"prefix-release-{name}", os.O_WRONLY | os.O_NONBLOCK)
                try:
                    assert os.write(release, b"go") == 2
                finally:
                    os.close(release)
        if delivery == "replay":
            _wait_for_child_path(project, candidate, "replay-ready")
        else:
            for name in ("a", "b"):
                _wait_for_child_path(project, candidate, f"ready-{name}")
        if delivery == "progress":
            _wait_for_child_path(project, candidate, "progress-ready")
        candidate.send_signal(signal.SIGINT)
        result = candidate.wait(timeout=20)
    finally:
        if candidate.poll() is None:
            candidate.kill()
            candidate.wait(timeout=10)
    assert result.returncode == status, result.stdout + result.stderr
    assert json.loads((tmp_path / "parent-events.json").read_text()) == expected
    record = json.loads((tmp_path / "result.json").read_text())
    assert record["exitstatus"] == status
    expected_cases = ["test_a.py::test_prefix", "test_b.py::test_prefix"]
    if delivery == "replay":
        expected_cases = [
            f"test_{name}.py::{case}"
            for name in ("a", "b")
            for case in ("test_prefix", "test_wait")
        ]
        assert (tmp_path / "replay-writes").read_text().splitlines() == ["write"]
    assert [case["nodeid"] for case in record["cases"]] == expected_cases
    assert record["execution_topology"]["active_workers"] == 2
