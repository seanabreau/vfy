"""POSIX signal timeouts for the independent runner without pytest-timeout."""

from __future__ import annotations

import inspect
import math
import os
import signal
import sys
import threading
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from vfy import native

from .facade import fail, hookimpl
from .stash import StashKey

if TYPE_CHECKING:
    from types import FrameType

    from .protocol import Config, Item, Node, Parser
    from .terminal import TerminalWriter


@dataclass(frozen=True)
class Settings:
    timeout: float = 0
    method: str = "signal"
    func_only: bool = False


_SETTINGS = StashKey[Settings]()


_TIMER_HANDLER: StashKey[Callable[[int, FrameType | None], None]] = StashKey()


def _seconds(value: object, source: str) -> float:
    if value is None or value == "":
        return 0
    try:
        if not isinstance(value, (str, int, float)):
            raise ValueError
        seconds = float(value)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError
        return seconds
    except (ValueError, OverflowError) as error:
        raise native._pytest.IndependentUsageError(
            f"{source} must be finite and nonnegative, got {value!r}"
        ) from error


def _settings(item: Item, defaults: Settings) -> Settings:
    marker = item.get_closest_marker("timeout")
    if marker is None:
        return defaults
    if not marker.args and not marker.kwargs:
        raise native._pytest.IndependentUsageError("timeout marker requires an argument")
    values = dict(marker.kwargs)
    if len(marker.args) > 2 or set(values) - {"timeout", "method", "func_only"}:
        raise native._pytest.IndependentUsageError(
            f"{item.nodeid}: unsupported timeout marker arguments"
        )
    for name, value in zip(("timeout", "method"), marker.args, strict=False):
        if name in values:
            raise native._pytest.IndependentUsageError(
                f"timeout marker has multiple values for {name}"
            )
        values[name] = value
    timeout = values.get("timeout")
    method = values.get("method")
    func_only = values.get("func_only")
    return Settings(
        defaults.timeout if timeout is None else _seconds(timeout, "timeout marker"),
        defaults.method if method is None else cast("str", method),
        defaults.func_only if func_only is None else cast("bool", func_only),
    )


def _validate(settings: Settings) -> None:
    if settings.method != "signal":
        raise native._pytest.IndependentUsageError(
            "timeout method must be 'signal'; thread timers require pytest-timeout"
        )
    if not isinstance(settings.func_only, bool):
        raise native._pytest.IndependentUsageError("timeout func_only must be a boolean")
    if settings.timeout and (
        not hasattr(signal, "setitimer")
        or threading.current_thread() is not threading.main_thread()
    ):
        raise native._pytest.IndependentUsageError(
            "signal timeouts require POSIX and the main thread"
        )


def pytest_addoption(parser: Parser) -> None:
    group = parser.getgroup("timeout", "POSIX signal timeouts")
    group.addoption("--timeout", type=float, default=None, dest="timeout")
    group.addoption("--timeout_method", "--timeout-method", dest="timeout_method", default=None)
    group.addoption("--timeout-disable-debugger-detection", action="store_true", default=False)
    group.addoption("--session-timeout", type=float, default=None, dest="session_timeout")
    parser.addini("timeout", "Timeout in seconds before dumping the stacks.", default=None)
    parser.addini("timeout_method", "Timeout mechanism to use.", default=None)
    parser.addini("timeout_func_only", "Limit timeouts to the test body.", type="bool")
    parser.addini("timeout_disable_debugger_detection", "Disable debugger detection.", type="bool")
    parser.addini("session_timeout", "Timeout in seconds for the entire session.", default=None)


def pytest_configure(config: Config) -> None:
    timeout = config.getoption("timeout")
    if timeout is None:
        timeout = os.environ.get("PYTEST_TIMEOUT")
    if timeout is None:
        timeout = config.getini("timeout")
    settings = Settings(
        _seconds(timeout, "timeout"),
        cast(
            "str", config.getoption("timeout_method") or config.getini("timeout_method") or "signal"
        ),
        cast("bool", config.getini("timeout_func_only")),
    )
    _validate(settings)
    session_timeout = config.getoption("session_timeout")
    if session_timeout is None:
        session_timeout = config.getini("session_timeout")
    if _seconds(session_timeout, "session_timeout"):
        raise native._pytest.IndependentUsageError("session_timeout requires pytest-timeout")
    if config.getoption("timeout_disable_debugger_detection") or config.getini(
        "timeout_disable_debugger_detection"
    ):
        raise native._pytest.IndependentUsageError(
            "timeout_disable_debugger_detection requires pytest-timeout"
        )
    config.stash[_SETTINGS] = settings
    config.addinivalue_line(
        "markers",
        "timeout(timeout, method=None, func_only=False): POSIX signal timeout; 0 disables.",
    )
    config.pluginmanager.register(Timers(settings), "vfy-independent-timeout-timers")


@hookimpl(trylast=True)
def pytest_collection_modifyitems(config: Config, items: list[Item]) -> None:
    defaults = config.stash[_SETTINGS]
    for item in items:
        settings = _settings(item, defaults)
        _validate(settings)


def _debugging() -> bool:
    trace = sys.gettrace()
    module = None if trace is None else inspect.getmodule(trace) or inspect.getmodule(type(trace))
    return sys.monitoring.get_tool(sys.monitoring.DEBUGGER_ID) is not None or (
        module is not None
        and any(part.startswith(("pydevd", "bdb")) for part in module.__name__.split("."))
    )


@contextmanager
def _timer(item: Item, timeout: float) -> Iterator[None]:
    previous_owner = item.stash.get(_TIMER_HANDLER, None)
    previous = signal.getsignal(signal.SIGALRM)
    if signal.getitimer(signal.ITIMER_REAL) != (0, 0) and (
        previous_owner is None or previous is not previous_owner
    ):
        raise native._pytest.IndependentUsageError(
            "signal timeout cannot replace an active ITIMER_REAL deadline"
        )

    def expired(signum: int, frame: FrameType | None) -> None:
        if _debugging():
            return
        terminal = cast("TerminalWriter", item.config.get_terminal_writer())
        for ident, thread_frame in sys._current_frames().items():
            if ident != threading.get_ident():
                terminal.sep("~", f"Stack of thread {ident}")
                terminal.write("".join(traceback.format_stack(thread_frame)))
        fail(f"Timeout (>{timeout}s) from vfy.")

    try:
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        item.stash[_TIMER_HANDLER] = expired
        yield
    finally:
        try:
            # A call-only replacement cancels the protocol deadline too, as upstream does.
            signal.setitimer(signal.ITIMER_REAL, 0)
        finally:
            try:
                signal.signal(signal.SIGALRM, previous)
            finally:
                if previous_owner is not None:
                    item.stash[_TIMER_HANDLER] = previous_owner
                elif _TIMER_HANDLER in item.stash:
                    del item.stash[_TIMER_HANDLER]


class Timers:
    """Resolve mutable item markers at each timer's lifecycle boundary."""

    def __init__(self, defaults: Settings) -> None:
        self.defaults = defaults

    def pytest_exception_interact(self, node: Node) -> None:
        owner = node.stash.get(_TIMER_HANDLER, None)
        if owner is not None and signal.getsignal(signal.SIGALRM) is owner:
            signal.setitimer(signal.ITIMER_REAL, 0)

    @hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item: Item) -> Iterator[object]:
        settings = _settings(item, self.defaults)
        _validate(settings)
        if settings.timeout and not settings.func_only:
            with _timer(item, settings.timeout):
                return (yield)
        return (yield)

    @hookimpl(wrapper=True)
    def pytest_runtest_call(self, item: Item) -> Iterator[object]:
        settings = _settings(item, self.defaults)
        _validate(settings)
        if settings.timeout and settings.func_only:
            with _timer(item, settings.timeout):
                return (yield)
        return (yield)
