"""Pytest lifecycle adapters for the Rust test engine."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Generator, Sequence
from functools import partial
from types import ModuleType
from typing import TYPE_CHECKING, cast

import pytest
from _pytest import stepwise as _pytest_stepwise
from _pytest.config import Config
from _pytest.pathlib import resolve_from_str
from _pytest.python import PyCollector
from _pytest.terminal import TerminalReporter, WarningReport

from vfy.pytest_metadata import metadata_key

from . import compact_collection, lazy_function, parallel, parameter_family
from .benchmark_support import (
    disable_worker_benchmarks,
    trusted_pytest_benchmark_plugin,
    worker_benchmarks_note,
)
from .case_counts import _expanded_count, _ExpandedDeselectedItems, _item_case_count
from .execution_record import (
    _execution_record_identity,
    _native_record_environment as _native_record_environment,
    _publish_or_retain_execution_record,
)
from .mock_support import trusted_pytest_mock_plugin
from .native_cache import _finish_native_cache, _NativeCache, _NativeCacheCollectionReporter
from .native_junit import (
    _disable_junit_reporter,
    _junit_global_properties,
    _junit_options,
    _junit_retains_passing_output,
    _NativeJUnitCollectionReporter,
    _retains_native_junit_locations,
    _validate_junit_configuration,
    case_xml_attributes as case_xml_attributes,
)
from .native_state import (
    _NATIVE_PLUGIN_MODULE,
    _VFY_COVERAGE_BRIDGE as _VFY_COVERAGE_BRIDGE,
    NativeExecutionSession,
    NativeParallelWorker,
    NativeRunResult,
    UnsupportedExtensionError,
    UnsupportedFeatureError,
    _cache_preflight_accepted_key,
    _interrupted_result_key,
    _native_collection_key,
    _native_junit_collection_reports_key,
    _parallel_exit_key,
    _parallel_worker_key,
    _result_key,
    _session_cleanup_error_key,
    _session_warnings_key,
    _worker_benchmark_fixture_key,
    _worker_benchmarks_note_key,
    _worker_collection_reports_key,
    run_native_suite,
)
from .native_warnings import _NativeSessionWarningRecorder, _publish_native_warnings
from .platformdirs_support import trusted_platformdirs_plugin
from .plugin_support import (
    native_compatible_entry_points,
    restore_user_plugin_autoload,
    trusted_native_compatible_plugin,
    worker_seed_options,
)
from .plugin_validation import (
    _guard_plugin_registration,
    _validate_plugin_before_registration,
    _validate_timeout_configuration,
)
from .run_preparation import (
    _adopt_worker_collection,
    _native_xdist_protocol,
    _NativeWorkerCollectionController,
    _NativeWorkerCollectionReporter,
    _prepare_native_run,
    _publish_worker_collection,
)
from .run_retention import (
    retain_interrupted_result as retain_interrupted_result,
    retain_session_cleanup_error as retain_session_cleanup_error,
)
from .terminal_capture import (
    _capture_summary_chars,
    _publish_terminal_capture_reports,
    _render_native_terminal_summary,
    _retains_native_capture_locations,
    _terminal_reports_success_capture,
)
from .timeout_support import TimeoutSettings, trusted_pytest_timeout_plugin
from .unittest_support import (
    normalize_unittest_skip as normalize_unittest_skip,
    subtest_xfail_matcher as subtest_xfail_matcher,
    unittest_subtest_report_callback as unittest_subtest_report_callback,
)
from .upstream_adaptation import (
    apply_upstream_warning_defaults as apply_upstream_warning_defaults,
    compatibility_policy,
    fixture_scope_mismatch_message as fixture_scope_mismatch_message,
    raise_protocol_teardown_error as raise_protocol_teardown_error,
)
from .vfy_bridge import (
    _accept_native_child_results,
    _native_bridge_adapters,
    _vfy_bridge_plugin_enabled,
)
from .xdist_cache import XdistCacheReporter
from .xdist_junit import atomic_junit_report
from .xdist_support import (
    is_native_xdist,
    is_xdist_controller,
    is_xdist_worker,
    trusted_xdist_plugin,
)

if TYPE_CHECKING:
    from _pytest.config import PytestPluginManager
    from _pytest.fixtures import FixtureRequest
    from _pytest.main import Session
    from xdist.workermanage import WorkerController

pytest_plugins = ("vfy.asyncio_plugin",)


@pytest.hookimpl(trylast=True)
def pytest_deselected(items: Sequence[pytest.Item]) -> None:
    """Keep pytest's terminal deselection count equal to expanded cases."""
    if not items:
        return
    terminal = items[0].config.pluginmanager.getplugin("terminalreporter")
    if terminal is None:
        return
    repeated = [(item, count - 1) for item in items if (count := _item_case_count(item)) > 1]
    if not repeated:
        return
    stats = terminal.stats
    deselected = stats.get("deselected")
    if not isinstance(deselected, _ExpandedDeselectedItems):
        deselected = _ExpandedDeselectedItems(deselected or ())
        stats["deselected"] = deselected
    for item, extra in repeated:
        deselected.add_repetitions(item, extra)


@pytest.hookimpl(tryfirst=True, optionalhook=True)
def pytest_timeout_set_timer(item: pytest.Item, settings: TimeoutSettings) -> None:
    # Parameter marks can stay compact through collection; fixtures can also add
    # marks at runtime. Guard the actual hook before upstream can start a timer.
    if settings.method == "thread":
        raise UnsupportedExtensionError(
            "native pytest supports pytest-timeout's signal method, not the thread "
            f"method ({item.nodeid}); use method='signal' or run this workflow with pytest"
        )
    if threading.current_thread() is not threading.main_thread():
        # Upstream silently substitutes a hard-exit thread timer in this case.
        raise UnsupportedExtensionError(
            "native pytest requires pytest-timeout signal timers to run in the main thread; "
            "run pytest in the main thread or disable pytest-timeout with -p no:timeout"
        )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_finish(session: Session) -> None:
    """Report case counts while collection stores one template per family."""
    _validate_timeout_configuration(session.config, session.items)
    terminal = session.config.pluginmanager.getplugin("terminalreporter")
    if terminal is not None:
        terminal._numcollected = _expanded_count(session.items) + len(
            terminal.stats.get("deselected", ())
        )
        get_reports_to_display = terminal._get_reports_to_display

        def expanded_reports_to_display(name: str) -> list[object]:
            deselected = terminal.stats.get(name)
            if name == "deselected" and isinstance(deselected, _ExpandedDeselectedItems):
                return deselected.filtered_for_display()
            return cast(list[object], get_reports_to_display(name))

        terminal._get_reports_to_display = expanded_reports_to_display


@pytest.hookimpl(wrapper=True)
def pytest_collection(session: Session) -> Generator[None, object, object]:
    """Restore the expanded count after pytest finalizes the collected items."""
    result = yield
    session.testscollected = _expanded_count(session.items)
    return result


@pytest.fixture
def cache(request: FixtureRequest) -> _NativeCache:
    value = request.config.cache
    assert isinstance(value, _NativeCache)
    return value


@pytest.hookimpl
def pytest_addoption(parser: pytest.Parser) -> None:
    # pytest blocks its stepwise plugin together with cacheprovider. Keep the
    # upstream parser contract while native execution owns the state machine.
    _pytest_stepwise.pytest_addoption(parser)
    group = parser.getgroup("general")
    group.addoption("--lf", "--last-failed", action="store_true", dest="lf")
    group.addoption("--ff", "--failed-first", action="store_true", dest="failedfirst")
    group.addoption("--nf", "--new-first", action="store_true", dest="newfirst")
    group.addoption("--cache-show", action="append", nargs="?", dest="cacheshow")
    group.addoption("--cache-clear", action="store_true", dest="cacheclear")
    group.addoption(
        "--lfnf",
        "--last-failed-no-failures",
        action="store",
        dest="last_failed_no_failures",
        choices=("all", "none"),
        default="all",
    )
    cache_dir_default = ".pytest_cache"
    if os.getenv("TOX_ENV_DIR"):
        cache_dir_default = os.path.join(os.environ["TOX_ENV_DIR"], cache_dir_default)
    parser.addini("cache_dir", default=cache_dir_default, help="Cache directory path")


def pytest_plugin_registered(
    plugin: object, plugin_name: str, manager: PytestPluginManager
) -> None:
    """Guard future registrations and validate hooks replayed from earlier ones."""
    module = plugin.__name__ if isinstance(plugin, ModuleType) else type(plugin).__module__
    if module == _NATIVE_PLUGIN_MODULE:
        _guard_plugin_registration(manager)
        return
    config = manager.get_plugin("pytestconfig")
    if plugin_name == "funcmanage" and isinstance(config, Config) and lazy_function.enabled(config):
        lazy_function.install_sorter(config, manager, plugin)
    _validate_plugin_before_registration(plugin, plugin_name, manager)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: WorkerController) -> None:
    from .xdist_codec import install_decoder  # noqa: PLC0415 - optional execnet dependency

    install_decoder(node)
    # Module-name xdist activation skips the ordering plugins' entry-point detection.
    for name, seed in worker_seed_options(node.config).items():
        node.workerinput.setdefault(name, seed)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: Config) -> None:
    compatibility_policy(config)
    if is_native_xdist(config):
        # Early registrations can precede parsing of pytest-xdist options.
        for name, plugin in config.pluginmanager.list_name_plugin():
            if plugin is not None:
                _validate_plugin_before_registration(plugin, name, config.pluginmanager)
    if is_native_xdist(config) and parallel.is_parallel():
        raise UnsupportedExtensionError(
            "native pytest cannot combine vfy's --workers/-j/-n with pytest-xdist; "
            "remove pytest-xdist options or plug-in requests"
        )
    parallel.prepare_worker(config)
    config.stash[_native_collection_key] = not parallel.is_parallel() and not is_native_xdist(
        config
    )
    compact_collection.native_mutation_controller(config)
    _validate_timeout_configuration(config)
    if not config.stash[_native_collection_key]:
        lazy_function.install(config)
    config.add_cleanup(parameter_family.install())
    if config.option.help:
        # Pytest completes help after configuration; unsupported execution
        # services must not suppress the retained parser's help output.
        return
    upstream_warnings: list[tuple[str, str, int, str]] = []
    config.stash[_session_warnings_key] = upstream_warnings
    warning_recorder = _NativeSessionWarningRecorder(upstream_warnings)
    config.pluginmanager.register(warning_recorder, "_vfy_native_session_warning_recorder")
    config.add_cleanup(lambda: config.pluginmanager.unregister(warning_recorder))
    _configure_native_stepwise(config)
    capture_mode = config.getoption("capture", "fd")
    if capture_mode not in {"fd", "sys", "no"}:
        raise UnsupportedExtensionError(
            f"native pytest does not implement --capture={capture_mode}; "
            "use --capture=fd, --capture=sys, or --capture=no"
        )
    unsupported = {
        "usepdb": "--pdb",
        "trace": "--trace",
        "setupshow": "--setup-show",
        "setuponly": "--setup-only",
        "setupplan": "--setup-plan",
        "cacheclear": "--cache-clear",
        "cacheshow": "--cache-show",
    }
    for option, spelling in unsupported.items():
        if getattr(config.option, option, None):
            raise UnsupportedExtensionError(f"native pytest does not implement {spelling}")
    if config.pluginmanager.hasplugin("cacheprovider"):
        raise UnsupportedExtensionError(
            "native pytest uses its Rust cache service; disable the stock cacheprovider"
        )
    if config.getoption("showcapture", default="all") not in {
        "all",
        "stdout",
        "stderr",
        "log",
        "no",
    }:
        raise UnsupportedExtensionError(
            "native pytest supports --show-capture=all, stdout, stderr, log, and no"
        )
    if config.getoption("tbstyle", default="auto") == "line":
        raise UnsupportedExtensionError(
            "native pytest supports --tb=auto, long, short, native, and no"
        )
    _validate_junit_configuration(config)
    cache = _NativeCache(resolve_from_str(config.getini("cache_dir"), config.rootpath), config)
    config.__dict__["cache"] = cache
    config.pluginmanager.register(
        _NativeCacheCollectionReporter(cache),
        "_vfy_native_cache_collection_reporter",
    )
    if is_native_xdist(config):
        config.pluginmanager.register(XdistCacheReporter(cache), "_vfy_native_xdist_cache_reporter")
    if config.option.xmlpath:
        collection_reports: list[tuple[str, str, str, str, int | None]] = []
        config.stash[_native_junit_collection_reports_key] = collection_reports
        config.pluginmanager.register(
            _NativeJUnitCollectionReporter(collection_reports),
            "_vfy_native_junit_collection_reporter",
        )


def _configure_native_stepwise(config: Config) -> None:
    """Keep pytest's stepwise parser options but leave its state machine to Rust."""
    if config.pluginmanager.hasplugin("stepwise"):
        config.pluginmanager.unregister(name="stepwise")
    if getattr(config.option, "stepwise_reset", False) or getattr(
        config.option, "stepwise_skip", False
    ):
        config.option.stepwise = True


@pytest.hookimpl(specname="pytest_sessionfinish")
def pytest_sessionfinish_cleanup(session: Session) -> None:
    """Raise retained fixture cleanup failures at pytest's session boundary."""
    error = session.config.stash.get(_session_cleanup_error_key, None)
    if error is not None:
        del session.config.stash[_session_cleanup_error_key]
        raise error


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session: Session) -> Generator[None, None, None]:
    if parallel.is_worker() and parallel.worker_collection_enabled(session.config):
        _publish_worker_collection(session)
    if (
        is_native_xdist(session.config)
        and compact_collection.native_mutation_controller(session.config) is None
    ):
        from .native_xdist import finish_session  # noqa: PLC0415 - optional worker adapter

        finish_session(session)
        with atomic_junit_report(session.config):
            yield
        if not is_xdist_worker(session.config) and not is_xdist_controller(session.config):
            _finish_native_cache(session)
        return
    collection_warnings: list[tuple[str, str, int, str]] = []
    if parallel.is_worker():
        collection_warnings = session.config.stash[_session_warnings_key].copy()
        session.config.stash[_session_warnings_key].clear()
    try:
        yield
    finally:
        parallel.finish_worker(session)
    if parallel.is_worker():
        worker = session.config.stash.get(_parallel_worker_key, None)
        result = session.config.stash.get(_result_key, None)
        if worker is not None and result is not None:
            worker.complete(
                result,
                {"global_properties": _junit_global_properties(session.config)},
                parallel.worker_metadata(session.config),
                collection_warnings,
                session.config.stash.get(_session_warnings_key, []),
            )
        elif (
            worker is not None
            and (reason := session.config.stash.get(_parallel_exit_key, None)) is not None
        ):
            worker.complete_exit(reason, session.exitstatus)
        return
    result = session.config.stash.get(_result_key, None)
    if parallel.is_parallel() and result is not None:
        warnings = result.merge_worker_warning_records(
            session.config.stash.get(_session_warnings_key, []), "finish"
        )
        session.config.stash[_session_warnings_key].clear()
        _publish_or_retain_execution_record(
            session.config,
            result,
            _execution_record_identity(session.config),
            worker_warnings=warnings,
        )
    _finish_native_cache(session)
    result = session.config.stash.get(_result_key, None)
    if result is None:
        result = session.config.stash.get(_interrupted_result_key, None)
    xmlpath = session.config.option.xmlpath
    if result is not None and xmlpath:
        destination = os.path.abspath(os.path.expandvars(os.path.expanduser(xmlpath)))
        result.write_junit(destination, _junit_options(session, result))


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makeitem(collector: object, name: str, obj: object) -> None:
    """Bind compact collection to native-owned collectors before pytest collects."""
    del name, obj
    if isinstance(collector, PyCollector) and collector.config.stash.get(
        _native_collection_key, False
    ):
        compact_collection.bind(collector)


@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session: Session) -> None:
    parallel.silence_worker_terminal(session.config)
    if parallel.is_worker():
        disable_worker_benchmarks(session.config)
    admitted = native_compatible_entry_points()
    external = sorted(
        {
            distribution.metadata["Name"]
            for plugin, distribution in session.config.pluginmanager.list_plugin_distinfo()
            if distribution.metadata["Name"].lower()
            not in ({"pytest", "vfy"} if _vfy_bridge_plugin_enabled(session.config) else {"pytest"})
            and not (
                distribution.metadata["Name"].lower() == "pytest-mock"
                and isinstance(plugin, ModuleType)
                and trusted_pytest_mock_plugin(plugin, plugin.__name__)
            )
            and not (
                distribution.metadata["Name"].lower() == "pytest-benchmark"
                and trusted_pytest_benchmark_plugin(plugin, getattr(plugin, "__name__", ""))
            )
            and not (
                distribution.metadata["Name"].lower() == "pytest-timeout"
                and isinstance(plugin, ModuleType)
                and trusted_pytest_timeout_plugin(plugin, plugin.__name__)
            )
            and not (
                distribution.metadata["Name"].lower() == "platformdirs"
                and isinstance(plugin, ModuleType)
                and trusted_platformdirs_plugin(plugin, plugin.__name__)
            )
            and not (
                distribution.metadata["Name"].lower() == "pytest-xdist"
                and isinstance(plugin, ModuleType)
                and trusted_xdist_plugin(plugin, plugin.__name__)
            )
            and not trusted_native_compatible_plugin(plugin, admitted)
        }
    )
    if external:
        names = ", ".join(external)
        raise UnsupportedExtensionError(
            f"native pytest does not execute third-party pytest plugins: {names}; "
            "remove those plugins or run this workflow with pytest"
        )
    config = session.config
    if is_xdist_worker(config):
        from .xdist_codec import install_encoder  # noqa: PLC0415 - optional execnet dependency

        install_encoder(config)
    parallel.validate_worker_collection(config)
    if parallel.worker_collection_enabled(config):
        if compact_collection.native_mutation_controller(config) is not None:
            raise UnsupportedExtensionError(
                "experimental worker collection does not support mutations"
            )
        if parallel.is_worker():
            reports: list[str] = []
            config.stash[_worker_collection_reports_key] = reports
            config.pluginmanager.register(
                _NativeWorkerCollectionReporter(reports),
                "_vfy_native_worker_collection_reporter",
            )
        else:
            config.pluginmanager.register(
                _NativeWorkerCollectionController(), "_vfy_native_worker_collection_controller"
            )


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session: Session) -> bool | None:
    """Compile and execute native cases while honoring pytest's maxfail threshold."""
    return _native_runtestloop(session)


def _native_runtestloop(
    session: Session, prepared_execution: NativeExecutionSession | None = None
) -> bool | None:
    if (
        is_native_xdist(session.config)
        and compact_collection.native_mutation_controller(session.config) is None
    ):
        from .native_xdist import run_loop  # noqa: PLC0415 - optional worker adapter

        return run_loop(session)
    if _prepare_native_run(session):
        return True
    config = session.config
    collection_failures = session.testsfailed
    capture_mode = config.getoption("capture", "fd")
    capture = capture_mode != "no"
    maxfail = _native_maxfail(config.option.maxfail)
    stepwise = bool(getattr(config.option, "stepwise", False))
    stepwise_skip = bool(getattr(config.option, "stepwise_skip", False))
    run_maxfail = (2 if stepwise_skip else 1) if stepwise else maxfail
    if stepwise and maxfail:
        run_maxfail = min(run_maxfail, maxfail)
    vfy_bridge = _vfy_bridge_plugin_enabled(config)
    record_identity = _execution_record_identity(config)
    try:
        result = _run_native_execution(
            session,
            capture,
            run_maxfail,
            collection_failures,
            vfy_bridge,
            prepared_execution=prepared_execution,
        )
    except UnsupportedFeatureError as error:
        raise UnsupportedExtensionError(f"native pytest: {error}") from error
    config.stash[_cache_preflight_accepted_key] = True
    session.config.stash[_result_key] = result
    if config.option.xmlpath:
        # Pluggy snapshots ordinary hook implementations at dispatch start, so
        # removing the reporter during pytest_sessionfinish is too late.
        _disable_junit_reporter(config)
    if vfy_bridge:
        observer = session.config.pluginmanager.getplugin("_vfy_run_observer")
        if observer is not None:
            observer._native_complete(
                result,
                bool(collection_failures or result.collected == 0),
            )
    terminal = session.config.pluginmanager.getplugin("terminalreporter")
    if terminal is not None:
        _publish_terminal_capture_reports(config, result, terminal)
    _publish_native_warnings(config, result)
    _publish_or_retain_execution_record(config, result, record_identity)
    session.testsfailed = collection_failures + result.failed + result.errors
    if maxfail and session.testsfailed >= maxfail:
        session.shouldfail = f"stopping after {session.testsfailed} failures"
        if session.items:
            raise session.Failed(session.shouldfail)
    failure_count = result.failed + result.errors
    if stepwise and failure_count and (not stepwise_skip or failure_count > 1):
        session.shouldstop = "Test failed, continuing from this test next run."
        raise session.Interrupted(session.shouldstop)
    if session.shouldfail:
        raise session.Failed(session.shouldfail)
    if session.shouldstop:
        raise session.Interrupted(session.shouldstop)
    return True


def _run_worker_collection_controller(
    session: Session, maxfail: int, initial_failures: int, vfy_bridge: bool
) -> NativeRunResult:
    config = session.config
    if vfy_bridge:
        _native_bridge_adapters(session)
    result = parallel.run_bootstrap(
        session,
        maxfail,
        initial_failures,
        partial(_adopt_worker_collection, session),
        retain_success_output=(
            _retains_native_success_output(config) or bool(getattr(config.option, "htmlpath", None))
        ),
    )
    if result is not None:
        _record_worker_benchmarks_note(session)
    if result is None:
        cache = config.cache
        if (
            session.shouldfail
            and isinstance(cache, _NativeCache)
            and not cache._session.has_selection
        ):
            # An early maxfail collection abort never reached selection or runtestloop.
            raise session.Failed(session.shouldfail)
        _prepare_native_run(session)
        raise UnsupportedExtensionError("native worker collection returned no execution result")
    result.merge_worker_warning_records(config.stash.get(_session_warnings_key, []), "collect")
    if vfy_bridge:
        _accept_native_child_results(session)
        config.stash[metadata_key].update(result.worker_metadata_records())
    return result


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> bool | None:
    """Execute the item supplied by pytest or upstream xdist inside Rust."""
    if not _native_xdist_protocol(item.config):
        return None
    from .native_xdist import run_protocol  # noqa: PLC0415 - optional worker adapter

    return run_protocol(item, nextitem)


def _run_native_session(
    session: Session,
    capture: bool,
    maxfail: int,
    initial_failures: int,
    vfy_bridge: bool,
    *,
    items: Sequence[pytest.Item] | None = None,
    parallel_worker: NativeParallelWorker | None = None,
) -> NativeRunResult:
    run_items = session.items if items is None else items
    coverage, observer = _native_bridge_adapters(session) if vfy_bridge else (None, None)
    restore_user_plugin_autoload()
    return run_native_suite(
        run_items,
        capture=capture,
        maxfail=maxfail,
        initial_failures=initial_failures,
        vfy_coverage=coverage,
        vfy_results=observer,
        retain_success_output=(
            _retains_native_success_output(session.config)
            or (vfy_bridge and os.environ.get("VFY_NATIVE_PARALLEL_RETAIN_OUTPUT") == "1")
        ),
        retain_report_locations=(
            parallel.is_worker()
            and (
                parallel.worker_collection_enabled(session.config)
                or parallel.is_collected_fork_child(session.config)
            )
            and _retains_native_junit_locations(session.config)
        ),
        # A worker's controller renders its summary without these items, so it
        # needs every location; this process resolves only rendered cases.
        retain_capture_report_locations=(
            parallel.is_worker() and _retains_native_capture_locations(session.config)
        ),
        capture_summary="" if parallel.is_worker() else _capture_summary_chars(session.config),
        retain_html_extras=(
            bool(getattr(session.config.option, "htmlpath", None))
            or parallel.worker_html_enabled(session.config)
        ),
        parallel_worker=parallel_worker,
    )


def _run_native_file_groups(session: Session, vfy_bridge: bool) -> NativeRunResult:
    """Run explicit mutation file groups and return their merged Rust result."""
    from vfy.mutation_parallel import (  # noqa: PLC0415 - lazy import for file isolation
        run_file_groups,
    )

    group_result = run_file_groups(session)
    if group_result.status != "completed":
        returncode = 124 if group_result.status == "timeout" else 3
        pytest.exit(
            group_result.detail or "native pytest file-group execution failed",
            returncode=returncode,
        )
    if group_result.native_result is None:
        pytest.exit("native pytest file-group runner returned no merged result", returncode=3)
    if vfy_bridge:
        _accept_native_child_results(session)
    return group_result.native_result


def _run_native_execution(
    session: Session,
    capture: bool,
    maxfail: int,
    initial_failures: int,
    vfy_bridge: bool,
    *,
    prepared_execution: NativeExecutionSession | None = None,
) -> NativeRunResult:
    if parallel.is_worker_collection_controller(session.config):
        return _run_worker_collection_controller(session, maxfail, initial_failures, vfy_bridge)
    if os.environ.get("VFY_MUTATION_TEST_ISOLATION") == "file":
        return _run_native_file_groups(session, vfy_bridge)
    if parallel.is_worker():
        worker = session.config.stash.get(_parallel_worker_key, None)
        if worker is None:
            worker = parallel.open_worker(session)
            session.config.stash[_parallel_worker_key] = worker
        try:
            if prepared_execution is not None:
                return prepared_execution.execute_collected(
                    worker, maxfail=maxfail, initial_failures=initial_failures
                )
            return _run_native_session(
                session,
                capture,
                maxfail,
                initial_failures,
                vfy_bridge,
                items=[session.items[index] for index in worker.indices],
                parallel_worker=worker,
            )
        except pytest.exit.Exception as error:
            session.config.stash[_parallel_exit_key] = error.msg
            raise
    if parallel.is_parallel() and session.items:
        if vfy_bridge:
            _native_bridge_adapters(session)
        if getattr(session.config.option, "stepwise", False):
            raise UnsupportedExtensionError("native stepwise execution requires --workers=1")
        result = parallel.run(
            session,
            maxfail,
            initial_failures,
            retain_success_output=(
                _retains_native_success_output(session.config)
                or bool(getattr(session.config.option, "htmlpath", None))
            ),
        )
        if result is not None:
            _record_worker_benchmarks_note(session)
        if result is None:
            return _run_native_session(
                session,
                capture,
                maxfail,
                initial_failures,
                vfy_bridge,
            )
        result.merge_worker_warning_records(
            session.config.stash.get(_session_warnings_key, []), "collect"
        )
        if vfy_bridge:
            _accept_native_child_results(session)
            session.config.stash[metadata_key].update(result.worker_metadata_records())
        return result
    return _run_native_session(
        session,
        capture,
        maxfail,
        initial_failures,
        vfy_bridge,
    )


def _retains_native_success_output(config: Config) -> bool:
    return _junit_retains_passing_output(config) or _terminal_reports_success_capture(config)


def _native_maxfail(value: int | None) -> int:
    if value is None:
        return 0
    if value < 0:
        return 1
    return min(value, sys.maxsize)


def _record_worker_benchmarks_note(session: Session) -> None:
    note = worker_benchmarks_note(
        session.config,
        session.config.stash.get(_worker_benchmark_fixture_key, False)
        or any("benchmark" in getattr(item, "fixturenames", ()) for item in session.items),
    )
    if note is not None:
        session.config.stash[_worker_benchmarks_note_key] = note


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_terminal_summary(
    terminalreporter: TerminalReporter, config: Config
) -> Generator[None, None, None]:
    """Render the native summary while keeping pytest's transient report stats compatible."""
    if is_native_xdist(config):
        yield
        return
    result = config.stash.get(_result_key, None)
    if result is None:
        result = config.stash.get(_interrupted_result_key, None)
    if result is None:
        yield
        return
    if parallel.is_parallel() and not parallel.is_worker():
        warnings = result.merge_worker_warning_records(
            config.stash.get(_session_warnings_key, []), "finish"
        )
        config.stash[_session_warnings_key].clear()
        for message, filename, lineno, nodeid in warnings:
            terminalreporter._add_stats(
                "warnings",
                [WarningReport(message=message, nodeid=nodeid, fslocation=(filename, lineno))],
            )
        _publish_or_retain_execution_record(
            config, result, _execution_record_identity(config), worker_warnings=warnings
        )
    requested_durations = config.option.durations
    config.option.durations = None
    try:
        yield
    finally:
        config.option.durations = requested_durations
    note = config.stash.get(_worker_benchmarks_note_key, None)
    if note is not None:
        terminalreporter.write_line(note)
    _render_native_terminal_summary(terminalreporter, config, result)
