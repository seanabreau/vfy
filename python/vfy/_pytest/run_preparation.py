"""Worker collection hand-off and pre-execution validation for native runs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from _pytest.config import Config
from _pytest.reports import CollectReport

from . import compact_collection, parallel
from .collection_readiness import report_collection_readiness
from .native_cache import _NativeCache
from .native_state import (
    _NATIVE_PLUGIN_MODULE,
    NativeParallelWorker,
    UnsupportedExtensionError,
    _session_warnings_key,
    _worker_benchmark_fixture_key,
    _worker_collection_adopted_key,
    _worker_collection_published_key,
    _worker_collection_reports_key,
)
from .native_warnings import _publish_warning_records
from .plugin_validation import _validate_conftest
from .xdist_support import is_native_xdist, is_serial_xdist, is_xdist_controller, is_xdist_worker

if TYPE_CHECKING:
    from _pytest.main import Session


class _NativeWorkerCollectionController:
    """Skip traversal after earlier public collection callbacks, like xdist."""

    @pytest.hookimpl
    def pytest_collection(self, session: Session) -> bool:
        return True


def _publish_worker_collection(session: Session) -> None:
    if session.config.stash.get(_worker_collection_published_key, False):
        return
    cache = session.config.cache
    if not isinstance(cache, _NativeCache):
        raise UnsupportedExtensionError(
            "native worker collection requires the native cache adapter"
        )
    NativeParallelWorker.publish_collection(
        os.environ["VFY_NATIVE_PARALLEL_DIR"],
        int(os.environ["VFY_NATIVE_PARALLEL_INDEX"]),
        [item.nodeid for item in session.items],
        session.config.stash[_worker_collection_reports_key],
        session.config.stash[_session_warnings_key],
        session.testsfailed,
        (
            cache._session.export_collection_snapshot()
            if os.environ["VFY_NATIVE_PARALLEL_INDEX"] == "0"
            else None
        ),
        any("benchmark" in getattr(item, "fixturenames", ()) for item in session.items),
    )
    session.config.stash[_worker_collection_published_key] = True


class _NativeWorkerCollectionReporter:
    """Serialize public collection reports at the fresh-worker boundary."""

    def __init__(self, reports: list[str]) -> None:
        self.reports = reports

    @pytest.hookimpl
    def pytest_collectreport(self, report: CollectReport) -> None:
        if not report.passed:
            self.reports.append(json.dumps(report._to_json()))


def _adopt_worker_collection(
    session: Session,
    collected: int,
    reports: list[str],
    warning_records: list[tuple[str, str, int, str]],
    errors: int,
    cache_snapshot: str | None,
    finish_warnings: list[tuple[str, str, int, str]],
    benchmark_fixture: bool,
) -> None:
    config = session.config
    cache = config.cache
    if not isinstance(cache, _NativeCache) or cache_snapshot is None:
        raise UnsupportedExtensionError("native worker collection requires a cache snapshot")
    cache._session.adopt_collection_snapshot(cache_snapshot)
    config.stash[_worker_collection_adopted_key] = True
    config.stash[_worker_benchmark_fixture_key] = benchmark_fixture
    # xdist forwards each distinct longrepr; worker-specific diagnostics survive.
    seen: set[str] = set()
    for encoded in reports:
        payload = json.loads(encoded)
        longrepr_key = json.dumps(payload["longrepr"], sort_keys=True)
        if longrepr_key in seen:
            continue
        seen.add(longrepr_key)
        if isinstance(payload["longrepr"], list):
            # JSON loses the skipped report's tuple; execnet preserves it in xdist.
            payload["longrepr"] = tuple(payload["longrepr"])
        report = CollectReport._from_json(payload)
        config.hook.pytest_collectreport(report=report)
    session.testscollected = collected
    terminal = config.pluginmanager.getplugin("terminalreporter")
    if terminal is not None:
        terminal._numcollected = collected
        if errors:
            _publish_warning_records(config, warning_records)
            _publish_warning_records(config, finish_warnings)
    if not errors:
        _prepare_native_run(session)


@dataclass(frozen=True)
class _XdistProtocolRoute:
    plugins: tuple[object | None, ...]
    execute_native: bool


_xdist_protocol_key = pytest.StashKey[_XdistProtocolRoute]()


def _native_xdist_protocol(config: Config) -> bool:
    if compact_collection.native_mutation_controller(config) is not None:
        return False
    manager = config.pluginmanager
    plugins = tuple(
        manager.getplugin(name)
        for name in (_NATIVE_PLUGIN_MODULE, "xdist", "xdist.plugin", "dsession")
    )
    previous = config.stash.get(_xdist_protocol_key, None)
    if previous is not None and all(
        current is verified for current, verified in zip(plugins, previous.plugins, strict=True)
    ):
        return previous.execute_native
    # Registration retains origin validation. Recheck routing when a plugin is
    # added, removed, or replaced without rediscovering distributions per item.
    execute_native = (
        is_native_xdist(config) and not is_serial_xdist(config) and not is_xdist_controller(config)
    )
    config.stash[_xdist_protocol_key] = _XdistProtocolRoute(plugins, execute_native)
    return execute_native


def _prepare_native_run(session: Session) -> bool:
    """Validate lifecycle hooks and dispatch the collection-only mutation host."""
    config = session.config
    if parallel.is_worker() and parallel.worker_collection_enabled(config):
        _publish_worker_collection(session)
    if parallel.is_worker_collection_controller(config) and not config.stash.get(
        _worker_collection_adopted_key, False
    ):
        return False
    if config.option.collectonly:
        report_collection_readiness(session)
        return True
    mutation_controller = compact_collection.native_mutation_controller(config)
    if (
        session.testsfailed
        and not config.option.continue_on_collection_errors
        and mutation_controller is None
        and not (is_native_xdist(config) and is_xdist_worker(config))
    ):
        count = session.testsfailed
        raise session.Interrupted(f"{count} error{'s' if count != 1 else ''} during collection")
    for plugin in config.pluginmanager._conftest_plugins:
        _validate_conftest(plugin, config.pluginmanager)
    capture_manager = config.pluginmanager.getplugin("capturemanager")
    if capture_manager is not None:
        # Imported test modules may retain pytest's capture streams. Suspend
        # capture for Rust execution without closing those live references.
        # Upstream protocols keep fd stdin capture active between items. The
        # borrowed worker capture must snapshot that same state on its first case.
        borrow_fd_capture = _native_xdist_protocol(config) and capture_manager._method == "fd"
        capture_manager.suspend_global_capture(in_=not borrow_fd_capture)
    if mutation_controller is not None:
        # Only forked children execute mutations. Publishing the collection-only
        # parent's result would incorrectly record an empty run.
        mutation_controller.run_native_mutations(session)
        return True
    config.pluginmanager.__dict__["_native_execution_started"] = True
    if (parallel.is_worker() and parallel.worker_collection_enabled(config)) or is_xdist_worker(
        config
    ):
        cache = config.cache
        if not isinstance(cache, _NativeCache):
            raise UnsupportedExtensionError(
                "native worker collection requires the native cache adapter"
            )
        # Controllers persist selection; worker cache fixtures use the disk service.
        cache._session.release_worker_collection()
    return False
