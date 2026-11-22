"""Collector identities and parent chains."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from .protocol import (
    Collector,
    File,
    Function as Function,
    _NativeNode,
)

if TYPE_CHECKING:
    from vfy._pytest_native import NativeCollectionArena


class Directory(_NativeNode, Collector):
    """A filesystem directory in the native collection parent chain."""


class Dir(Directory):
    """A directory without package collection semantics."""


class Package(Directory):
    """A directory containing a Python package initializer."""


class PyCollector(Collector):
    """The ``python_functions`` and ``python_classes`` filters plug-ins call."""

    def funcnamefilter(self, name: str) -> bool:
        return self._vfy_matches(name, "python_functions")

    def classnamefilter(self, name: str) -> bool:
        return self._vfy_matches(name, "python_classes")

    def istestfunction(self, obj: object, name: str) -> bool:
        if not self.funcnamefilter(name):
            return False
        if isinstance(obj, staticmethod | classmethod):
            obj = obj.__func__
        return callable(obj) and not hasattr(obj, "__vfy_fixture__")

    def istestclass(self, obj: object, name: str) -> bool:
        return self.classnamefilter(name) and getattr(obj, "__test__", True) is not False

    def _vfy_matches(self, name: str, option: str) -> bool:
        from vfy import native  # noqa: PLC0415 - the native module imports this protocol

        patterns = cast("list[str]", self.config.getini(option))
        return native._pytest.independent_matches_python_name(name, patterns)


class Module(_NativeNode, File, PyCollector):
    """Import the module through the same native owner used by collection."""

    _ALLOW_MARKERS = True
    _vfy_arena: NativeCollectionArena

    @property
    def obj(self) -> object:
        return self._vfy_arena.module_object(self)

    @obj.setter
    def obj(self, value: object) -> None:
        self._vfy_arena.set_module_object(self, value)


class Class(_NativeNode, PyCollector):
    """Hold the actual Python class before its methods are collected."""

    _obj: object

    @property
    def obj(self) -> object:
        return self._obj

    @obj.setter
    def obj(self, value: object) -> None:
        self._obj = value

    def newinstance(self) -> object:
        return cast(Callable[[], object], self.obj)()


class FunctionDefinition(Function):
    """Expose the definition and real parent used by parameter generation."""


# The builtin ``pytest_collect_file`` result that stands for the Python module
# the native collector creates for a file matching ``python_files``.
PYTHON_FILE = object()


def _collect_python_file(file_path: object, parent: object) -> object:
    return PYTHON_FILE


def _collect_no_file(file_path: object, parent: object) -> None:
    return None
