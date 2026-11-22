"""Enter the native capture owner at pytest's initial hook wrapper boundary."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Protocol

from .capture_fixtures import capfd, capfdbinary, capsys, capsysbinary, capteesys
from .facade import hookimpl

if TYPE_CHECKING:
    from .protocol import Config


class InitialCaptureOwner(Protocol):
    def initial(self, config: Config) -> AbstractContextManager[object]: ...


class InitialCaptureHooks:
    capsys = staticmethod(capsys)
    capfd = staticmethod(capfd)
    capsysbinary = staticmethod(capsysbinary)
    capfdbinary = staticmethod(capfdbinary)
    capteesys = staticmethod(capteesys)

    def __init__(self, owner: InitialCaptureOwner) -> None:
        self._owner = owner

    @hookimpl(wrapper=True)
    def pytest_load_initial_conftests(self, early_config: Config) -> Iterator[None]:
        with self._owner.initial(early_config):
            yield
