"""Resources for CPython unraisable exceptions."""

from __future__ import annotations

import functools
import gc
import sys
import traceback
import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple, SupportsIndex

from vfy import native

from .facade import hookimpl
from .stash import StashKey

if TYPE_CHECKING:
    from vfy._pytest_native import NativeUnraisableQueue

    from .protocol import Config, Item


gc_collect_iterations_key = StashKey[SupportsIndex]()
unraisable_exceptions: StashKey[NativeUnraisableQueue] = StashKey()


class UnraisableMeta(NamedTuple):
    msg: str
    cause_msg: str
    exc_value: BaseException | None


def gc_collect_harder(iterations: SupportsIndex) -> None:
    """Run the actual index/range and collector protocols at shutdown."""
    for _ in range(iterations):
        gc.collect()


def collect_unraisable(config: Config) -> None:
    """Drain the selected queue while preserving real warning exception scopes."""
    # The public stash may contain a foreign pop resource or metadata protocol.
    pop_unraisable: Callable[[], Any] = config.stash[unraisable_exceptions].pop
    driver = None
    meta: Any = None
    hook_error = None
    try:
        while True:
            try:
                meta = pop_unraisable()
            except IndexError:
                break
            if driver is None:
                driver = native._pytest.NativeUnraisableDrain()
            if driver.is_error(meta):
                hook_error = driver.hook_error()
                hook_error.__cause__ = meta
                driver.append(hook_error)
                continue
            msg = meta.msg
            try:
                # Preserve the provider's default callsite and actual warn keyword arguments.
                warnings.warn(sys.modules["pytest"].PytestUnraisableExceptionWarning(msg))  # noqa: B028 - pytest keeps the default stacklevel
            except sys.modules["pytest"].PytestUnraisableExceptionWarning as error:
                driver.filtered(meta, error)
        if driver is not None:
            driver.finish()
    finally:
        try:
            if driver is not None:
                driver.release()
        finally:
            del meta, hook_error


def cleanup(*, config: Config, prev_hook: Callable[[sys.UnraisableHookArgs], object]) -> None:
    """Restore the real previous hook and remove the stash even when draining fails."""
    gc_collect_iterations = native._pytest.independent_unraisable_iterations(config)
    try:
        try:
            gc_collect_harder(gc_collect_iterations)
            collect_unraisable(config)
        finally:
            sys.unraisablehook = prev_hook
    finally:
        del config.stash[unraisable_exceptions]


def tracemalloc_message(source: object) -> str:
    """Format the optional allocation traceback in its owning frame."""
    if source is None:
        return ""
    try:
        # tracemalloc is optional and import hooks can raise ImportError.
        import tracemalloc  # noqa: PLC0415 - optional module, may be absent
    except ImportError:
        return ""
    tb = tracemalloc.get_object_traceback(source)
    if tb is not None:
        formatted_tb = "\n".join(tb.format())
        return native._pytest.independent_unraisable_allocation(formatted_tb)
    return native._pytest.independent_unraisable_allocation(None)


def unraisable_hook(
    unraisable: sys.UnraisableHookArgs,
    /,
    *,
    append: Callable[[UnraisableMeta | BaseException], object],
) -> None:
    """Capture actual callback resources before CPython releases its arguments."""
    try:
        err_msg = native._pytest.independent_unraisable_prefix(unraisable)
        summary = f"{err_msg}: {unraisable.object!r}"
        traceback_message = "\n\n" + "".join(
            traceback.format_exception(
                unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback
            )
        )
        tracemalloc_tb = "\n" + tracemalloc_message(unraisable.object)
        append(
            native._pytest.independent_unraisable_metadata(
                summary, traceback_message, tracemalloc_tb, unraisable.exc_value
            )
        )
    except BaseException as error:
        append(error)
        raise


@hookimpl
def pytest_configure(config: Config) -> None:
    prev_hook = sys.unraisablehook
    queue = native._pytest.NativeUnraisableQueue()
    config.stash[unraisable_exceptions] = queue
    config.add_cleanup(functools.partial(cleanup, config=config, prev_hook=prev_hook))
    sys.unraisablehook = functools.partial(unraisable_hook, append=queue.append)


@hookimpl
def pytest_unconfigure(config: Config) -> None:
    if not native._pytest.independent_unraisable_configured(config):
        return
    gc_collect_iterations = native._pytest.independent_unraisable_iterations(config)
    gc_collect_harder(gc_collect_iterations)
    collect_unraisable(config)


@hookimpl(trylast=True)
def pytest_runtest_setup(item: Item) -> None:
    collect_unraisable(item.config)


@hookimpl(trylast=True)
def pytest_runtest_call(item: Item) -> None:
    collect_unraisable(item.config)


@hookimpl(trylast=True)
def pytest_runtest_teardown(item: Item) -> None:
    collect_unraisable(item.config)


class UnraisableProvider:
    """Declare actual lifecycle hooks on the registered provider instance."""

    def __init__(self) -> None:
        self.pytest_configure = pytest_configure
        self.pytest_unconfigure = pytest_unconfigure
        self.pytest_runtest_setup = pytest_runtest_setup
        self.pytest_runtest_call = pytest_runtest_call
        self.pytest_runtest_teardown = pytest_runtest_teardown
