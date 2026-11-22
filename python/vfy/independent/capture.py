"""Standard-library logging and warning-filter adapters for native independent capture scopes."""

from __future__ import annotations

import logging
import re
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from io import StringIO
from typing import TYPE_CHECKING, Protocol, override

if TYPE_CHECKING:
    from .protocol import Config


# pytest memoizes `parse_warning_filter` with the same bound; repeated per-test
# filters resolve their category once, as they do under pytest.
@lru_cache(maxsize=50)
def parse_warning_filter(filter_: str, escape: bool) -> tuple[str, str, type[Warning], str, int]:
    from vfy import native  # noqa: PLC0415 - native capture initializes this adapter lazily

    return native._pytest.independent_parse_warning_filter(filter_, escape)


class PhaseLogging(Protocol):
    """Native resource guards called by the builtin logging hook wrappers."""

    def phase(self, when: str) -> AbstractContextManager[None]: ...
    def protocol(self, config: Config) -> AbstractContextManager[None]: ...


class DatetimeFormatter(logging.Formatter):
    """Honor strftime microseconds through CPython's datetime protocol."""

    @override
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        if datefmt and "%f" in datefmt:
            timestamp = self.converter(record.created)
            zone = timezone(timedelta(seconds=timestamp.tm_gmtoff), timestamp.tm_zone)
            value = datetime(*timestamp[:6], microsecond=int(record.msecs * 1000), tzinfo=zone)
            return value.strftime(datefmt)
        return super().formatTime(record, datefmt)


class LogState(Protocol):
    """Mutable LogRecord containers retained by the native phase owner."""

    @property
    def records(self) -> list[logging.LogRecord]: ...
    @property
    def active(self) -> bool: ...
    def get_records(self, when: str) -> list[logging.LogRecord]: ...
    def emit(self, record: logging.LogRecord) -> None: ...
    def clear(self) -> None: ...
    def reset(self) -> None: ...


class LogHandler(logging.StreamHandler[StringIO]):
    """Pass stdlib formatting results and original records to native storage."""

    def __init__(self, state: LogState, formatter: logging.Formatter) -> None:
        super().__init__(StringIO())
        self.state = state
        self.setFormatter(formatter)

    @override
    def emit(self, record: logging.LogRecord) -> None:
        # Records are retained before formatting, including malformed messages.
        self.state.emit(record)
        super().emit(record)

    @override
    def handleError(self, record: logging.LogRecord) -> None:
        if logging.raiseExceptions:
            raise  # noqa: PLE0704 - StreamHandler calls this with the formatting exception active.

    @property
    def records(self) -> list[logging.LogRecord]:
        return self.state.records

    def clear(self) -> None:
        self.state.clear()
        self._reset_stream()

    def reset(self) -> None:
        self.state.reset()
        self._reset_stream()

    def _reset_stream(self) -> None:
        self.stream = StringIO()


class LoggingScope:
    """Attach handlers to the loggers used by stdlib propagation."""

    __slots__ = ("_groups", "handlers", "level")

    def __init__(self, handlers: list[logging.Handler], level: int | None) -> None:
        self.handlers = handlers
        self.level = level
        self._groups: list[tuple[logging.Handler, list[logging.Logger], int | None]] = []

    def __enter__(self) -> None:
        for handler in self.handlers:
            self._enter_handler(handler)

    def _enter_handler(self, handler: logging.Handler) -> None:
        root = logging.getLogger()
        if self.level is not None:
            handler.setLevel(self.level)
        attached = [root]
        self._groups.append((handler, attached, None))
        root.addHandler(handler)
        for logger in root.manager.loggerDict.values():
            if isinstance(logger, logging.Logger) and logger is not root and not logger.propagate:
                attached.append(logger)
                logger.addHandler(handler)
        if self.level is not None:
            original_level = root.level
            self._groups[-1] = (handler, attached, original_level)
            root.setLevel(min(original_level, self.level))

    def close(self) -> None:
        if not self._groups:
            return
        handler, attached, original_level = self._groups.pop()
        try:
            root = logging.getLogger()
            if original_level is not None:
                root.setLevel(original_level)
            for logger in attached:
                logger.removeHandler(handler)
        finally:
            self.close()


class LogCaptureFixture:
    """Expose live native phase records through pytest's logging fixture API."""

    def __init__(self, state: LogState, handler: LogHandler) -> None:
        self._state = state
        self._handler = handler
        self._logger_levels: dict[str | None, int] = {}
        self._handler_level: int | None = None
        self._disabled_level: int | None = None

    @property
    def handler(self) -> LogHandler:
        if not self._state.active:
            raise KeyError("caplog_handler")
        return self._handler

    def get_records(self, when: str) -> list[logging.LogRecord]:
        if not self._state.active:
            raise KeyError("caplog_records")
        return self._state.get_records(when)

    @property
    def records(self) -> list[logging.LogRecord]:
        # Handler access enforces the fixture's per-test lifetime.
        return self.handler.records

    @property
    def text(self) -> str:
        return re.sub(r"\x1b\[[\d;]+m", "", self.handler.stream.getvalue())

    @property
    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]

    @property
    def record_tuples(self) -> list[tuple[str, int, str]]:
        return [(record.name, record.levelno, record.getMessage()) for record in self.records]

    def clear(self) -> None:
        self.handler.clear()

    @staticmethod
    def _enable(level: int | str, logger: logging.Logger) -> int:
        previous = logger.manager.disable
        numeric = (
            logging.getLevelNamesMapping().get(level, level) if isinstance(level, str) else level
        )
        if not isinstance(numeric, int):
            logging.disable(logging.NOTSET)
        elif not logger.isEnabledFor(numeric):
            logging.disable(max(numeric - 10, logging.NOTSET))
        return previous

    def set_level(self, level: int | str, logger: str | None = None) -> None:
        target = logging.getLogger(logger)
        self._logger_levels.setdefault(logger, target.level)
        target.setLevel(level)
        if self._handler_level is None:
            self._handler_level = self.handler.level
        self.handler.setLevel(level)
        disabled = self._enable(level, target)
        if self._disabled_level is None:
            self._disabled_level = disabled

    @contextmanager
    def at_level(self, level: int | str, logger: str | None = None) -> Generator[None]:
        target = logging.getLogger(logger)
        original, handler_level = target.level, self.handler.level
        target.setLevel(level)
        self.handler.setLevel(level)
        disabled = self._enable(level, target)
        try:
            yield
        finally:
            target.setLevel(original)
            self.handler.setLevel(handler_level)
            logging.disable(disabled)

    @contextmanager
    def filtering(self, filter_: logging.Filter) -> Generator[None]:
        self.handler.addFilter(filter_)
        try:
            yield
        finally:
            self.handler.removeFilter(filter_)

    def close(self) -> None:
        if self._handler_level is not None:
            self.handler.setLevel(self._handler_level)
        for name, level in self._logger_levels.items():
            logging.getLogger(name).setLevel(level)
        if self._disabled_level is not None:
            logging.disable(self._disabled_level)
            self._disabled_level = None
