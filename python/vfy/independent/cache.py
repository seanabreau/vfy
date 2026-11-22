"""The ``cache`` fixture, ``cache_dir`` setting, and ``--nf`` ordering plug-in."""

from __future__ import annotations

import warnings
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, override

from vfy import native

from .facade import PytestWarning, fixture, hookimpl
from .legacy_path import LegacyPath
from .protocol import Config

if TYPE_CHECKING:
    from vfy._pytest_native import NativeIndependentRequest

    from .protocol import Item, Parser


class PytestCacheWarning(PytestWarning):
    """Warn when the configured cache cannot persist a value."""

    __module__ = "pytest"


class Cache:
    """Expose persistent values and directories through the native cache owner."""

    _CACHE_PREFIX_DIRS = "d"
    _CACHE_PREFIX_VALUES = "v"

    def __init__(self, cachedir: Path, config: Config, *, _ispytest: bool = False) -> None:
        self._cachedir = cachedir
        self._config = config
        self._service = native._pytest.NativeCacheService(str(cachedir))

    @override
    def __repr__(self) -> str:
        return "Cache()"

    @classmethod
    def for_config(cls, config: Config, *, _ispytest: bool = False) -> Cache:
        return cls(cls.cache_dir_from_config(config), config, _ispytest=True)

    @staticmethod
    def cache_dir_from_config(config: Config, *, _ispytest: bool = False) -> Path:
        return Path(config._configuration.projection()["cache_dir"])

    def get(self, key: str, default: object) -> object:
        return self._service.get(key, default)

    def set(self, key: str, value: object) -> None:
        try:
            self._service.set(key, value)
        except native._pytest.CachePathError as error:
            self.warn(f"could not create cache path {self._getvaluepath(key)}: {error}")
        except native._pytest.CacheWriteError as error:
            self.warn(f"cache could not write path {self._getvaluepath(key)}: {error}")

    def mkdir(self, name: str) -> Path:
        return Path(self._service.mkdir(name))

    def makedir(self, name: str) -> LegacyPath:
        return LegacyPath(self.mkdir(name))

    def _getvaluepath(self, key: str) -> Path:
        return self._cachedir / self._CACHE_PREFIX_VALUES / key

    def warn(self, fmt: str, *, _ispytest: bool = False, **args: object) -> None:
        warnings.warn(
            PytestCacheWarning(fmt.format(**args) if args else fmt),
            self._config.hook,
            stacklevel=3,
        )


@fixture
def cache(request: NativeIndependentRequest) -> Cache:
    """Return the configured session's persistent cache owner."""
    config = request.config
    assert isinstance(config, Config)
    return config.cache


class CacheProvider:
    """Register the configure callback and fixture declaration."""

    cache = staticmethod(cache)

    def __init__(
        self,
        configure: Callable[[Config, int | None], None],
        cache_dir_default: str,
        publisher_pid: int | None,
    ) -> None:
        self._configure = configure
        self._publisher_pid = publisher_pid
        self._cache_dir_default = cache_dir_default

    @hookimpl
    def pytest_addoption(self, parser: Parser) -> None:
        parser.addini("cache_dir", help="Cache directory path", default=self._cache_dir_default)

    @hookimpl(tryfirst=True)
    def pytest_configure(self, config: Config) -> None:
        self._configure(config, self._publisher_pid)


class _NodeidCache(Protocol):
    def collect(self, items: Sequence[Item]) -> None: ...
    def finish(self, config: Config) -> None: ...


class NFPlugin:
    """Adapt selected-item and finish hooks to the native node-ID cache owner."""

    def __init__(self, config: Config, state: _NodeidCache) -> None:
        self.config = config
        self._state = state

    @hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, items: list[Item]) -> Generator[None]:
        result = yield
        self._state.collect(items)
        return result

    @hookimpl
    def pytest_sessionfinish(self) -> None:
        self._state.finish(self.config)
