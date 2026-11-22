"""Bounded subprocess capture shared by repository tests and ecosystem checks."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Literal


def _signal_process_tree(
    process: subprocess.Popen[str] | subprocess.Popen[bytes],
    action: Literal["kill", "terminate"],
    *,
    timeout: float,
) -> None:
    if sys.platform == "win32":
        if process.poll() is None:
            # taskkill cannot discover descendants after their parent exits.
            with suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
        with suppress(ProcessLookupError):
            process.kill()
        return

    value = signal.SIGKILL if action == "kill" else signal.SIGTERM
    try:
        os.killpg(process.pid, value)
    except ProcessLookupError:
        with suppress(ProcessLookupError):
            process.send_signal(value)


def kill_process_tree(
    process: subprocess.Popen[str] | subprocess.Popen[bytes], *, timeout: float = 10.0
) -> None:
    """Force-stop a process and its descendants when they share its process group."""

    _signal_process_tree(process, "kill", timeout=timeout)


def terminate_process_tree(
    process: subprocess.Popen[str] | subprocess.Popen[bytes], *, timeout: float = 10.0
) -> None:
    """Stop a process and its descendants when they share its process group."""

    _signal_process_tree(process, "terminate", timeout=timeout)


def run_captured(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    capture_stdout: bool = True,
    subprocess_umask: int | None = None,
    on_start: Callable[[int], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Capture a command with a deadline and best-effort descendant cleanup."""

    if subprocess_umask is not None and (
        os.name != "posix"
        or type(subprocess_umask) is not int
        or not 0 <= subprocess_umask <= 0o777
    ):
        raise ValueError("subprocess umask requires a POSIX integer permission mask")
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=sys.platform != "win32",
        umask=-1 if subprocess_umask is None else subprocess_umask,
    ) as process:
        try:
            if on_start is not None:
                on_start(process.pid)
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException as error:
            # The child may own another process session. Give its termination
            # handler time to cancel that session before forcing the outer group.
            with suppress(OSError):
                terminate_process_tree(process)
            try:
                try:
                    stdout, stderr = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    with suppress(OSError):
                        kill_process_tree(process)
                    try:
                        stdout, stderr = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired as drain_error:
                        stdout, stderr = drain_error.stdout, drain_error.stderr
                        if isinstance(error, subprocess.TimeoutExpired):
                            stdout = stdout if stdout is not None else error.stdout
                            stderr = stderr if stderr is not None else error.stderr
                else:
                    # A child that closed its pipes can still outlive its parent.
                    with suppress(OSError):
                        kill_process_tree(process)
                if isinstance(error, subprocess.TimeoutExpired):
                    error.stdout, error.stderr = (
                        value.encode("utf-8") if isinstance(value, str) else value or b""
                        for value in (stdout, stderr)
                    )
            except (OSError, ValueError):
                # Cleanup must not replace the original child or callback failure.
                with suppress(OSError):
                    kill_process_tree(process)
            raise
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)
