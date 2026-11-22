"""Shared stdlib loop and runner lifetime adapters for both native frontends."""

from __future__ import annotations

import contextlib
import functools
import sys
import warnings
from collections.abc import Callable, Generator, Iterator
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import asyncio

type LoopFactory = Callable[[], "asyncio.AbstractEventLoop"]


def _sync_metadata[**P, T](
    function: Callable[P, T], source: Callable[..., object]
) -> Callable[P, T]:
    functools.update_wrapper(function, source)
    function.__dict__.pop("_is_coroutine_marker", None)
    return function


def _policy() -> asyncio.AbstractEventLoopPolicy:
    import asyncio  # noqa: PLC0415 - public decorator imports do not initialize asyncio

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        # Policy access preserves pytest's supported policy overrides through Python 3.15.
        return asyncio.get_event_loop_policy()  # ty: ignore[deprecated]


def _policy_loop(policy: asyncio.AbstractEventLoopPolicy) -> asyncio.AbstractEventLoop | None:
    state = getattr(policy, "_local", None)
    if state is not None and hasattr(state, "_loop"):
        return cast("asyncio.AbstractEventLoop | None", state._loop)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            return policy.get_event_loop()
        except RuntimeError:
            return None


@contextlib.contextmanager
def _temporary_event_loop(loop: asyncio.AbstractEventLoop) -> Iterator[None]:
    policy = _policy()
    previous = _policy_loop(policy)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        policy.set_event_loop(loop)
    try:
        yield
    finally:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            policy.set_event_loop(previous)


def _wrap_sync_fixture[**P, T](
    fixture_function: Callable[P, T], runner: asyncio.Runner
) -> Callable[P, T]:
    def call(*args: P.args, **kwargs: P.kwargs) -> T:
        with _temporary_event_loop(runner.get_loop()):
            return fixture_function(*args, **kwargs)

    return _sync_metadata(call, fixture_function)


def _wrap_syncgen_fixture[**P, T](
    fixture_function: Callable[P, Generator[T, None, None]], runner: asyncio.Runner
) -> Callable[P, Generator[T, None, None]]:
    def iterate(*args: P.args, **kwargs: P.kwargs) -> Generator[T, None, None]:
        with _temporary_event_loop(runner.get_loop()):
            yield from fixture_function(*args, **kwargs)

    return _sync_metadata(iterate, fixture_function)


@contextlib.contextmanager
def _runner_lifetime(
    policy: asyncio.AbstractEventLoopPolicy, factory: LoopFactory | None, debug: bool
) -> Iterator[asyncio.Runner]:
    import asyncio  # noqa: PLC0415 - only runner acquisition initializes asyncio

    previous_policy = _policy()
    previous_loop = _policy_loop(previous_policy)
    runner = asyncio.Runner(loop_factory=factory, debug=debug)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            # Legacy policy overrides remain part of the pytest compatibility boundary.
            asyncio.set_event_loop_policy(policy)  # ty: ignore[deprecated]
        runner.get_loop()
        if factory is not None:
            policy.set_event_loop(runner.get_loop())
        yield runner
    finally:
        failure = sys.exception()
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore", ".*BaseEventLoop.shutdown_asyncgens.*", RuntimeWarning
                )
                try:
                    runner.close()
                except BaseException as error:
                    if failure is not None:
                        failure.add_note(f"Async runner cleanup also failed: {error!r}")
                    elif isinstance(error, RuntimeError):
                        warnings.warn(
                            f"Async runner cleanup failed: {error}; do not close the test loop",
                            RuntimeWarning,
                            stacklevel=2,
                        )
                    else:
                        raise
        finally:
            primary = sys.exception() if failure is None else failure
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    # Restore the caller's legacy policy after the scoped runner closes.
                    asyncio.set_event_loop_policy(previous_policy)  # ty: ignore[deprecated]
                    previous_policy.set_event_loop(previous_loop)
            except BaseException as error:
                if primary is None:
                    raise
                primary.add_note(f"Event loop state restoration also failed: {error!r}")
