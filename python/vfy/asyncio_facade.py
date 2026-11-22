"""Async fixture declarations and item predicates without activating a test plugin."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any, Literal, TypedDict, Unpack, cast, overload

if TYPE_CHECKING:
    import pytest
    from _pytest.fixtures import FixtureFunctionDefinition

    from .asyncio_plugin import _AsyncTestAdapter

_ScopeName = Literal["function", "class", "module", "package", "session"]

# Collection and the public predicate share an identity; no session state is stored here.
_ASYNC_TEST_ADAPTER = cast("pytest.StashKey[type[_AsyncTestAdapter]]", object())


class FixtureOptions(TypedDict, total=False):
    scope: _ScopeName | Callable[[str, pytest.Config], _ScopeName]
    params: Iterable[object] | None
    autouse: bool
    ids: Sequence[object | None] | Callable[[Any], object | None] | None
    name: str | None


@overload
def fixture(
    fixture_function: Callable[..., object],
    loop_scope: _ScopeName | None = None,
    **kwargs: Unpack[FixtureOptions],
) -> FixtureFunctionDefinition: ...


@overload
def fixture(
    fixture_function: None = None,
    loop_scope: _ScopeName | None = None,
    **kwargs: Unpack[FixtureOptions],
) -> Callable[[Callable[..., object]], FixtureFunctionDefinition]: ...


def fixture(
    fixture_function: Callable[..., object] | None = None,
    loop_scope: _ScopeName | None = None,
    **kwargs: Unpack[FixtureOptions],
) -> FixtureFunctionDefinition | Callable[[Callable[..., object]], FixtureFunctionDefinition]:
    """Declare a fixture whose setup and teardown use a scoped async runner."""

    def decorate(function: Callable[..., object]) -> FixtureFunctionDefinition:
        import pytest  # noqa: PLC0415 - resolve the active runner only for fixture declaration

        metadata = getattr(function, "__func__", function)
        # The native fixture planner reads these compatibility attributes too.
        for attribute, value in (("_force_asyncio_fixture", True), ("_loop_scope", loop_scope)):
            setattr(metadata, attribute, value)
        return pytest.fixture(function, **kwargs)

    return decorate if fixture_function is None else decorate(fixture_function)


async_fixture = fixture


def is_async_test(item: pytest.Item) -> bool:
    """Whether vfy claimed this item during collection."""
    import pytest  # noqa: PLC0415 - item checks use the active runner's Function type

    return isinstance(item, pytest.Function) and _ASYNC_TEST_ADAPTER in item.stash
