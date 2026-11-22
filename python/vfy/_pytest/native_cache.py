"""pytest's cache protocol adapter and its collection hooks."""

from __future__ import annotations

import warnings
from collections.abc import Generator, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, override

import pytest
from _pytest.cacheprovider import Cache as _PytestCache
from _pytest.config import Config
from _pytest.legacypath import LEGACY_PATH, legacy_path
from _pytest.nodes import Directory
from _pytest.pathlib import resolve_from_str
from _pytest.reports import CollectReport
from _pytest.warning_types import PytestCacheWarning

from . import compact_collection, parallel
from .native_state import (
    CachePathError,
    CacheWriteError,
    NativeCacheService,
    NativeCacheSession,
    NativeRunResult,
    UnsupportedExtensionError,
    _cache_preflight_accepted_key,
    _compatibility_policy_key,
    _result_key,
)
from .xdist_cache import XdistCacheReporter
from .xdist_support import is_native_xdist

if TYPE_CHECKING:
    from _pytest.main import Session


# pytest's Cache final marker is for users; the subclass preserves its fixture identity.
class _NativeCache(_PytestCache):  # ty: ignore[subclass-of-final-class]
    """pytest's cache protocol backed by the Rust cache service."""

    def __init__(self, cachedir: str | Path, config: Config, *, _ispytest: bool = False) -> None:
        super().__init__(Path(cachedir), config, _ispytest=True)
        self._service = NativeCacheService(str(cachedir))
        policy = config.stash[_compatibility_policy_key]
        self._session = NativeCacheSession(
            str(cachedir), stepwise_cache_format=policy.stepwise_cache_format
        )

    @override
    def __repr__(self) -> str:
        return "Cache()"

    @classmethod
    @override
    def for_config(cls, config: Config, *, _ispytest: bool = False) -> _NativeCache:
        del _ispytest
        return cls(cls.cache_dir_from_config(config), config)

    @staticmethod
    @override
    def cache_dir_from_config(config: Config, *, _ispytest: bool = False) -> Path:
        del _ispytest
        return resolve_from_str(config.getini("cache_dir"), config.rootpath)

    @override
    def get(self, key: str, default: object) -> object:
        return self._service.get(key, default)

    @override
    def set(self, key: str, value: object) -> None:
        try:
            self._service.set(key, value)
        except CachePathError as exc:
            self.warn(f"could not create cache path {self._value_path(key)}: {exc}")
        except CacheWriteError as exc:
            self.warn(f"cache could not write path {self._value_path(key)}: {exc}")

    @override
    def mkdir(self, name: str) -> Path:
        return Path(self._service.mkdir(name))

    def makedir(self, name: str) -> LEGACY_PATH:
        return legacy_path(self.mkdir(name))

    @override
    def _getvaluepath(self, key: str) -> Path:
        return self._value_path(key)

    def initialize(self) -> None:
        try:
            self._service.initialize()
        except OSError as exc:
            self.warn(f"could not create cache path {self._cachedir}: {exc}")

    def selection_flags(self) -> list[str]:
        flags: list[str] = []
        if self._config.option.lf:
            flags.append("last-failed")
        if self._config.option.failedfirst:
            flags.append("failed-first")
        if self._config.option.newfirst:
            flags.append("new-first")
        if getattr(self._config.option, "stepwise", False):
            flags.append("stepwise")
        if getattr(self._config.option, "stepwise_reset", False):
            flags.append("stepwise-reset")
        if getattr(self._config.option, "stepwise_skip", False):
            flags.append("stepwise-skip")
        if self._config.option.last_failed_no_failures == "none":
            flags.append("last-failed-no-failures-none")
        return flags

    def select_stepwise_items(self, items: Sequence[pytest.Item]) -> list[int]:
        return self._session.select_stepwise(
            [item.nodeid for item in items], self.selection_flags()
        )

    def select_items(self, items: Sequence[pytest.Item]) -> list[int]:
        mtimes = (
            [item.path.stat().st_mtime_ns for item in items]
            if getattr(self._config.option, "newfirst", False)
            else []
        )
        return self._session.select_items(items, mtimes, self.selection_flags())

    def finish_session(self, result: NativeRunResult) -> None:
        # Pytest subtracts this persisted local timestamp from a naive datetime.now().
        timestamp = datetime.now().isoformat()  # noqa: DTZ005
        self._session.finish_result(result, timestamp)

    def record_collection_report(self, report: CollectReport) -> None:
        self._session.collect_report(
            report.nodeid,
            report.outcome,
            [child.nodeid for child in report.result],
        )

    def finish_collection(self) -> None:
        self._session.finish_collection()

    @override
    def warn(self, fmt: str, *, _ispytest: bool = False, **args: object) -> None:
        del _ispytest
        message = fmt.format(**args) if args else fmt
        warnings.warn(PytestCacheWarning(message), self._config.hook, stacklevel=3)

    def _value_path(self, key: str) -> Path:
        return self._cachedir / "v" / key


class _NativeCacheFileSkipper:
    """Install pytest's file-skip hook after a cached failure is collected."""

    def __init__(self, cache: _NativeCache) -> None:
        self.cache = cache

    @pytest.hookimpl
    def pytest_make_collect_report(self, collector: pytest.Collector) -> CollectReport | None:
        if isinstance(collector, pytest.File) and self.cache._session.skip_collection_file(
            str(collector.path)
        ):
            return CollectReport(collector.nodeid, "passed", longrepr=None, result=[])
        return None


class _NativeCacheCollectionReporter:
    """Apply cache hooks registered during configuration, matching pytest's ordering."""

    def __init__(self, cache: _NativeCache) -> None:
        self.cache = cache
        self.last_failed = bool(cache._config.option.lf)
        if self.last_failed:
            cache._session.configure_last_failed_collection(str(cache._config.rootpath))

    @pytest.hookimpl(specname="pytest_make_collect_report", wrapper=True)
    def pytest_make_collect_report_order(
        self, collector: pytest.Collector
    ) -> Generator[None, CollectReport, CollectReport]:
        report = yield
        if not self.last_failed:
            return report
        kind = (
            "directory"
            if isinstance(collector, (pytest.Session, Directory))
            else "file"
            if isinstance(collector, pytest.File)
            else "other"
        )
        children = [
            (
                child.nodeid,
                str(child.path),
                isinstance(child, pytest.Collector),
                kind == "file" and collector.session.isinitpath(child.path),
            )
            for child in report.result
        ]
        indices, enable_skip = self.cache._session.collection_report_indices(
            kind, str(collector.path), children
        )
        if enable_skip:
            collector.config.pluginmanager.register(
                _NativeCacheFileSkipper(self.cache),
                "_vfy_native_last_failed_collection_skipper",
            )
        if kind == "directory":
            report.result = [report.result[index] for index in indices]
        elif kind == "file":
            report.result[:] = [report.result[index] for index in indices]
        return report

    @pytest.hookimpl
    def pytest_collectreport(self, report: CollectReport) -> None:
        if not parallel.is_worker_collection_controller(self.cache._config):
            self.cache.record_collection_report(report)

    @pytest.hookimpl(specname="pytest_collection_modifyitems")
    def pytest_collection_modifyitems_stepwise(
        self, items: list[pytest.Item], config: Config
    ) -> None:
        """Apply stepwise before collection wrappers resume, as pytest does."""
        if getattr(config.option, "stepwise", False):
            _apply_cache_selection(items, config, self.cache.select_stepwise_items(items))

    @pytest.hookimpl(specname="pytest_collection_modifyitems", wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems_cache_order(
        self, items: list[pytest.Item], config: Config
    ) -> Generator[None, object, object]:
        """Apply last-failed and new-first after ordinary collection wrappers."""
        compact_collection.expand_inventory(items, phase="collection")
        result = yield
        _apply_cache_selection(
            items,
            config,
            self.cache.select_items(items),
            coalesced_duplicates=bool(config.option.newfirst),
        )
        compact_collection.expand_inventory(items, phase="execution")
        return result


def _finish_native_cache(session: Session) -> None:
    cache = getattr(session.config, "cache", None)
    if isinstance(cache, _NativeCache):
        xdist = is_native_xdist(session.config)
        accepted = session.config.stash.get(_cache_preflight_accepted_key, False)
        if xdist:
            # No execution result exists for empty, deselected, or collection-
            # interrupted sessions. Successful selection still owns their cache.
            accepted = (
                cache._session.has_selection
                and not session.config.option.collectonly
                and session.exitstatus
                not in {pytest.ExitCode.INTERNAL_ERROR, pytest.ExitCode.USAGE_ERROR}
            )
        if accepted:
            cache.initialize()
            result = session.config.stash.get(_result_key, None)
            if (result is not None or xdist) and not session.config.option.collectonly:
                try:
                    if xdist:
                        reporter = session.config.pluginmanager.getplugin(
                            "_vfy_native_xdist_cache_reporter"
                        )
                        if not isinstance(reporter, XdistCacheReporter):
                            raise UnsupportedExtensionError(
                                "native pytest-xdist cache adapter is missing"
                            )
                        reporter.finish_outcomes()
                    elif result is not None:
                        cache.finish_session(result)
                except CachePathError as exc:
                    cache.warn(f"could not create cache path {cache._cachedir}: {exc}")
                except CacheWriteError as exc:
                    cache.warn(f"cache could not write native session state: {exc}")
        else:
            try:
                cache.finish_collection()
            except CachePathError as exc:
                cache.warn(f"could not create cache path {cache._cachedir}: {exc}")
            except CacheWriteError as exc:
                cache.warn(f"cache could not write collection state: {exc}")


def _apply_cache_selection(
    items: list[pytest.Item],
    config: Config,
    selected_indices: list[int],
    *,
    coalesced_duplicates: bool = False,
) -> None:
    selected_set = set(selected_indices)
    # Rust's new-first selection retains one item per node ID. Pytest reports
    # exclusions from selection hooks, but silently drops duplicate identities.
    retained_nodeids = (
        {items[index].nodeid for index in selected_indices} if coalesced_duplicates else set()
    )
    deselected = [
        item
        for index, item in enumerate(items)
        if index not in selected_set and item.nodeid not in retained_nodeids
    ]
    items[:] = [items[index] for index in selected_indices]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
