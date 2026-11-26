"""Temporary projects for tests of public vfy commands."""

from __future__ import annotations

import atexit
import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Self, TextIO

import pytest
from coverage import CoverageData
from scripts.process_tree import kill_process_tree, run_captured, terminate_process_tree

from vfy import __version__

ENGINE_NAMES = ("stock", "independent")
ENGINES = pytest.mark.parametrize("engine", ENGINE_NAMES)


@dataclass(frozen=True)
class CommandResult:
    """The observable result of a subprocess used by an integration test."""

    returncode: int
    stdout: str
    stderr: str
    command: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def from_completed(cls, completed: subprocess.CompletedProcess[str]) -> Self:
        """Capture a completed subprocess, keeping its command for failure messages."""

        return cls(
            completed.returncode,
            completed.stdout,
            completed.stderr,
            _command_tuple(completed.args),
        )

    def expect(self, returncode: int) -> Self:
        """Assert the exit status; the failure message carries the command and both streams."""

        if self.returncode != returncode:
            rendered = subprocess.list2cmdline(self.command) if self.command else "<unknown>"
            raise AssertionError(
                f"expected exit status {returncode}, got {self.returncode}\n"
                f"command: {rendered}\n"
                f"stdout:\n{self.stdout}\nstderr:\n{self.stderr}"
            )
        return self


def _command_tuple(args: object) -> tuple[str, ...]:
    if isinstance(args, (str, os.PathLike)):
        return (str(args),)
    if isinstance(args, Sequence):
        return tuple(str(argument) for argument in args)
    return ()


def _stop_commands(commands: Iterable[ManagedCommand]) -> None:
    """Stop owned process groups, reap their launchers, and drain captured output."""

    for command in list(commands):
        with suppress(OSError):
            command.terminate()
        with suppress(OSError, subprocess.TimeoutExpired):
            command.process.wait(timeout=2)
        # A launcher can exit while its descendants ignore termination.
        with suppress(OSError):
            command.kill()
        with suppress(OSError, subprocess.TimeoutExpired):
            command.wait(timeout=10)


class ManagedCommand:
    """Capture a long-running command while exposing synchronization points."""

    def __init__(
        self, process: subprocess.Popen[str], *, capture_partial_output: bool = False
    ) -> None:
        self.process = process
        self._capture_partial_output = capture_partial_output
        self._output: dict[str, list[str]] = {"stdout": [], "stderr": []}
        self._changed = threading.Event()
        self._threads = [
            threading.Thread(
                target=self._read_stream,
                args=("stdout", process.stdout),
                daemon=True,
            ),
            threading.Thread(
                target=self._read_stream,
                args=("stderr", process.stderr),
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()

    def _read_stream(self, name: str, stream: TextIO | None) -> None:
        if stream is None:
            return
        with stream:
            chunks = iter(lambda: stream.read(1), "") if self._capture_partial_output else stream
            for chunk in chunks:
                self._output[name].append(chunk)
                self._changed.set()
        self._changed.set()

    def poll(self) -> int | None:
        """Return the child status without waiting for a run boundary."""

        return self.process.poll()

    def text(self, stream: str = "stderr") -> str:
        """Return output observed so far from one child stream."""

        if stream not in self._output:
            raise ValueError(f"unknown stream: {stream}")
        return "".join(self._output[stream])

    def wait_for(self, text: str, *, stream: str = "stderr", timeout: float = 10.0) -> None:
        """Wait until the selected child stream contains the marker."""

        self.wait_for_match(re.escape(text), stream=stream, timeout=timeout)

    def wait_for_match(
        self,
        pattern: str,
        *,
        stream: str = "stderr",
        after: int = 0,
        timeout: float = 10.0,
    ) -> re.Match[str]:
        """Wait for a regex match in newly observed output after a text offset."""

        matcher = re.compile(pattern)
        deadline = time.monotonic() + timeout
        while True:
            match = matcher.search(self.text(stream), after)
            if match is not None:
                return match
            if self.poll() is not None and not any(thread.is_alive() for thread in self._threads):
                raise AssertionError(
                    f"command exited before output pattern {pattern!r}\n"
                    f"stdout:\n{self.text('stdout')}\nstderr:\n{self.text()}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"timed out waiting for output pattern {pattern!r}\n"
                    f"stdout:\n{self.text('stdout')}\nstderr:\n{self.text()}"
                )
            self._changed.wait(min(remaining, 0.05))
            self._changed.clear()

    def wait(self, timeout: float | None = None) -> CommandResult:
        """Reap the process and return all captured output.

        A timeout stops the process tree, drains both streams for a bounded
        period, and raises ``TimeoutExpired`` with the output captured so far.
        """

        try:
            returncode = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            kill_process_tree(self.process)
            deadline = time.monotonic() + 10.0
            with suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
            for thread in self._threads:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            error.stdout = self.text("stdout").encode("utf-8")
            error.stderr = self.text("stderr").encode("utf-8")
            raise
        for thread in self._threads:
            thread.join(timeout=1)
        return CommandResult(
            returncode,
            self.text("stdout"),
            self.text("stderr"),
            _command_tuple(self.process.args),
        )

    def send_signal(self, value: signal.Signals) -> None:
        """Deliver a signal through the managed process boundary."""

        self.process.send_signal(value)

    def terminate(self) -> None:
        """Send SIGTERM to the process group; on Windows, end the process tree."""

        terminate_process_tree(self.process)

    def kill(self) -> None:
        """Force-stop the process group; on Windows, end the process tree."""

        kill_process_tree(self.process)


_STARTED: list[ManagedCommand] = []
atexit.register(lambda: _stop_commands(_STARTED))


def started_commands() -> frozenset[ManagedCommand]:
    """Return the commands started so far by any project in this process."""

    return frozenset(_STARTED)


def stop_commands_started_since(before: frozenset[ManagedCommand]) -> None:
    """Stop commands started after a ``started_commands`` snapshot and forget finished ones."""

    _stop_commands(command for command in _STARTED if command not in before)
    _STARTED[:] = [command for command in _STARTED if command.poll() is None]


class VfyProject:
    """Create and exercise a temporary project through supported entry points.

    Tests supply source fixtures and assertions; the harness manages processes
    and reads their data. Children use the active installation, falling back
    to the source path only when vfy is not installed.

    Commands from ``start_vfy`` and ``start_python`` are tracked. ``close`` (also
    run by leaving a ``with`` block) stops this project's commands that are
    still running. ``tests/conftest.py`` stops every command a test leaves
    running, and interpreter exit stops any that remain.
    """

    def __init__(
        self,
        root: Path,
        *,
        timeout: float = 120,
        interpreter: str | Path | None = None,
    ) -> None:
        self.root = root
        self.timeout = timeout
        self.interpreter = str(interpreter) if interpreter is not None else sys.executable
        self._explicit_interpreter = interpreter is not None
        # Pytest owns the parent; keep ambient configuration outside measured source.
        self._home = root.parent / f".{root.name}-vfy-home"
        self._home.mkdir(parents=True, exist_ok=True)
        self._commands: list[ManagedCommand] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Stop commands started through this project that are still running."""

        _stop_commands(self._commands)

    def normalize(self, text: str) -> str:
        """Replace machine-specific values in command output with stable markers.

        The project root becomes ``[PROJECT]``, the system temporary directory
        ``[TMP]``, summary durations such as ``0.12s`` ``[TIME]``, and the vfy
        version ``[VERSION]``. The project root is replaced first because it
        usually lies inside the temporary directory.
        """

        text = text.replace(str(self.root), "[PROJECT]")
        text = text.replace(str(Path(tempfile.gettempdir())), "[TMP]")
        text = re.sub(r"(?<![\w.])\d+\.\d+s\b", "[TIME]", text)
        return re.sub(rf"(?<![\w.]){re.escape(__version__)}(?![\w.])", "[VERSION]", text)

    def write(self, relative: str | Path, contents: str) -> Path:
        """Write a project file and create its parent directory."""

        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def git_init(self, message: str = "baseline") -> None:
        """Create a deterministic Git baseline for test-selection scenarios."""

        self._run_checked(["git", "init", "-q"])
        self._run_checked(["git", "config", "user.email", "vfy-tests@example.invalid"])
        self._run_checked(["git", "config", "user.name", "vfy Tests"])
        self.git_commit(message)

    def git_commit(self, message: str) -> None:
        """Commit the current project state for a follow-up selection run."""

        self._run_checked(["git", "add", "."])
        self._run_checked(["git", "commit", "-qm", message])

    def run_vfy(
        self,
        *arguments: str,
        env: Mapping[str, str | None] | None = None,
    ) -> CommandResult:
        """Run ``python -m vfy`` in the project and capture both streams."""

        return self._run([self.interpreter, "-m", "vfy", *arguments], env=env)

    def run_vfy_console(
        self,
        *arguments: str,
        env: Mapping[str, str | None] | None = None,
    ) -> CommandResult:
        """Run the installed ``vfy`` console entry point in the project."""

        # Prefer the script installed beside this interpreter over another on PATH.
        executable = shutil.which("vfy", path=str(Path(self.interpreter).parent))
        if executable is None and not self._explicit_interpreter:
            executable = shutil.which("vfy")
        if executable is None:
            raise RuntimeError("the vfy console entry point is not installed")
        return self._run([executable, *arguments], env=env)

    def start_vfy(
        self,
        *arguments: str,
        env: Mapping[str, str | None] | None = None,
        capture_partial_output: bool = False,
    ) -> ManagedCommand:
        """Start ``python -m vfy`` and optionally capture flushed partial lines."""

        return self._start(
            [self.interpreter, "-m", "vfy", *arguments],
            env=env,
            capture_partial_output=capture_partial_output,
        )

    def run(
        self,
        *command: str,
        env: Mapping[str, str | None] | None = None,
    ) -> CommandResult:
        """Run an arbitrary project command with the harness environment."""

        return self._run(command, env=env)

    def run_python(
        self,
        *arguments: str,
        env: Mapping[str, str | None] | None = None,
    ) -> CommandResult:
        """Run a project Python program with the same isolated environment."""

        return self._run([self.interpreter, *arguments], env=env)

    def start_python(
        self,
        *arguments: str,
        env: Mapping[str, str | None] | None = None,
        capture_partial_output: bool = False,
    ) -> ManagedCommand:
        """Start Python and optionally capture flushed partial lines."""

        return self._start(
            [self.interpreter, *arguments],
            env=env,
            capture_partial_output=capture_partial_output,
        )

    def run_coverage_py(
        self,
        *arguments: str,
        env: Mapping[str, str | None] | None = None,
    ) -> CommandResult:
        """Run the pinned Coverage.py command for a differential oracle."""

        return self._run([self.interpreter, "-m", "coverage", *arguments], env=env)

    def run_closing_stdout(
        self,
        *command: str,
        lines: int = 1,
        marker: str = "stdout-closed",
        env: Mapping[str, str | None] | None = None,
    ) -> CommandResult:
        """Run a command whose stdout reader closes the pipe after ``lines`` lines.

        After closing, the reader creates ``marker`` in the project so a fixture
        can wait for it and make the command's next write fail deterministically.
        A write already in progress when the reader closes can still succeed.
        The result holds the lines read and the complete stderr.
        """

        with subprocess.Popen(
            list(command),
            cwd=self.root,
            env=self._environment(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=os.name == "posix",
        ) as process:
            assert process.stdout is not None
            try:
                # Read unbuffered so the pipe keeps everything after the last line read.
                read = bytearray()
                while read.count(b"\n") < lines and (byte := process.stdout.read(1)):
                    read += byte
                process.stdout.close()
                (self.root / marker).touch()
                _stdout, stderr = process.communicate(timeout=self.timeout)
            except BaseException:
                kill_process_tree(process)
                raise
        return CommandResult(process.returncode, read.decode(), stderr.decode(), tuple(command))

    def run_pytest(
        self,
        *arguments: str,
        env: Mapping[str, str | None] | None = None,
        load_xdist: bool = True,
    ) -> CommandResult:
        """Run pytest with vfy's public plug-in boundary enabled."""

        command = [self.interpreter, "-m", "pytest"]
        if load_xdist:
            command.extend(["-p", "vfy.pytest_plugin", "-p", "xdist.plugin"])
        command.extend(arguments)
        return self._run(command, env=env)

    def run_engine(
        self,
        engine: str,
        *arguments: str,
        stock: Sequence[str] = (),
        independent: Sequence[str] = (),
        no_cov: bool = True,
        env: Mapping[str, str | None] | None = None,
    ) -> CommandResult:
        """Run stock pytest or ``vfy test --engine=native`` on the same arguments."""

        if engine == "stock":
            return self.run_python("-m", "pytest", *stock, *arguments, env=env)
        if engine not in {"native", "independent"}:
            raise ValueError(f"unknown test engine: {engine!r}")
        prefix = ["test", "--engine=native", *(["--no-cov"] if no_cov else [])]
        return self.run_vfy(*prefix, *independent, *arguments, env=env)

    def data(self, relative: str | Path = ".coverage") -> CoverageData:
        """Read a schema-7 data file produced by a completed command."""

        data = CoverageData(str(self.root / relative))
        data.read()
        return data

    def lines(self, source: str | Path, relative: str | Path = ".coverage") -> list[int] | None:
        """Return measured lines using Coverage.py's public reader."""

        return self.data(relative).lines(str(self.root / source))

    def arcs(
        self, source: str | Path, relative: str | Path = ".coverage"
    ) -> list[tuple[int, int]] | None:
        """Return measured arcs using Coverage.py's public reader."""

        return self.data(relative).arcs(str(self.root / source))

    def _run(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str | None] | None = None,
    ) -> CommandResult:
        try:
            completed = run_captured(
                command, cwd=self.root, env=self._environment(env), timeout=self.timeout
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"project command timed out after {self.timeout}s: "
                f"{subprocess.list2cmdline(command)}\ncwd: {self.root}\n"
                f"stdout:\n{error.stdout}\nstderr:\n{error.stderr}"
            ) from error
        return CommandResult.from_completed(completed)

    def _start(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str | None] | None = None,
        capture_partial_output: bool = False,
    ) -> ManagedCommand:
        process = subprocess.Popen(
            list(command),
            cwd=self.root,
            env=self._environment(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=os.name == "posix",
        )
        command_handle = ManagedCommand(process, capture_partial_output=capture_partial_output)
        self._commands.append(command_handle)
        _STARTED.append(command_handle)
        return command_handle

    def wait_for_path(self, relative: str | Path, timeout: float = 10.0) -> Path:
        """Wait for a fixture marker created by a managed child process."""

        path = self.root / relative
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for fixture path {path}")
            time.sleep(0.02)
        return path

    def _environment(self, env: Mapping[str, str | None] | None) -> dict[str, str]:
        process_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GIT_", "COVERAGE_", "PYTEST_"))
            and key not in {"PYTHONPATH", "PYTHONHOME"}
        }
        home = str(self._home)
        process_environment.update(
            HOME=home,
            USERPROFILE=home,
            XDG_CONFIG_HOME=home,
            APPDATA=home,
            PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_COUNT="6",
            GIT_CONFIG_KEY_0="commit.gpgsign",
            GIT_CONFIG_VALUE_0="false",
            GIT_CONFIG_KEY_1="tag.gpgsign",
            GIT_CONFIG_VALUE_1="false",
            GIT_CONFIG_KEY_2="core.autocrlf",
            GIT_CONFIG_VALUE_2="false",
            GIT_CONFIG_KEY_3="core.hooksPath",
            GIT_CONFIG_VALUE_3=home,
            # Detached auto-maintenance can add and remove lock files while a
            # test copies the repository.
            GIT_CONFIG_KEY_4="maintenance.auto",
            GIT_CONFIG_VALUE_4="false",
            GIT_CONFIG_KEY_5="gc.auto",
            GIT_CONFIG_VALUE_5="0",
        )
        if self._explicit_interpreter:
            process_environment["PYTHONNOUSERSITE"] = "1"
        elif importlib.util.find_spec("vfy") is None:
            # A raw checkout needs a source path; installed wheels own their imports.
            process_environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "python")
        if env:
            for key, value in env.items():
                if value is None:
                    process_environment.pop(key, None)
                else:
                    process_environment[key] = value
        return process_environment

    def _run_checked(self, command: Sequence[str]) -> CommandResult:
        result = self._run(command)
        if result.returncode != 0:
            rendered = " ".join(command)
            raise RuntimeError(f"project setup command failed ({rendered}): {result.stderr}")
        return result
