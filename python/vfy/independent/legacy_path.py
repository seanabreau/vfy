"""Legacy temporary-path protocols over resources owned by native fixtures."""

from __future__ import annotations

import io
import os
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import overload, override

from .helpers import TempPathFactory


class LegacyPath:
    """Adapt the file operations exposed by the independent ``tmpdir`` fixture."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(os.path.abspath(path))

    @property
    def strpath(self) -> str:
        return str(self._path)

    @property
    def basename(self) -> str:
        return self._path.name

    def __fspath__(self) -> str:
        return self.strpath

    @override
    def __str__(self) -> str:
        return self.strpath

    @override
    def __repr__(self) -> str:
        return f"local({self.strpath!r})"

    @override
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, (str, os.PathLike)):
            return False
        return os.path.normcase(self.strpath) == os.path.normcase(os.fspath(other))

    @override
    def __hash__(self) -> int:
        return hash(os.path.normcase(self.strpath))

    # The legacy public protocol names its absolute-path keyword ``abs``.
    def join(self, *parts: str | os.PathLike[str], abs: bool = False) -> LegacyPath:  # noqa: A002 - py.path keyword
        path = self._path
        for part in parts:
            text = os.fspath(part)
            if not abs:
                text = text.lstrip(os.sep)
                if os.altsep:
                    text = text.lstrip(os.altsep)
            path = path / text
        return LegacyPath(Path(os.path.normpath(path)))

    def __truediv__(self, other: str | os.PathLike[str]) -> LegacyPath:
        return self.join(other)

    def __add__(self, other: object) -> LegacyPath:
        """Return a sibling whose basename has ``other`` appended."""
        return LegacyPath(self._path.with_name(self._path.name + str(other)))

    def exists(self) -> bool:
        return self._path.exists()

    def check(self, **conditions: object) -> bool:
        """Test ``exists``, ``file``, ``dir`` and ``link``; with none, whether it exists."""
        tests = {
            "exists": self._path.exists,
            "file": self._path.is_file,
            "dir": self._path.is_dir,
            "link": self._path.is_symlink,
        }
        unknown = set(conditions) - set(tests)
        if unknown:
            raise TypeError(f"unsupported check conditions: {sorted(unknown)}")
        if not conditions:
            return self._path.exists()
        return all(tests[name]() == bool(expected) for name, expected in conditions.items())

    def isfile(self) -> bool:
        return self._path.is_file()

    @property
    def dirname(self) -> str:
        return str(self._path.parent)

    @property
    def ext(self) -> str:
        return self._path.suffix

    @property
    def purebasename(self) -> str:
        return self._path.stem

    def listdir(self) -> list[LegacyPath]:
        return [LegacyPath(child) for child in sorted(self._path.iterdir())]

    def ensure(self, *parts: str | os.PathLike[str], dir: bool = False) -> LegacyPath:  # noqa: A002 - legacy keyword
        path = self.join(*parts)
        if dir:
            path._path.mkdir(parents=True, exist_ok=True)
        else:
            path._path.parent.mkdir(parents=True, exist_ok=True)
            path._path.touch()
        return path

    def mkdir(self, *parts: str | os.PathLike[str]) -> LegacyPath:
        path = self.join(*parts)
        path._path.mkdir()
        return path

    def chdir(self) -> LegacyPath | None:
        """Change directory to this path and return the previous one, or ``None`` if it is gone."""
        try:
            previous: LegacyPath | None = LegacyPath(os.getcwd())
        except FileNotFoundError:
            previous = None
        os.chdir(self.strpath)
        return previous

    @contextmanager
    def as_cwd(self) -> Iterator[LegacyPath | None]:
        """Change directory for the managed block and yield the previous one."""
        previous = self.chdir()
        try:
            yield previous
        finally:
            if previous is not None:
                previous.chdir()

    def realpath(self) -> LegacyPath:
        return LegacyPath(os.path.realpath(self.strpath))

    def samefile(self, other: str | os.PathLike[str]) -> bool:
        other_path = os.path.abspath(os.fspath(other))
        return self.strpath == other_path or os.path.samefile(self.strpath, other_path)

    def islink(self) -> bool:
        return self._path.is_symlink()

    def size(self) -> int:
        return self._path.stat().st_size

    def mtime(self) -> float:
        return self._path.stat().st_mtime

    def atime(self) -> float:
        return self._path.stat().st_atime

    def setmtime(self, mtime: float | None = None) -> None:
        """Set the modification time, or the current time when ``mtime`` is ``None``."""
        if mtime is None:
            os.utime(self.strpath, None)
        else:
            os.utime(self.strpath, (self._path.stat().st_atime, mtime))

    def remove(self, rec: int = 1, ignore_errors: bool = False) -> None:
        """Remove a file, link, or directory; ``rec`` removes a directory tree."""
        if self._path.is_dir() and not self._path.is_symlink():
            if rec:
                shutil.rmtree(self.strpath, ignore_errors=ignore_errors)
            else:
                os.rmdir(self.strpath)
        else:
            os.remove(self.strpath)

    def rename(self, target: str | os.PathLike[str]) -> None:
        os.rename(self.strpath, os.fspath(target))

    def readlines(self, cr: int = 1) -> list[str]:
        """Read lines; with ``cr`` false, split on newline and drop the separators."""
        if not cr:
            return str(self.read()).split("\n")
        with self._path.open("r") as stream:
            return stream.readlines()

    def ensure_dir(self, *parts: str | os.PathLike[str]) -> LegacyPath:
        return self.ensure(*parts, dir=True)

    def relto(self, relpath: str | os.PathLike[str]) -> str:
        """Return the path below ``relpath``, or an empty string when it is not below it."""
        prefix = os.fspath(relpath)
        if prefix and prefix[-1] != os.sep:
            prefix += os.sep
        return self.strpath[len(prefix) :] if self.strpath.startswith(prefix) else ""

    def parts(self, reverse: bool = False) -> list[LegacyPath]:
        """Return the ancestors from the root, then this path; ``reverse`` flips the order."""
        ancestors: list[LegacyPath] = [self]
        while ancestors[-1].dirpath() != ancestors[-1]:
            ancestors.append(ancestors[-1].dirpath())
        return ancestors if reverse else ancestors[::-1]

    def isdir(self) -> bool:
        return self._path.is_dir()

    # Same ``abs`` keyword as join().
    def dirpath(self, *parts: str | os.PathLike[str], abs: bool = False) -> LegacyPath:  # noqa: A002 - py.path keyword
        return LegacyPath(self._path.parent).join(*parts, abs=abs)

    def open(
        self, mode: str = "r", ensure: bool = False, encoding: str | None = None
    ) -> io.TextIOBase | io.BufferedIOBase:
        if ensure:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        stream = self._path.open(mode, encoding=encoding)
        assert isinstance(stream, (io.TextIOBase, io.BufferedIOBase))
        return stream

    def write(self, data: object, mode: str = "w", ensure: bool = False) -> None:
        if "b" in mode:
            if not isinstance(data, bytes):
                raise ValueError("can only process bytes")
            value: str | bytes = data
        elif isinstance(data, bytes):
            value = data.decode(sys.getdefaultencoding())
        else:
            value = str(data)
        if ensure:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open(mode) as stream:
            stream.write(value)

    @overload
    def read(self) -> str: ...

    @overload
    def read(self, mode: str) -> str | bytes: ...

    def read(self, mode: str = "r") -> str | bytes:
        with self.open(mode) as stream:
            return stream.read()

    def read_binary(self) -> bytes:
        return self._path.read_bytes()

    read_bytes = read_binary

    def read_text(self, encoding: str) -> str:
        return self._path.read_text(encoding=encoding)

    def write_text(self, data: str, encoding: str, ensure: bool = False) -> None:
        if ensure:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(data, encoding=encoding)

    def write_binary(self, data: bytes, ensure: bool = False) -> None:
        if ensure:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(data)


class LegacyPathFactory:
    """Expose the legacy factory protocol without acquiring another resource."""

    def __init__(self, factory: TempPathFactory) -> None:
        self._factory = factory

    def getbasetemp(self) -> LegacyPath:
        return LegacyPath(self._factory.getbasetemp())

    def mktemp(self, basename: str, numbered: bool = True) -> LegacyPath:
        return LegacyPath(self._factory.mktemp(basename, numbered=numbered))
