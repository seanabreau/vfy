"""CPython fault-handler adapters for native-owned configuration and protocol scopes."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import TYPE_CHECKING

from vfy import native

from .facade import hookimpl
from .stash import StashKey

if TYPE_CHECKING:
    from .protocol import Config, Item


fault_handler_original_stderr_fd_key = StashKey[int]()
fault_handler_stderr_fd_key = StashKey[int]()


def get_stderr_fileno() -> object:
    """Resolve the live stderr protocol, falling back to CPython's original stream."""
    try:
        descriptor = sys.stderr.fileno()
        if descriptor == -1:
            raise AttributeError
        return descriptor
    except (AttributeError, ValueError):
        assert sys.__stderr__ is not None
        return sys.__stderr__.fileno()


@hookimpl
def pytest_configure(config: Config) -> None:
    native._pytest.independent_faulthandler_configure(config, get_stderr_fileno())


@hookimpl
def pytest_unconfigure(config: Config) -> None:
    native._pytest.independent_faulthandler_unconfigure(config)


@hookimpl(wrapper=True, trylast=True)
def pytest_runtest_protocol(item: Item) -> Iterator[object]:
    armed = native._pytest.independent_faulthandler_start(item.config)
    if not armed:
        return (yield)
    try:
        return (yield)
    finally:
        native._pytest.independent_faulthandler_cancel()


@hookimpl(tryfirst=True)
def pytest_enter_pdb() -> None:
    native._pytest.independent_faulthandler_cancel()


@hookimpl(tryfirst=True)
def pytest_exception_interact() -> None:
    native._pytest.independent_faulthandler_cancel()


class FaulthandlerProvider:
    """Expose the real CPython adapters to the native hook registry."""

    def __init__(self) -> None:
        self.pytest_configure = pytest_configure
        self.pytest_unconfigure = pytest_unconfigure
        self.pytest_runtest_protocol = pytest_runtest_protocol
        self.pytest_enter_pdb = pytest_enter_pdb
        self.pytest_exception_interact = pytest_exception_interact
