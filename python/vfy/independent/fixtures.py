"""Fixture declaration identities and Python descriptor bindings.

The native collector unwraps definitions when it builds its fixture bindings.
Calling a declaration directly never executes its underlying fixture function.
"""

from __future__ import annotations

import inspect
import warnings
from collections.abc import Callable, Iterable
from dataclasses import InitVar, dataclass
from functools import update_wrapper
from typing import TYPE_CHECKING, NoReturn, TypedDict, overload, override

if TYPE_CHECKING:
    from .protocol import Config

type Scope = str | Callable[[str, Config], str]
type FixtureIds = tuple[object, ...] | Callable[[object], object] | None


class FixtureMetadata(TypedDict):
    scope: Scope
    autouse: bool
    name: str | None
    params: tuple[object, ...] | None
    ids: FixtureIds


def _private_api(ispytest: bool) -> None:
    if not ispytest:
        # The facade owns this warning identity; defer the import to avoid the
        # facade's fixture-declaration import cycle.
        from .facade import (  # noqa: PLC0415 - declaration cycle
            PytestDeprecationWarning,
        )

        warnings.warn(
            "A private pytest class or function was used.",
            PytestDeprecationWarning,
            stacklevel=3,
        )


@dataclass(slots=True)
class FuncFixtureInfo:
    """Fixture names a plug-in passes when it builds a ``Function`` item."""

    argnames: tuple[str, ...]
    initialnames: tuple[str, ...]
    names_closure: list[str]
    name2fixturedefs: dict[str, object]


@dataclass(frozen=True)
class FixtureFunctionMarker:
    """Carry declaration arguments until applied to a fixture function."""

    scope: Scope
    params: tuple[object, ...] | None
    autouse: bool = False
    ids: FixtureIds = None
    name: str | None = None
    _ispytest: InitVar[bool] = False

    def __post_init__(self, _ispytest: bool) -> None:
        _private_api(_ispytest)

    def __call__(self, function: Callable[..., object]) -> FixtureFunctionDefinition:
        if inspect.isclass(function):
            raise ValueError("class fixtures not supported (maybe in the future)")
        if isinstance(function, FixtureFunctionDefinition):
            raise ValueError(
                "@pytest.fixture is being applied more than once to the same function "
                f"{function.__name__!r}"
            )
        if hasattr(function, "pytestmark"):
            reject_fixture_marks()
        definition = FixtureFunctionDefinition(
            function=function, fixture_function_marker=self, _ispytest=True
        )
        if definition.name == "request":
            from .facade import fail  # noqa: PLC0415 - declaration cycle

            filename = inspect.getsourcefile(function) or inspect.getfile(function)
            line = inspect.getsourcelines(function)[1]
            fail(
                "'request' is a reserved word for fixtures, use another name:\n"
                f"  {filename}:{line}",
                pytrace=False,
            )
        return definition


class FixtureFunctionDefinition:
    """A declared fixture with the wrapped function's descriptor protocol."""

    def __init__(
        self,
        *,
        function: Callable[..., object],
        fixture_function_marker: FixtureFunctionMarker,
        instance: object | None = None,
        _ispytest: bool = False,
    ) -> None:
        _private_api(_ispytest)
        # Callable instances may implement the function protocol dynamically.
        self.name = fixture_function_marker.name or getattr(function, "__name__")  # noqa: B009 - callable instances define this dynamically
        self.__name__: str = self.name
        self._fixture_function_marker = fixture_function_marker
        self._fixture_function: Callable[..., object] = (
            getattr(function, "__get__")(instance)  # noqa: B009 - callable descriptor protocol
            if instance is not None
            else function
        )
        update_wrapper(self, function)

    @property
    def __vfy_fixture__(self) -> FixtureMetadata:
        marker = self._fixture_function_marker
        return {
            "scope": marker.scope,
            "autouse": marker.autouse,
            "name": marker.name,
            "params": marker.params,
            "ids": marker.ids,
        }

    @override
    def __repr__(self) -> str:
        return f"<pytest_fixture({self._fixture_function})>"

    def __get__(
        self, instance: object | None, owner: type[object] | None = None
    ) -> FixtureFunctionDefinition:
        return FixtureFunctionDefinition(
            function=self._fixture_function,
            fixture_function_marker=self._fixture_function_marker,
            instance=instance,
            _ispytest=True,
        )

    def __call__(self, *args: object, **kwargs: object) -> NoReturn:
        from .facade import fail  # noqa: PLC0415 - declaration cycle

        fail(
            f'Fixture "{self.name}" called directly. '
            "Fixtures are not meant to be called directly,\n"
            "but are created automatically when test functions request them as parameters.\n"
            "See https://docs.pytest.org/en/stable/explanation/fixtures.html "
            "for more information about fixtures, and\n"
            "https://docs.pytest.org/en/stable/deprecations.html#calling-fixtures-directly",
            pytrace=False,
        )

    def _get_wrapped_function(self) -> Callable[..., object]:
        return self._fixture_function


def reject_fixture_marks() -> NoReturn:
    from .facade import fail  # noqa: PLC0415 - declaration cycle

    fail(
        "Marks cannot be applied to fixtures.\n"
        "See docs: https://docs.pytest.org/en/stable/deprecations.html#applying-a-mark-to-a-fixture-function"
    )


@overload
def fixture(
    fixture_function: Callable[..., object],
    *,
    scope: Scope = "function",
    params: Iterable[object] | None = None,
    autouse: bool = False,
    ids: Iterable[object] | Callable[[object], object] | None = None,
    name: str | None = None,
) -> FixtureFunctionDefinition: ...


@overload
def fixture(
    fixture_function: None = None,
    *,
    scope: Scope = "function",
    params: Iterable[object] | None = None,
    autouse: bool = False,
    ids: Iterable[object] | Callable[[object], object] | None = None,
    name: str | None = None,
) -> FixtureFunctionMarker: ...


def fixture(
    fixture_function: Callable[..., object] | None = None,
    *,
    scope: Scope = "function",
    params: Iterable[object] | None = None,
    autouse: bool = False,
    ids: Iterable[object] | Callable[[object], object] | None = None,
    name: str | None = None,
) -> FixtureFunctionDefinition | FixtureFunctionMarker:
    """Declare a fixture while keeping its executable function in a definition."""
    marker = FixtureFunctionMarker(
        scope=scope,
        params=None if params is None else tuple(params),
        autouse=autouse,
        ids=ids if ids is None or callable(ids) else tuple(ids),
        name=name,
        _ispytest=True,
    )
    return marker if fixture_function is None else marker(fixture_function)
