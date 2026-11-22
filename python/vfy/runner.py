"""Launch test commands with the controller's selected execution adapters."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from types import FrameType, TracebackType
from typing import Literal, NamedTuple, Protocol, Self


class RunnerRequest(NamedTuple):
    """Arguments, environment, and output destination for one test command."""

    arguments: tuple[str, ...]
    environment: Mapping[str, str]
    cwd: Path | None = None
    stdout: Literal["inherit", "stderr"] = "inherit"


class RunnerBackend(Protocol):
    """Execution backend for test commands."""

    def run(self, request: RunnerRequest) -> int:
        """Execute one test request and return its pytest-compatible status."""


def _signal_process_group(
    process: subprocess.Popen[str] | subprocess.Popen[bytes],
    action: Literal["interrupt", "terminate", "kill"],
) -> None:
    if os.name == "posix":
        value = {
            "interrupt": signal.SIGINT,
            "terminate": signal.SIGTERM,
            "kill": signal.SIGKILL,
        }[action]
        os.killpg(process.pid, value)
    elif action == "interrupt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    elif action == "terminate":
        process.terminate()
    else:
        process.kill()


def capture_command(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Capture a command and stop its process group on timeout or interruption."""
    with (
        _ChildTermination() as termination,
        subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name == "posix",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        ) as process,
    ):
        try:
            termination.attach(process)
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException as error:
            termination.cleaning_up = True
            previous_interrupt = None
            if threading.current_thread() is threading.main_thread():
                previous_interrupt = signal.getsignal(signal.SIGINT)
                if previous_interrupt is not None:
                    signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                # A vfy controller owns nested process groups. Let its SIGTERM
                # handler stop them before terminating the outer capture group.
                with suppress(ProcessLookupError):
                    _signal_process_group(process, "terminate")
                try:
                    stdout, stderr = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        _signal_process_group(process, "kill")
                    try:
                        stdout, stderr = process.communicate(timeout=5)
                    except subprocess.TimeoutExpired as drain_error:
                        if isinstance(error, subprocess.TimeoutExpired):
                            error.stdout, error.stderr = drain_error.stdout, drain_error.stderr
                        if isinstance(error, _RunnerTerminated):
                            raise
                        raise error from None
                finally:
                    # The leader can exit while descendants retain capture pipes.
                    with suppress(ProcessLookupError):
                        _signal_process_group(process, "kill")
                if isinstance(error, subprocess.TimeoutExpired):
                    error.stdout, error.stderr = (
                        value.encode("utf-8") if isinstance(value, str) else value or b""
                        for value in (stdout, stderr)
                    )
            finally:
                if previous_interrupt is not None:
                    signal.signal(signal.SIGINT, previous_interrupt)
            if not isinstance(error, _RunnerTerminated):
                raise
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


class _RunnerTerminated(BaseException):
    """Unwind the owned child wait before relaying SIGTERM to its caller."""


class _ChildTermination:
    """Restore the caller's SIGTERM behavior after the owned child has stopped."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | subprocess.Popen[bytes] | None = None
        self.requested: tuple[int, FrameType | None] | None = None
        self.cleaning_up = False
        self.previous = signal.getsignal(signal.SIGTERM)
        self.installed = False

    def __enter__(self) -> Self:
        if (
            threading.current_thread() is threading.main_thread()
            and self.previous is not None
            and self.previous != signal.SIG_IGN
        ):
            signal.signal(signal.SIGTERM, self._receive)
            self.installed = True
        return self

    def attach(self, process: subprocess.Popen[str] | subprocess.Popen[bytes]) -> None:
        self.process = process
        if self.requested is not None:
            raise _RunnerTerminated

    def _receive(self, signum: int, frame: FrameType | None) -> None:
        self.requested = signum, frame
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if self.process is not None and not self.cleaning_up:
            raise _RunnerTerminated

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if not self.installed or self.previous is None:
            return
        signal.signal(signal.SIGTERM, self.previous)
        requested, self.requested = self.requested, None
        if requested is not None:
            if callable(self.previous):
                self.previous(*requested)
            elif self.previous == signal.SIG_DFL:
                os.kill(os.getpid(), signal.SIGTERM)


class PytestRunner:
    """Launch the selected test command in the project environment."""

    def __init__(self, executable: Sequence[str] | None = None) -> None:
        self._executable = tuple(executable or (sys.executable, "-m", "pytest"))
        self._default_executable = executable is None

    def run(self, request: RunnerRequest) -> int:
        environment = dict(request.environment)
        in_process = environment.pop("VFY_IN_PROCESS_PYTEST", None) == "1"
        force_subprocess = environment.pop("VFY_FORCE_PYTEST_SUBPROCESS", None) == "1"
        if (
            in_process
            and not force_subprocess
            and self._default_executable
            and request.cwd is None
            and request.stdout == "inherit"
        ):
            import pytest  # noqa: PLC0415 - only the in-process backend needs pytest

            previous = os.environ.copy()
            previous_path = sys.path.copy()
            os.environ.clear()
            os.environ.update(environment)
            working_directory = os.getcwd()
            if not sys.path or sys.path[0] != working_directory:
                sys.path.insert(0, working_directory)
            try:
                return int(pytest.main(list(request.arguments)))
            except Exception as error:  # noqa: BLE001 - Match unhandled subprocess errors.
                sys.excepthook(type(error), error, error.__traceback__)
                return 1
            finally:
                sys.path[:] = previous_path
                os.environ.clear()
                os.environ.update(previous)
        if "VFY_PROFILE_DIR" in environment:
            environment["VFY_PROFILE_PYTEST_CHILD_SPAWN_NS"] = str(time.perf_counter_ns())
        with _ChildTermination() as termination:
            process = subprocess.Popen(
                [*self._executable, *request.arguments],
                cwd=request.cwd,
                env=environment,
                stdout=sys.stderr if request.stdout == "stderr" else None,
                start_new_session=os.name == "posix",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            try:
                termination.attach(process)
                return process.wait()
            except (KeyboardInterrupt, _RunnerTerminated) as error:
                termination.cleaning_up = True
                # Keep a second signal from abandoning the owned process group.
                previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
                try:
                    _signal_process_group(
                        process,
                        "interrupt" if isinstance(error, KeyboardInterrupt) else "terminate",
                    )
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        if isinstance(error, KeyboardInterrupt):
                            _signal_process_group(process, "terminate")
                            with suppress(subprocess.TimeoutExpired):
                                process.wait(timeout=2)
                        _signal_process_group(process, "kill")
                        process.wait()
                except ProcessLookupError:
                    process.wait()
                finally:
                    # The leader's exit does not prove that every group member
                    # accepted the cooperative shutdown signal.
                    with suppress(ProcessLookupError):
                        _signal_process_group(process, "kill")
                    signal.signal(signal.SIGINT, previous)
                if isinstance(error, KeyboardInterrupt):
                    raise
                return process.wait()
