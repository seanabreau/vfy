"""Built-in fixtures: ``pytestconfig``, temp paths, ``monkeypatch``, ``recwarn``, ``caplog``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, override

from .facade import fixture, hookimpl
from .helpers import TempPathFactory
from .legacy_path import LegacyPath
from .protocol import Config

if TYPE_CHECKING:
    from vfy._pytest_native import NativeIndependentRequest

    from .protocol import Session


type ResourceKind = Literal[
    "tmp_path_factory", "tmp_path", "tmpdir_factory", "monkeypatch", "recwarn", "caplog"
]


def _resource(
    request: NativeIndependentRequest, kind: ResourceKind, dependency: object = None
) -> object:
    from vfy.native import _pytest  # noqa: PLC0415 - facade/bootstrap import cycle

    return _pytest.independent_fixture_resource(request, kind, dependency)


class _TemporaryDirectoryOwner(Protocol):
    def getbasetemp(self) -> Path: ...
    def mktemp(self, basename: str, numbered: bool = True) -> Path: ...
    def close(self) -> None: ...


class ConfiguredTempPathFactory(TempPathFactory):
    """Expose the existing factory protocol through its configured native owner."""

    def __init__(self, owner: _TemporaryDirectoryOwner) -> None:
        self._owner = owner

    @override
    def getbasetemp(self) -> Path:
        return self._owner.getbasetemp()

    @override
    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        return self._owner.mktemp(basename, numbered)

    @override
    def cleanup(self) -> None:
        self._owner.close()


@fixture(scope="session")
def pytestconfig(request: NativeIndependentRequest) -> Config:
    config = request.config
    assert isinstance(config, Config)
    return config


@fixture(scope="session")
def tmp_path_factory(request: NativeIndependentRequest) -> object:
    return _resource(request, "tmp_path_factory")


@fixture
def tmp_path(request: NativeIndependentRequest, tmp_path_factory: object) -> object:
    return _resource(request, "tmp_path", tmp_path_factory)


@fixture(scope="session")
def tmpdir_factory(request: NativeIndependentRequest) -> object:
    return _resource(request, "tmpdir_factory")


@fixture
def tmpdir(tmp_path: Path) -> LegacyPath:
    return LegacyPath(tmp_path)


@fixture
def monkeypatch(request: NativeIndependentRequest) -> object:
    return _resource(request, "monkeypatch")


@fixture
def recwarn(request: NativeIndependentRequest) -> object:
    return _resource(request, "recwarn")


@fixture
def caplog(request: NativeIndependentRequest) -> object:
    return _resource(request, "caplog")


class FixturesProvider:
    pytestconfig = staticmethod(pytestconfig)


class TmpdirProvider:
    tmp_path_factory = staticmethod(tmp_path_factory)
    tmp_path = staticmethod(tmp_path)

    def __init__(
        self, configure: Callable[[Config], None], finish: Callable[[Session, int], None]
    ) -> None:
        self.pytest_configure = configure
        self.pytest_sessionfinish = finish


class LegacyPathProvider:
    def __init__(self, configure: Callable[[Config], None]) -> None:
        self.pytest_configure = configure


class LegacyTmpdirProvider:
    tmpdir_factory = staticmethod(tmpdir_factory)
    tmpdir = staticmethod(tmpdir)


class MonkeyPatchProvider:
    monkeypatch = staticmethod(monkeypatch)


class RecwarnProvider:
    recwarn = staticmethod(recwarn)


class LoggingProvider:
    caplog = staticmethod(caplog)

    def __init__(self, configure: Callable[[Config], None]) -> None:
        self._configure = configure

    @hookimpl(trylast=True)
    def pytest_configure(self, config: Config) -> None:
        self._configure(config)
