"""CPython resource adapters used by independent-runner builtin fixtures."""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import warnings
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import IO, TYPE_CHECKING, NamedTuple

from .compat import NOTSET
from .stdio import CaptureOwner

if TYPE_CHECKING:
    from vfy._pytest_native import NativeMonkeyPatchState


def _annotated_attribute(owner: object, name: str, path: str) -> object:
    try:
        return getattr(owner, name)
    except AttributeError as error:
        raise AttributeError(
            f"{type(owner).__name__!r} object at {path} has no attribute {name!r}"
        ) from error


def _attribute_target(target: str, *, raising: bool) -> tuple[object, str]:
    if "." not in target:
        raise TypeError(f"must be absolute import path string, not {target!r}")
    owner_path, attribute = target.rsplit(".", 1)
    components = owner_path.split(".")
    owner: object = importlib.import_module(components[0])
    for index, component in enumerate(components[1:], 1):
        try:
            owner = getattr(owner, component)
            continue
        except AttributeError:
            pass
        import_path = ".".join(components[: index + 1])
        try:
            importlib.import_module(import_path)
        except ImportError as error:
            if str(error).split()[-1] == import_path:
                raise
            raise ImportError(f"import error in {import_path}: {error}") from error
        owner = _annotated_attribute(owner, component, import_path)
    if raising:
        _annotated_attribute(owner, attribute, owner_path)
    return owner, attribute


class MonkeyPatch:
    """Adapt Python mutation protocols to the native reversible journals."""

    def __init__(self) -> None:
        # The facade imports this adapter before constructing native fixtures.
        from vfy.native import _pytest  # noqa: PLC0415 - facade/bootstrap import cycle

        self._state: NativeMonkeyPatchState = _pytest.NativeMonkeyPatchState()

    @property
    def _setattr(self) -> list[tuple[object, object, object]]:
        return self._state.attributes

    @_setattr.setter
    def _setattr(self, value: list[tuple[object, object, object]]) -> None:
        self._state.attributes = value

    @property
    def _setitem(self) -> list[tuple[object, object, object]]:
        return self._state.items

    @_setitem.setter
    def _setitem(self, value: list[tuple[object, object, object]]) -> None:
        self._state.items = value

    def setattr(
        self,
        target: object,
        name: str | object,
        value: object = NOTSET,
        raising: bool = True,
    ) -> None:
        if value is NOTSET:
            if not isinstance(target, str):
                raise TypeError(
                    "use setattr(target, name, value) or setattr(target, value) "
                    "with target being a dotted import string"
                )
            target, attribute = _attribute_target(target, raising=raising)
            value = name
        elif isinstance(name, str):
            attribute = name
        else:
            raise TypeError(
                "use setattr(target, name, value) with name being a string or "
                "setattr(target, value) with target being a dotted import string"
            )
        self._state.setattr(target, attribute, value, raising)

    def delattr(self, target: object, name: object = NOTSET, raising: bool = True) -> None:
        if name is NOTSET:
            if not isinstance(target, str):
                raise TypeError(
                    "use delattr(target, name) or delattr(target) "
                    "with target being a dotted import string"
                )
            target, name = _attribute_target(target, raising=raising)
        self._state.delattr(target, name, raising)

    def setitem[K, V](self, mapping: Mapping[K, V], name: K, value: V) -> None:
        self._state.setitem(mapping, name, value)

    def delitem[K, V](self, mapping: Mapping[K, V], name: K, raising: bool = True) -> None:
        self._state.delitem(mapping, name, raising)

    def setenv(self, name: str, value: str, prepend: str | None = None) -> None:
        if not isinstance(value, str):
            from .facade import PytestWarning  # noqa: PLC0415 - facade bootstrap cycle

            warnings.warn(
                PytestWarning(
                    f"Value of environment variable {name} type should be str, but got "
                    f"{value!r} (type: {type(value).__name__}); converted to str implicitly"
                ),
                stacklevel=2,
            )
            value = str(value)
        if prepend and name in os.environ:
            value = value + prepend + os.environ[name]
        self.setitem(os.environ, name, value)

    def delenv(self, name: str, raising: bool = True) -> None:
        self.delitem(os.environ, name, raising=raising)

    def syspath_prepend(self, path: str | os.PathLike[str]) -> None:
        self._state.syspath_prepend(path)

    def chdir(self, path: str | os.PathLike[str]) -> None:
        self._state.chdir(path)

    def undo(self) -> None:
        self._state.undo()

    @classmethod
    @contextmanager
    def context(cls) -> Generator[MonkeyPatch]:
        instance = cls()
        try:
            yield instance
        finally:
            instance.undo()


class TempPathFactory:
    """Allocate temporary paths below an explicit or pytest-style default base.

    Without ``basetemp``, the base is a new numbered directory below
    ``pytest-of-{user}``, locked while this session runs; a retention count
    of 0 removes it when the session finishes.
    """

    def __init__(self, basetemp: str | None = None, retention_count: int = 3) -> None:
        from vfy.native import _pytest  # noqa: PLC0415 - facade/bootstrap import cycle

        if basetemp is None:
            self._base, self._lock = _pytest.independent_default_basetemp(retention_count)
        else:
            self._base, self._lock = Path(basetemp), None
        self._keep = basetemp is not None or retention_count != 0

    def getbasetemp(self) -> Path:
        return self._base

    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        from vfy.native import _pytest  # noqa: PLC0415 - facade/bootstrap import cycle

        return _pytest.independent_temporary_path(self.getbasetemp(), basename, numbered)

    def cleanup(self) -> None:
        if self._lock is not None:
            self._lock.unlink(missing_ok=True)
        if not self._keep:
            shutil.rmtree(self._base, ignore_errors=True)


class CaptureResult[T](NamedTuple):
    out: T
    err: T


@contextmanager
def _capture_disabled(
    stdout: IO[str], stderr: IO[str], owner: CaptureOwner | None
) -> Generator[None]:
    if owner is not None:
        with owner.suspended():
            yield
        return
    previous = sys.stdout, sys.stderr
    sys.stdout = getattr(stdout, "_vfy_outer_capture_stream", stdout)
    sys.stderr = getattr(stderr, "_vfy_outer_capture_stream", stderr)
    try:
        yield
    finally:
        sys.stdout, sys.stderr = previous


class WarningsRecorder:
    def __init__(self) -> None:
        self._context = warnings.catch_warnings(record=True)
        self.list = self._context.__enter__()
        warnings.simplefilter("always")

    def __iter__(self) -> Iterator[warnings.WarningMessage]:
        return iter(self.list)

    def __len__(self) -> int:
        return len(self.list)

    def __getitem__(self, index: int) -> warnings.WarningMessage:
        return self.list[index]

    def pop(self, cls: type[Warning] = Warning) -> warnings.WarningMessage:
        """Pop the exact-category match, else the most specific subclass match of ``cls``."""
        __tracebackhide__ = True
        best: int | None = None
        for index, record in enumerate(self.list):
            if record.category == cls:
                return self.list.pop(index)
            if issubclass(record.category, cls) and (
                best is None or not issubclass(record.category, self.list[best].category)
            ):
                best = index
        if best is not None:
            return self.list.pop(best)
        raise AssertionError(f"{cls!r} not found in warning list")

    def clear(self) -> None:
        self.list.clear()

    def close(self) -> None:
        self._context.__exit__(None, None, None)


def create_builtin(name: str, root: str) -> tuple[object, Callable[[], None]] | None:
    """Acquire a resource that the native fixture engine requests from Python.

    The temporary-path and capture fixtures are owned by their native providers.
    """
    if name == "monkeypatch":
        from .facade import (  # noqa: PLC0415 - facade imports this helper base
            MonkeyPatch as FixtureMonkeyPatch,
        )

        patch = FixtureMonkeyPatch()
        return patch, patch.undo
    if name == "recwarn":
        recorder = WarningsRecorder()
        return recorder, recorder.close
    return None
