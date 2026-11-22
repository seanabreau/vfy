"""The ``mocker`` fixtures when the pytest-mock distribution is absent.

The public API matches pytest-mock 3.x: ``MockerFixture`` patches through
``unittest.mock`` and undoes every patch at the end of the fixture's scope.
"""

from __future__ import annotations

import builtins
import functools
import inspect
import itertools
import unittest.mock
from collections.abc import Callable, Generator as Generator, Iterable, Iterator, Mapping
from typing import Any, Literal as Literal, cast

import pytest as pytest

from . import mock_fixtures as _mock_fixtures
from .mock_fixtures import (
    _fixture as _fixture,
    _mocker as _mocker,
    class_mocker as class_mocker,
    mocker as mocker,
    module_mocker as module_mocker,
    package_mocker as package_mocker,
    pytest_addoption as pytest_addoption,
    session_mocker as session_mocker,
)

Mock = unittest.mock.Mock
MagicMock = unittest.mock.MagicMock
NonCallableMock = unittest.mock.NonCallableMock
NonCallableMagicMock = unittest.mock.NonCallableMagicMock
PropertyMock = unittest.mock.PropertyMock
AsyncMock = unittest.mock.AsyncMock
call = unittest.mock.call
ANY = unittest.mock.ANY
DEFAULT = unittest.mock.DEFAULT
sentinel = unittest.mock.sentinel
mock_open = unittest.mock.mock_open
seal = unittest.mock.seal
create_autospec = unittest.mock.create_autospec

__all__ = (
    "AsyncMockType",
    "MockType",
    "MockerFixture",
    "PytestMockWarning",
    "class_mocker",
    "mocker",
    "module_mocker",
    "package_mocker",
    "session_mocker",
)

MockType = unittest.mock.MagicMock | unittest.mock.AsyncMock | unittest.mock.NonCallableMagicMock
AsyncMockType = unittest.mock.AsyncMock


class PytestMockWarning(UserWarning):
    """Base class for warnings this fixture emits."""


class MockerFixture:
    """Patch objects for one fixture scope and undo the patches afterwards."""

    Mock = unittest.mock.Mock
    MagicMock = unittest.mock.MagicMock
    NonCallableMock = unittest.mock.NonCallableMock
    NonCallableMagicMock = unittest.mock.NonCallableMagicMock
    PropertyMock = unittest.mock.PropertyMock
    AsyncMock = unittest.mock.AsyncMock
    call = unittest.mock.call
    ANY = unittest.mock.ANY
    DEFAULT = unittest.mock.DEFAULT
    sentinel = unittest.mock.sentinel
    mock_open = unittest.mock.mock_open
    seal = unittest.mock.seal

    def __init__(self, config: object) -> None:
        self._patches: list[tuple[Any, MockType]] = []
        self.patch = self._Patcher(self._patches)
        self.mock_module = unittest.mock

    def create_autospec(
        self, spec: object, spec_set: bool = False, instance: bool = False, **kwargs: object
    ) -> MockType:
        """Create an autospecced mock that ``resetall`` also resets."""
        mock = create_autospec(spec, spec_set, instance, **cast("dict[str, Any]", kwargs))
        self._patches.append((None, mock))
        return cast("MockType", mock)

    def resetall(self, *, return_value: bool = False, side_effect: bool = False) -> None:
        """Reset every mock this fixture created."""
        for _, mock in self._patches:
            if hasattr(mock, "reset_mock"):
                if hasattr(mock, "spy_return_list"):
                    mock.spy_return_list = []
                if hasattr(mock, "spy_return_iter"):
                    mock.spy_return_iter = None
                if isinstance(mock, unittest.mock.NonCallableMock):
                    mock.reset_mock(return_value=return_value, side_effect=side_effect)
                else:
                    mock.reset_mock()

    def stopall(self) -> None:
        """Undo every active patch in reverse order."""
        for patcher, _ in reversed(self._patches):
            if patcher is not None:
                patcher.stop()
        self._patches.clear()

    def stop(self, mock: unittest.mock.MagicMock) -> None:
        """Undo the patch that created ``mock``."""
        for index, (patcher, patched) in enumerate(self._patches):
            if patched is mock and patcher is not None:
                patcher.stop()
                del self._patches[index]
                return
        raise ValueError("This mock object is not registered")

    def spy(self, obj: object, name: str, duplicate_iterators: bool = False) -> MockType:
        """Wrap ``obj.name`` so calls reach the original and are recorded."""
        method = getattr(obj, name)
        autospec = inspect.ismethod(method) or inspect.isfunction(method)
        if inspect.isclass(obj):
            raw = inspect.getattr_static(obj, name)
            if isinstance(raw, classmethod | staticmethod):
                autospec = False

        def record(result: object) -> object:
            spy_obj.spy_return = result
            spy_obj.spy_return_list.append(result)
            return result

        def failure(error: BaseException) -> None:
            spy_obj.spy_exception = error

        if inspect.iscoroutinefunction(method):

            @functools.wraps(method)
            async def wrapped(*args: object, **kwargs: object) -> object:
                spy_obj.spy_return = None
                spy_obj.spy_exception = None
                try:
                    result = await method(*args, **kwargs)
                except BaseException as error:
                    failure(error)
                    raise
                return record(result)

        else:

            @functools.wraps(method)
            def wrapped(*args: object, **kwargs: object) -> object:
                spy_obj.spy_return = None
                spy_obj.spy_exception = None
                try:
                    result = method(*args, **kwargs)
                except BaseException as error:
                    failure(error)
                    raise
                if duplicate_iterators and isinstance(result, Iterator):
                    result, spy_obj.spy_return_iter = itertools.tee(result)
                else:
                    spy_obj.spy_return_iter = None
                return record(result)

        spy_obj = self.patch.object(obj, name, side_effect=wrapped, autospec=autospec)
        spy_obj.spy_return = None
        spy_obj.spy_return_iter = None
        spy_obj.spy_return_list = []
        spy_obj.spy_exception = None
        return spy_obj

    def stub(self, name: str | None = None) -> unittest.mock.MagicMock:
        """Return a callable mock that accepts any arguments."""
        return unittest.mock.MagicMock(spec=lambda *args, **kwargs: None, name=name)

    def async_stub(self, name: str | None = None) -> unittest.mock.AsyncMock:
        """Return an awaitable mock that accepts any arguments."""
        return unittest.mock.AsyncMock(spec=lambda *args, **kwargs: None, name=name)

    class _Patcher:
        """``mocker.patch`` and its ``object``, ``multiple``, and ``dict`` forms."""

        def __init__(self, patches: list[tuple[Any, MockType]]) -> None:
            self._patches = patches

        def _start(self, patcher: builtins.object) -> MockType:
            mocked = cast("MockType", cast("Any", patcher).start())
            self._patches.append((patcher, mocked))
            return mocked

        def object(
            self,
            target: builtins.object,
            attribute: str,
            *args: builtins.object,
            **kwargs: builtins.object,
        ) -> MockType:
            """Patch ``target.attribute``."""
            patch_object = cast("Callable[..., Any]", unittest.mock.patch.object)
            return self._start(patch_object(target, attribute, *args, **kwargs))

        def context_manager(
            self,
            target: builtins.object,
            attribute: str,
            *args: builtins.object,
            **kwargs: builtins.object,
        ) -> MockType:
            """Patch ``target.attribute`` without the context-manager warning."""
            return self.object(target, attribute, *args, **kwargs)

        def multiple(
            self, target: builtins.object, *args: builtins.object, **kwargs: builtins.object
        ) -> MockType:
            """Patch several attributes of ``target``."""
            patch_multiple = cast("Callable[..., Any]", unittest.mock.patch.multiple)
            return self._start(patch_multiple(target, *args, **kwargs))

        def dict(
            self,
            in_dict: builtins.object,
            values: Mapping[builtins.object, builtins.object]
            | Iterable[tuple[builtins.object, builtins.object]] = (),
            clear: bool = False,
            **kwargs: builtins.object,
        ) -> MockType:
            """Patch a mapping for the fixture scope."""
            return self._start(unittest.mock.patch.dict(in_dict, values, clear, **kwargs))

        def __call__(
            self, target: str, *args: builtins.object, **kwargs: builtins.object
        ) -> MockType:
            """Patch the object named by ``target``."""
            patch = cast("Callable[..., Any]", unittest.mock.patch)
            return self._start(patch(target, *args, **kwargs))


vars(_mock_fixtures)["MockerFixture"] = MockerFixture
