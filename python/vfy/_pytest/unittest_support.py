"""Unittest skip and subtest adapters called from the Rust runner."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TypeGuard, cast, override

import pytest
from _pytest import unittest as _pytest_unittest
from _pytest._code import ExceptionInfo
from _pytest.config import Config
from _pytest.runner import CallInfo

from . import parallel


def normalize_unittest_skip(item: pytest.Item, exception: BaseException) -> BaseException:
    """Apply the enabled unittest plug-in's normalization for every test phase."""
    unittest = sys.modules.get("unittest")
    if (
        unittest is None
        or not item.config.pluginmanager.hasplugin("unittest")
        or not isinstance(exception, unittest.SkipTest)
    ):
        return exception
    call = CallInfo[None](
        None,
        ExceptionInfo.from_exc_info((type(exception), exception, exception.__traceback__)),
        start=0,
        stop=0,
        duration=0,
        when="call",
        _ispytest=True,
    )
    # Native execution already drained the current unittest exception. Keep
    # later cleanup errors queued while reusing pytest's SkipTest conversion,
    # including its upstream diagnostic location.
    if isinstance(item, _pytest_unittest.TestCaseFunction):
        pending = item._excinfo
        item._excinfo = None
        try:
            _pytest_unittest.pytest_runtest_makereport(item, call)
        finally:
            item._excinfo = pending
    else:
        _pytest_unittest.pytest_runtest_makereport(item, call)
    assert call.excinfo is not None
    return call.excinfo.value


def subtest_xfail_matcher(raises: object) -> tuple[object, Callable[[], None] | None]:
    """Share one match evaluation between upstream and native subtest reports."""
    # Only pytest 9's unittest subtest bridge uses this adapter; pytest 8.0 does
    # not provide the raises module.
    from _pytest.raises import AbstractRaises  # noqa: PLC0415

    if not isinstance(raises, (type, tuple, AbstractRaises)):
        return raises, None

    class SubtestMatcher(AbstractRaises[BaseException]):
        def __init__(self) -> None:
            super().__init__(match=None, check=None)
            self._exception: BaseException | None = None
            self._matched = False

        def reset(self) -> None:
            self._exception = None

        @override
        def matches(self, exception: BaseException) -> TypeGuard[BaseException]:
            if exception is self._exception:
                return self._matched
            if isinstance(raises, AbstractRaises):
                matched = raises.matches(exception)
            else:
                matched = isinstance(
                    exception,
                    cast("type[BaseException] | tuple[type[BaseException], ...]", raises),
                )
            self._exception, self._matched = exception, matched
            return matched

    matcher = SubtestMatcher()
    return matcher, matcher.reset


def unittest_subtest_report_callback(
    config: Config, callback: Callable[..., object]
) -> Callable[..., object]:
    """Preserve upstream subtest status writes within worker stdout capture."""
    if not parallel.is_worker() or config.option.capture == "no":
        return callback

    def report_subtest(*arguments: object) -> object:
        __tracebackhide__ = True
        manager = config.pluginmanager.getplugin("capturemanager")
        terminal = config.pluginmanager.getplugin("terminalreporter")
        if manager is None or terminal is None or not manager.is_globally_capturing():
            return callback(*arguments)
        previous = terminal._tw._file
        terminal._tw._file = sys.__stdout__
        try:
            return callback(*arguments)
        finally:
            terminal._tw._file = previous

    return report_subtest
