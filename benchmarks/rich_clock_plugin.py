"""Hold Rich's logging timestamp and FileProxy lifetime steady during tests."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from importlib import import_module
from io import TextIOBase

import pytest

_FIXED_TIMESTAMP = 1_700_000_000.0


@pytest.fixture(autouse=True)
def retain_rich_file_proxies(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close each Rich stream in its creating test's teardown, before finalization."""

    file_proxy_type = import_module("rich.file_proxy").FileProxy
    proxies: list[TextIOBase] = []
    original_init = file_proxy_type.__init__

    def retain_init(proxy: TextIOBase, console: object, file: object) -> None:
        original_init(proxy, console, file)
        proxies.append(proxy)

    monkeypatch.setattr(file_proxy_type, "__init__", retain_init)
    try:
        yield
    finally:
        for proxy in proxies:
            proxy.close()


@pytest.fixture(autouse=True)
def freeze_rich_log_clock(
    request: pytest.FixtureRequest, retain_rich_file_proxies: None
) -> Iterator[None]:
    """Keep repeated-time rendering independent of each engine's run speed."""

    if request.node.path.name != "test_logging.py":
        yield
        return

    original = logging.getLogRecordFactory()

    def fixed_factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = original(*args, **kwargs)
        record.created = _FIXED_TIMESTAMP
        record.msecs = 0.0
        record.relativeCreated = 0.0
        return record

    logging.setLogRecordFactory(fixed_factory)
    try:
        yield
    finally:
        logging.setLogRecordFactory(original)
