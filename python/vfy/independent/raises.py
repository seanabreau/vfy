"""Exception context adaptation over vfy's shared native matcher.

The base identity is also used by native xfail policy to admit maintained
matchers. No pytest modules are imported when this service is loaded.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, override

from vfy import native

if TYPE_CHECKING:
    from .facade import ExceptionInfo


class AbstractRaises[ExcT: BaseException](ABC):
    """Owned matcher identity recognized by the native exception policy."""

    @abstractmethod
    def matches(self, exception: BaseException | None) -> bool:
        """Test the original exception and retain a diagnostic on failure."""

    @property
    @abstractmethod
    def fail_reason(self) -> str | None:
        """Explain the most recent failed match."""


class RaisesExc[ExcT: BaseException](AbstractRaises[ExcT]):
    """Match exception types, message patterns, notes, and an optional check."""

    def __init__(
        self,
        expected_exception: type[ExcT] | tuple[type[ExcT], ...] | None = None,
        /,
        *,
        match: object = None,
        check: object = None,
    ) -> None:
        self._matcher = native._pytest.NativeRaisesMatcher(
            expected_exception, pattern=match, check=check
        )

    @override
    def matches(self, exception: BaseException | None) -> bool:
        return self._matcher.matches(exception)

    @property
    @override
    def fail_reason(self) -> str | None:
        return self._matcher.fail_reason

    @property
    def expected_exceptions(self) -> tuple[type[BaseException], ...]:
        return self._matcher.expected_exceptions

    @property
    def match(self) -> re.Pattern[str] | None:
        return self._matcher.pattern

    @property
    def check(self) -> Callable[[BaseException], bool] | None:
        return self._matcher.check

    @property
    def is_baseexception(self) -> bool:
        return self._matcher.is_baseexception

    @override
    def __repr__(self) -> str:
        return self._matcher.description()

    def __enter__(self) -> ExceptionInfo:
        # The facade owns ExceptionInfo and imports this module.
        from .facade import ExceptionInfo  # noqa: PLC0415 - facade import cycle

        self.excinfo = ExceptionInfo()
        return self.excinfo

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if exception_type is None:
            # The facade owns Failed and imports this module.
            from .facade import Failed  # noqa: PLC0415 - facade import cycle

            raise Failed(self._matcher.missing_exception_message())
        if self.excinfo is None:
            raise AssertionError("Internal error - should have been constructed in __enter__")
        if not self.matches(exception):
            if self._matcher.just_propagate:
                return False
            raise AssertionError(self.fail_reason) from None
        if exception is None:
            raise AssertionError("matching requires an actual exception")
        self.excinfo._fill(exception, traceback)
        return True
