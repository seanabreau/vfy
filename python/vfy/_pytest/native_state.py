"""Stash keys, constants, and extension handles shared by the native pytest adapter."""

from __future__ import annotations

import pytest

from vfy.native import _pytest, native_pytest_supported_conftest_hooks

from . import PytestCompatibilityPolicy

CachePathError = _pytest.CachePathError
CacheWriteError = _pytest.CacheWriteError
NativeCacheService = _pytest.NativeCacheService
NativeCacheSession = _pytest.NativeCacheSession
NativeExecutionSession = _pytest.NativeExecutionSession
NativeParallelWorker = _pytest.NativeParallelWorker
NativeRunResult = _pytest.NativeRunResult
NativeRequest = _pytest.NativeRequest
UnsupportedFeatureError = _pytest.UnsupportedFeatureError
run_native_suite = _pytest.run_native_suite


_result_key = pytest.StashKey[NativeRunResult]()
_interrupted_result_key = pytest.StashKey[NativeRunResult]()
_session_cleanup_error_key = pytest.StashKey[BaseException]()
_parallel_worker_key = pytest.StashKey[NativeParallelWorker]()
_parallel_exit_key = pytest.StashKey[str]()
_worker_collection_reports_key = pytest.StashKey[list[str]]()
_worker_collection_adopted_key = pytest.StashKey[bool]()
_worker_collection_published_key = pytest.StashKey[bool]()
_native_collection_key = pytest.StashKey[bool]()
_cache_preflight_accepted_key = pytest.StashKey[bool]()
_compatibility_policy_key = pytest.StashKey[PytestCompatibilityPolicy]()
_session_warnings_key = pytest.StashKey[list[tuple[str, str, int, str]]]()
_native_junit_reporter_key = pytest.StashKey[object]()
_native_junit_collection_reports_key = pytest.StashKey[
    list[tuple[str, str, str, str, int | None]]
]()
SUPPORTED_CONFTEST_HOOKS = frozenset(native_pytest_supported_conftest_hooks())

_VFY_COVERAGE_BRIDGE = "vfy-serial-coverage-v1"
_NATIVE_PLUGIN_MODULE = "vfy._pytest.native_pytest"
_NATIVE_ADAPTER_MODULES = frozenset(
    {
        _NATIVE_PLUGIN_MODULE,
        "vfy._pytest.native_cache",
        "vfy._pytest.native_junit",
        "vfy._pytest.native_warnings",
        "vfy._pytest.run_preparation",
    }
)
_VFY_ASYNCIO_PLUGIN_MODULE = "vfy.asyncio_plugin"
_EXECUTION_PROBE_MODULE = "pytest_execution_probe"
_EXECUTION_RECORDER_NAME = "_vfy_execution_recorder"


UnsupportedExtensionError = pytest.UsageError


_worker_benchmarks_note_key = pytest.StashKey[str]()
_worker_benchmark_fixture_key = pytest.StashKey[bool]()
