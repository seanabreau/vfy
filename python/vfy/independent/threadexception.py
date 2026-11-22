"""CPython thread exception hooks backed by the native capture queue and drain."""

from __future__ import annotations

import functools
import sys
import threading
import traceback
import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple

from vfy import native

from .facade import hookimpl
from .stash import StashKey
from .unraisable import tracemalloc_message

if TYPE_CHECKING:
    from vfy._pytest_native import NativeUnraisableQueue

    from .protocol import Config, Item


class ThreadExceptionMeta(NamedTuple):
    msg: str
    cause_msg: str
    exc_value: BaseException | None


thread_exceptions: StashKey[NativeUnraisableQueue] = StashKey()


def collect_thread_exception(config: Config) -> None:
    """Keep warning emission in its real exception scope while Rust aggregates failures."""
    # Stash consumers can replace the queue with a foreign pop/metadata resource.
    pop_exception: Callable[[], Any] = config.stash[thread_exceptions].pop
    driver = None
    meta: Any = None
    hook_error = None
    try:
        while True:
            try:
                meta = pop_exception()
            except IndexError:
                break
            if driver is None:
                driver = native._pytest.NativeUnraisableDrain("thread")
            if driver.is_error(meta):
                hook_error = driver.hook_error()
                hook_error.__cause__ = meta
                driver.append(hook_error)
                continue
            try:
                warnings.warn(sys.modules["pytest"].PytestUnhandledThreadExceptionWarning(meta.msg))  # noqa: B028 - pytest preserves the provider's warning callsite.
            except sys.modules["pytest"].PytestUnhandledThreadExceptionWarning as error:
                driver.filtered(meta, error)
        if driver is not None:
            driver.finish()
    finally:
        try:
            if driver is not None:
                driver.release()
        finally:
            del meta, hook_error


def cleanup(*, config: Config, prev_hook: Callable[[threading.ExceptHookArgs], object]) -> None:
    """Drain pending failures and restore the previous hook even if warnings fail."""
    try:
        try:
            collect_thread_exception(config)
        finally:
            threading.excepthook = prev_hook
    finally:
        del config.stash[thread_exceptions]


def thread_exception_hook(
    args: threading.ExceptHookArgs,
    /,
    *,
    append: Callable[[ThreadExceptionMeta | BaseException], object],
) -> None:
    """Capture the actual thread name and traceback before CPython releases its arguments."""
    try:
        thread_name = "<unknown>" if args.thread is None else args.thread.name
        summary = f"Exception in thread {thread_name}"
        traceback_message = "\n\n" + "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        allocation = "\n" + tracemalloc_message(args.thread)
        append(
            native._pytest.independent_thread_exception_metadata(
                summary, traceback_message, allocation, args.exc_value
            )
        )
    except BaseException as error:
        append(error)
        raise


@hookimpl
def pytest_configure(config: Config) -> None:
    previous = threading.excepthook
    queue = native._pytest.NativeUnraisableQueue()
    config.stash[thread_exceptions] = queue
    config.add_cleanup(functools.partial(cleanup, config=config, prev_hook=previous))
    threading.excepthook = functools.partial(thread_exception_hook, append=queue.append)


@hookimpl(trylast=True)
def pytest_runtest_setup(item: Item) -> None:
    collect_thread_exception(item.config)


@hookimpl(trylast=True)
def pytest_runtest_call(item: Item) -> None:
    collect_thread_exception(item.config)


@hookimpl(trylast=True)
def pytest_runtest_teardown(item: Item) -> None:
    collect_thread_exception(item.config)


class ThreadExceptionProvider:
    """Register capture and phase-drain hooks."""

    def __init__(self) -> None:
        self.pytest_configure = pytest_configure
        self.pytest_runtest_setup = pytest_runtest_setup
        self.pytest_runtest_call = pytest_runtest_call
        self.pytest_runtest_teardown = pytest_runtest_teardown
