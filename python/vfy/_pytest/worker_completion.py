"""Observe experimental worker completion without importing the native plugin early."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from _pytest._code import ExceptionInfo
from _pytest.config import Config
from _pytest.config.argparsing import Parser


class _NativeWorkerCompletionRecorder:
    """Keep post-unconfigure completion separate from pytest's session hooks."""

    def __init__(self) -> None:
        self.config: Config | None = None
        self.infrastructure_failed = False
        self.exit_reason: str | None = None

    @pytest.hookimpl
    def pytest_addoption(self, parser: Parser) -> None:
        # pytest 9.1 names programmatic runs separately from its -m entry point.
        if parser.prog == "pytest.main()":
            parser.prog = "python -m pytest"

    @pytest.hookimpl
    def pytest_plugin_registered(self, plugin: object, plugin_name: str) -> None:
        # Historic registration captures Config before parse or configure can exit.
        if plugin_name == "pytestconfig" and isinstance(plugin, Config):
            self.config = plugin

    @pytest.hookimpl(tryfirst=True)
    def pytest_internalerror(self) -> None:
        self.infrastructure_failed = True

    @pytest.hookimpl(tryfirst=True)
    def pytest_keyboard_interrupt(self, excinfo: ExceptionInfo[BaseException]) -> None:
        # A successful status alone cannot distinguish pytest.exit from a masked failure.
        error = excinfo.value
        if isinstance(error, pytest.exit.Exception):
            self.exit_reason = str(error.msg)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self) -> Generator[None, None, None]:
        # Pytest catches finish-hook exits without calling pytest_keyboard_interrupt.
        try:
            yield
        except pytest.exit.Exception as error:
            self.exit_reason = str(error.msg)
            raise
