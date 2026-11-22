"""CPython stream and descriptor resources controlled by native capture guards."""

from __future__ import annotations

import io
import os
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, ExitStack, suppress
from pathlib import Path
from typing import IO, Literal, Protocol, Self, TextIO, override


def bind_worker_output(directory: str, index: int) -> None:
    """Reopen the fork child's streams over its supervisor-owned output files."""
    stdout_encoding, stdout_errors = sys.stdout.encoding, sys.stdout.errors
    stderr_encoding, stderr_errors = sys.stderr.encoding, sys.stderr.errors
    for descriptor, prefix in ((1, "stdout"), (2, "stderr")):
        fd = os.open(
            Path(directory) / f"{prefix}.{index}", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        try:
            os.dup2(fd, descriptor)
        finally:
            os.close(fd)
    sys.stdout = os.fdopen(
        os.dup(1), "w", encoding=stdout_encoding, errors=stdout_errors, buffering=1
    )
    sys.stderr = os.fdopen(
        os.dup(2), "w", encoding=stderr_encoding, errors=stderr_errors, buffering=1
    )


def _reopen_console_stream(
    stream: TextIO, mode: Literal["rb", "wb"], buffering: int
) -> io.TextIOWrapper:
    with ExitStack() as resources:
        raw = resources.enter_context(open(os.dup(stream.fileno()), mode, buffering))
        wrapper = io.TextIOWrapper(
            raw, stream.encoding, stream.errors, stream.newlines, bool(stream.line_buffering)
        )
        resources.pop_all()
        return wrapper


def _prepare_fd_console() -> None:
    if not sys.platform.startswith("win32") or hasattr(sys, "pypy_version_info"):
        return
    if not hasattr(sys.stdout, "buffer"):
        return
    buffer = sys.stdout.buffer
    raw = getattr(buffer, "raw", buffer)
    if not isinstance(raw, io._WindowsConsoleIO):
        return
    buffering = -1 if hasattr(buffer, "raw") else 0
    with ExitStack() as resources:
        streams = (
            resources.enter_context(_reopen_console_stream(sys.stdin, "rb", -1)),
            resources.enter_context(_reopen_console_stream(sys.stdout, "wb", buffering)),
            resources.enter_context(_reopen_console_stream(sys.stderr, "wb", buffering)),
        )
        sys.stdin, sys.stdout, sys.stderr = streams
        resources.pop_all()


def prepare_stdio(*, fd_capture: bool) -> None:
    """Bind console and readline IO before native descriptor capture starts."""
    if fd_capture:
        _prepare_fd_console()
    if sys.platform.startswith("win32"):
        with suppress(ImportError):
            import colorama  # noqa: PLC0415 - Exercise active import hooks before capture starts.

            del colorama
    with suppress(ImportError):
        import readline  # noqa: PLC0415 - Exercise active import hooks before capture starts.

        del readline


class CaptureOwner(Protocol):
    def suspended(self) -> AbstractContextManager[object]: ...
    def fixture_started(self, resource: object) -> None: ...
    def fixture_stopped(self, resource: object) -> None: ...


class CaptureBuffer(io.TextIOWrapper):
    _vfy_global_capture: CaptureOwner

    def __init__(self, original: IO[str]) -> None:
        self._buffer = io.BytesIO()
        super().__init__(self._buffer, encoding="UTF-8", newline="", write_through=True)
        self._vfy_outer_capture_stream = original

    def getvalue(self) -> str:
        buffer = self.buffer
        assert isinstance(buffer, io.BytesIO)
        return buffer.getvalue().decode("UTF-8")

    def getbytes(self) -> bytes:
        self.flush()
        return self._buffer.getvalue()


class TeeBuffer(CaptureBuffer):
    @override
    def write(self, text: str) -> int:
        super().write(text)
        return self._vfy_outer_capture_stream.write(text)


class SysCaptureIO:
    """Retain stream buffers while native guards suspend and resume redirection."""

    def __init__(self, *, tee: bool = False) -> None:
        self.original = (sys.stdout, sys.stderr)
        buffer = TeeBuffer if tee else CaptureBuffer
        self.streams = (buffer(self.original[0]), buffer(self.original[1]))

    def bind(self, owner: CaptureOwner) -> None:
        for stream in self.streams:
            stream._vfy_global_capture = owner

    def resume(self) -> None:
        sys.stdout, sys.stderr = self.streams

    def suspend(self) -> None:
        sys.stdout, sys.stderr = self.original

    def readouterr(self) -> tuple[str, str]:
        return self._readouterr(CaptureBuffer.getvalue)

    def readouterr_bytes(self) -> tuple[bytes, bytes]:
        return self._readouterr(CaptureBuffer.getbytes)

    def _readouterr[T](self, read: Callable[[CaptureBuffer], T]) -> tuple[T, T]:
        out_stream, err_stream = self.streams
        out = read(out_stream)
        out_stream.seek(0)
        out_stream.truncate()
        err = read(err_stream)
        err_stream.seek(0)
        err_stream.truncate()
        return out, err

    def write_original(self, out: str, err: str) -> None:
        for stream, value in zip(self.original, (out, err), strict=True):
            if value:
                stream.write(value)
                stream.flush()

    def write_original_bytes(self, out: bytes, err: bytes) -> None:
        for stream, value in zip(self.original, (out, err), strict=True):
            if value:
                stream.flush()
                stream.buffer.write(value)
                stream.buffer.flush()

    def close(self) -> None:
        self.suspend()
        for stream in self.streams:
            stream.close()


class FdStream(io.TextIOWrapper):
    _vfy_global_capture: CaptureOwner
    _vfy_outer_capture_stream: IO[str]

    @property
    @override
    def name(self) -> str:
        return repr(self.buffer)

    @property
    @override
    def mode(self) -> str:
        return "r+"


def _native_fd_stream(original: TextIO, fd: int) -> FdStream:
    """Build a stream over a duplicate of the capture file."""
    with ExitStack() as resources:
        raw = resources.enter_context(os.fdopen(os.dup(fd), "wb", buffering=0))
        stream = resources.enter_context(
            FdStream(raw, encoding="UTF-8", errors="replace", newline="", write_through=True)
        )
        stream._vfy_outer_capture_stream = original
        resources.pop_all()
        return stream


class FdCaptureIO:
    """Own saved descriptors and files without choosing capture boundaries."""

    def __init__(self) -> None:
        self.original = (sys.stdout, sys.stderr)
        for stream in self.original:
            stream.flush()
        with ExitStack() as resources:
            saved: list[int] = []
            for fd in (1, 2):
                saved.append(os.dup(fd))
                resources.callback(os.close, saved[-1])
            self._saved = (saved[0], saved[1])
            self._files: tuple[IO[bytes], IO[bytes]] = (
                resources.enter_context(tempfile.TemporaryFile(mode="w+b")),
                resources.enter_context(tempfile.TemporaryFile(mode="w+b")),
            )
            streams: list[FdStream] = []
            for original, file in zip(self.original, self._files, strict=True):
                raw = resources.enter_context(os.fdopen(os.dup(file.fileno()), "wb", buffering=0))
                stream = resources.enter_context(
                    FdStream(
                        raw, encoding="UTF-8", errors="replace", newline="", write_through=True
                    )
                )
                stream._vfy_outer_capture_stream = original
                streams.append(stream)
            self.streams = (streams[0], streams[1])
            self._resources: ExitStack | None = resources.pop_all()

    def bind(self, owner: CaptureOwner) -> None:
        for stream in self.streams:
            stream._vfy_global_capture = owner

    def resume(self) -> None:
        try:
            for fd, file in zip((1, 2), self._files, strict=True):
                os.dup2(file.fileno(), fd)
            sys.stdout, sys.stderr = self.streams
        except BaseException:
            self.suspend()
            raise

    def suspend(self) -> None:
        # ExitStack attempts every restoration even when a caller stream fails.
        with ExitStack() as restore:
            for fd, saved in zip((1, 2), self._saved, strict=True):
                restore.callback(os.dup2, saved, fd)
            restore.callback(self._restore_streams)
            for stream in self.streams:
                stream.flush()

    def _restore_streams(self) -> None:
        sys.stdout, sys.stderr = self.original

    def readouterr(self) -> tuple[str, str]:
        out, err = self.readouterr_bytes()
        return out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")

    def readouterr_bytes(self) -> tuple[bytes, bytes]:
        for stream in self.streams:
            stream.flush()
        values: list[bytes] = []
        for file in self._files:
            file.seek(0)
            data = file.read()
            values.append(data)
            if data:
                file.seek(0)
                file.truncate()
        return values[0], values[1]

    def write_original(self, out: str, err: str) -> None:
        self.write_original_bytes(out.encode("utf-8"), err.encode("utf-8"))

    def write_original_bytes(self, out: bytes, err: bytes) -> None:
        for value, fd in zip((out, err), self._saved, strict=True):
            remaining = memoryview(value)
            while remaining:
                written = os.write(fd, remaining)
                if written == 0:
                    raise OSError("capture output write made no progress")
                remaining = remaining[written:]

    def close(self) -> None:
        resources, self._resources = self._resources, None
        if resources is not None:
            try:
                self.suspend()
            finally:
                resources.close()


class UnreadableInput(TextIO):
    """Match the pytest input stream protocol while output capture is active."""

    @property
    @override
    def encoding(self) -> str | None:
        return sys.__stdin__.encoding if sys.__stdin__ is not None else None

    @override
    def read(self, size: int = -1) -> str:
        raise OSError("pytest: reading from stdin while output is captured!  Consider using `-s`.")

    readline = read

    @override
    def readlines(self, hint: int | None = -1) -> list[str]:
        self.read()
        return []

    @override
    def __iter__(self) -> Iterator[str]:
        return self

    @override
    def __next__(self) -> str:
        return self.read()

    @override
    def fileno(self) -> int:
        raise io.UnsupportedOperation("redirected stdin is pseudofile, has no fileno()")

    @override
    def flush(self) -> None:
        raise io.UnsupportedOperation("redirected stdin is pseudofile, has no flush()")

    @override
    def isatty(self) -> bool:
        return False

    @override
    def close(self) -> None:
        pass

    @override
    def readable(self) -> bool:
        return False

    @override
    def seek(self, offset: int, whence: int = 0) -> int:
        raise io.UnsupportedOperation("redirected stdin is pseudofile, has no seek(int)")

    @override
    def seekable(self) -> bool:
        return False

    @override
    def tell(self) -> int:
        raise io.UnsupportedOperation("redirected stdin is pseudofile, has no tell()")

    @override
    def truncate(self, size: int | None = None) -> int:
        raise io.UnsupportedOperation("cannot truncate stdin")

    @override
    def write(self, data: str) -> int:
        raise io.UnsupportedOperation("cannot write to stdin")

    @override
    def writelines(self, lines: Iterable[str]) -> None:
        raise io.UnsupportedOperation("Cannot write to stdin")

    @override
    def writable(self) -> bool:
        return False

    @override
    def __enter__(self) -> Self:
        return self

    @override
    def __exit__(self, *args: object) -> None:
        pass

    @property
    @override
    def buffer(self) -> Self:
        return self


class InputCaptureIO:
    """Own input replacement and optional descriptor redirection to devnull."""

    def __init__(self, *, redirect_fd: bool) -> None:
        self._original = sys.stdin
        self._stream = UnreadableInput()
        self._saved: int | None = None
        self._null: IO[bytes] | None = None
        with ExitStack() as resources:
            if redirect_fd:
                self._saved = os.dup(0)
                resources.callback(os.close, self._saved)
                self._null = resources.enter_context(open(os.devnull, "rb"))
            self._resources: ExitStack | None = resources.pop_all()

    def start(self) -> None:
        try:
            if self._null is not None:
                os.dup2(self._null.fileno(), 0)
            sys.stdin = self._stream
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        resources, self._resources = self._resources, None
        if resources is not None:
            try:
                sys.stdin = self._original
                if self._saved is not None:
                    os.dup2(self._saved, 0)
            finally:
                resources.close()
