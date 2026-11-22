"""Execute async callables while bridging pytest's synchronous fixture context.

Runner lifetime and fixture finalizer ordering belong to the caller. Each fixture
owns a task context so teardown sees its setup values after other fixtures run.
"""

from __future__ import annotations

import contextvars
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from typing import TYPE_CHECKING, Any, cast, overload

if TYPE_CHECKING:
    import asyncio


def run_async_test(
    runner: asyncio.Runner,
    function: Callable[..., Coroutine[Any, Any, object]],
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
    *,
    context: contextvars.Context | None = None,
) -> None:
    """Await a test in the supplied runner without changing the caller's context."""
    task_context = contextvars.copy_context() if context is None else context
    runner.run(function(*args, **kwargs), context=task_context)


class _FixtureContext:
    def __init__(self) -> None:
        self.task = contextvars.copy_context()
        self.restore: list[contextvars.Token[Any]] = []

    def publish(self) -> None:
        """Make setup's values visible to dependent fixtures and the test body."""
        caller = contextvars.copy_context()
        for variable, value in self.task.items():
            if variable not in caller or caller[variable] is not value:
                self.restore.append(variable.set(value))

    def reset(self) -> None:
        """Undo this fixture's writes in the reverse order of publication."""
        while self.restore:
            token = self.restore.pop()
            token.var.reset(token)


@overload
def run_async_fixture[T](
    runner: asyncio.Runner,
    function: Callable[..., AsyncIterator[T]],
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> tuple[T, Callable[[], None] | None]: ...


@overload
def run_async_fixture[T](
    runner: asyncio.Runner,
    function: Callable[..., Awaitable[T]],
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> tuple[T, Callable[[], None] | None]: ...


def run_async_fixture(
    runner: asyncio.Runner,
    function: Callable[..., object],
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> tuple[object, Callable[[], None] | None]:
    """Set up an async fixture and return its synchronous teardown operation."""
    generator: AsyncIterator[object] | None = None
    context = _FixtureContext()
    if inspect.isasyncgenfunction(function):
        generator = cast(AsyncIterator[object], function(*args, **kwargs))
        pending = anext(generator)
    elif inspect.iscoroutinefunction(function):
        pending = cast(Awaitable[object], function(*args, **kwargs))
    else:
        raise TypeError("Expected an async fixture callable")

    async def acquire() -> object:
        return await pending

    value = runner.run(acquire(), context=context.task)
    context.publish()

    def release() -> None:
        async def advance() -> None:
            assert generator is not None
            try:
                await anext(generator)
            except StopAsyncIteration:
                return
            raise ValueError("Async fixture yielded more than once")

        try:
            if generator is not None:
                runner.run(advance(), context=context.task)
        finally:
            context.reset()

    cleanup = release if generator is not None or context.restore else None
    return value, cleanup
