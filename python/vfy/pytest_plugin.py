"""pytest-cov-compatible integration backed by the vfy engine."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import tempfile
from collections.abc import Generator
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, cast

import pytest

from ._pytest.xdist_support import is_native_xdist, is_xdist_controller
from .errors import VfyError
from .native import (
    TestReportAccumulator,
    ci_plan_source_current,
    finalize_ci_plan,
    pytest_coverage_failure_message as _coverage_failure_message,
    pytest_coverage_threshold_failed as _coverage_threshold_failed,
    resolve_cli_config,
    update_test_history,
    write_test_collection,
)
from .process_startup import active_coverage, finish, subprocess_environment
from .pytest_options import (
    StoreReport as StoreReport,
    coverage_config_argument as _config_argument,
    validate_context as validate_context,
    validate_decision_report as validate_decision_report,
    validate_fail_under as validate_fail_under,
    validate_report as validate_report,
)
from .source_roots import source_root_paths
from .watch_reports import (
    stage_coverage_reports as _stage_watch_reports,
    stage_external_pytest_reports as _stage_external_pytest_reports,
)

if TYPE_CHECKING:
    from _pytest.terminal import TerminalReporter
    from pluggy import Result
    from xdist.workermanage import WorkerController

    from ._pytest_native import NativeRunResult
    from .api import Coverage
    from .config_types import ResolvedCommand
    from .pytest_reporting import _VfyHtmlReporter


pytest_plugins = ("vfy.pytest_options",)


_env_coverage: Coverage | None = None
_env_automatic = False
_diagnostic_profile = None
if os.getenv("VFY_PROFILE_DIR"):
    from .diagnostic_profile import DiagnosticProfile

    profile_process = (
        "pytest-in-process" if os.getenv("VFY_IN_PROCESS_PYTEST") == "1" else "pytest-child"
    )
    if profile_worker := os.getenv("PYTEST_XDIST_WORKER"):
        profile_process = f"pytest-worker-{profile_worker}-{os.getpid()}"
    _diagnostic_profile = DiagnosticProfile(profile_process)


def _native_pytest_active(config: pytest.Config) -> bool:
    native_plugin = sys.modules.get("vfy._pytest.native_pytest")
    if (
        native_plugin is None
        or getattr(native_plugin, "_VFY_COVERAGE_BRIDGE", None) != "vfy-serial-coverage-v1"
    ):
        return False
    return config.pluginmanager.get_name(native_plugin) is not None


def _native_coverage_bridge_enabled(config: pytest.Config | None = None) -> bool:
    return (
        os.getenv("VFY_NATIVE_COVERAGE_BRIDGE") == "1"
        and config is not None
        and _native_pytest_active(config)
    )


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    pluginmanager.unregister(name="_vfy_coverage_replay_fixtures")
    replacements = (
        "pytest_cov",
        "pytest_cov.plugin",
    )
    for name, plugin in pluginmanager.list_name_plugin():
        if plugin is not None and (
            name in replacements
            or (isinstance(plugin, ModuleType) and plugin.__name__ in replacements)
        ):
            raise pytest.UsageError(
                f"vfy replaces the {name!r} plug-in; remove its explicit load "
                "and load -p vfy.pytest_plugin before pytest collects conftests"
            )
    # These replacements register the same options, hooks, and fixtures.
    # Block their autoload before pytest replays registration hooks.
    for name in replacements:
        pluginmanager.set_blocked(name)


def _profile_begin() -> int:
    return _diagnostic_profile.begin() if _diagnostic_profile is not None else 0


def _profile_end(name: str, started: int) -> None:
    if _diagnostic_profile is not None:
        _diagnostic_profile.end(name, started)


@pytest.hookimpl(wrapper=True)
def pytest_collection(session: pytest.Session) -> Generator[None, object, object]:
    started = _profile_begin()
    result = yield
    _profile_end("collection", started)
    return result


@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session: pytest.Session) -> Generator[None, object, object]:
    started = _profile_begin()
    result = yield
    _profile_end("execution", started)
    return result


class _VfyTestSelection:
    """Apply controller-selected identities to pytest's final collection."""

    def __init__(self, nodeids: list[str]) -> None:
        self.nodeids = frozenset(nodeids)
        self._modules: frozenset[str] | None = None

    @pytest.hookimpl(tryfirst=True)
    def pytest_ignore_collect(self, collection_path: Path, config: pytest.Config) -> bool | None:
        # Importing a test module costs more than its items; a module without
        # selected tests contributes only deselected items, even when a
        # requested directory includes it. Conftest and package modules still
        # load because they can define hooks and fixtures for selected tests.
        if collection_path.suffix != ".py" or collection_path.name in {
            "conftest.py",
            "__init__.py",
        }:
            return None
        if self._modules is None:
            self._modules = frozenset(
                os.path.realpath(config.rootpath / nodeid.split("::", 1)[0])
                for nodeid in self.nodeids
            )
        return True if os.path.realpath(collection_path) not in self._modules else None

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node: WorkerController) -> None:
        # Remote workers cannot read the controller's temporary selection file.
        node.workerinput["vfy_selected_tests"] = sorted(self.nodeids)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> Generator[None, object, object]:
        # Resolve identities after project plugins finish rewriting collection.
        result = yield
        selected = [item for item in items if item.nodeid in self.nodeids]
        selected_ids = {item.nodeid for item in selected}
        missing = self.nodeids - selected_ids
        if missing:
            message = "vfy failed to collect selected tests: " + ", ".join(sorted(missing))
            if not hasattr(config, "workerinput"):
                raise pytest.UsageError(message)
            # xdist does not forward worker UsageError diagnostics. Report a
            # collection failure and send an empty collection so no test starts.
            items.clear()
            config.hook.pytest_collectreport(
                report=pytest.CollectReport(
                    nodeid="vfy selection", outcome="failed", longrepr=message, result=[]
                )
            )
            return result
        deselected = [item for item in items if item.nodeid not in selected_ids]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
        items[:] = selected
        return result


class _VfyCiCollection:
    """Finalize selection after project collection hooks."""

    def __init__(
        self,
        request_path: str | None,
        final_path: str,
        collection_path: str,
        audit: bool,
        ignored: list[str],
    ) -> None:
        self.request_path = request_path
        self.final_path = final_path
        self.collection_path = collection_path
        self.audit = audit
        self.ignored = ignored

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node: WorkerController) -> None:
        node.workerinput["vfy_ci_collection"] = (
            self.request_path,
            self.final_path,
            self.collection_path,
            self.audit,
            self.ignored,
        )

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> Generator[None, object, object]:
        result = yield
        try:
            collected = [item.nodeid for item in items]
            write_test_collection(self.collection_path, collected)
            if self.request_path is None:
                return result
            if not ci_plan_source_current(self.request_path, self.ignored):
                raise ValueError("source changed after the CI plan was prepared; rerun the command")
            plan = finalize_ci_plan(self.request_path, collected, self.final_path)
        except (OSError, RuntimeError, ValueError) as exc:
            raise pytest.UsageError(f"Failed to finalize vfy CI selection: {exc}") from exc
        if self.audit or config.option.collectonly or plan["fallback_full"]:
            return result
        selected_ids = frozenset(plan["selected"])
        deselected = [item for item in items if item.nodeid not in selected_ids]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item.nodeid in selected_ids]
        return result


class _VfyRunObserver:
    """Capture one controller-owned pytest invocation as a structured batch."""

    def __init__(self, result_path: str, config: pytest.Config) -> None:
        self.result_path = result_path
        self.config = config
        self.reports = TestReportAccumulator()
        self._report_owners: list[tuple[str, str, str, float]] = []
        self.infrastructure_failed = False
        self._native_capability = _native_pytest_active(config)
        self._native_bridge = _native_coverage_bridge_enabled(config)
        self._native_rejected = self._native_capability and not self._native_bridge
        self._native_accepted = False
        self._native_completed = False
        self._native_result: NativeRunResult | None = None
        self._native_infrastructure_failed = False

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when in {"setup", "call", "teardown"}:
            outcome = str(report.outcome)
            if outcome not in {"passed", "failed", "skipped"}:
                self.infrastructure_failed = True
                return
            if hasattr(self.config, "workerinput"):
                # The xdist controller owns result persistence; workers only forward reports.
                return
            nodeid = str(report.nodeid)
            phase = str(report.when)
            duration = max(0.0, float(report.duration) * 1000.0)
            if type(nodeid) is not str or type(phase) is not str or type(outcome) is not str:
                # String subclasses can own observable finalizers or reference cycles.
                self._report_owners.append((nodeid, phase, outcome, duration))
            self.reports.append(nodeid, phase, outcome, duration)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.infrastructure_failed = True

    def pytest_internalerror(
        self, excrepr: object, excinfo: pytest.ExceptionInfo[BaseException]
    ) -> None:
        self.infrastructure_failed = True

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: WorkerController, error: object) -> None:
        if error is not None:
            self.infrastructure_failed = True

    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_sessionfinish(
        self, session: pytest.Session, exitstatus: int
    ) -> Generator[object, object, object]:
        if hasattr(session.config, "workerinput"):
            # Worker adapters supply the native lifecycle listener; upstream
            # xdist reports are persisted only by the controller observer.
            return (yield)
        try:
            result = yield
        except BaseException:
            if self._native_bridge:
                self._finish_native_result(session, infrastructure_failed=True)
            raise
        if self._native_rejected:
            # Candidate invocations without the explicit vfy bridge opt-in
            # reject before execution and must preserve prior result artifacts.
            return result
        if self._native_bridge:
            self._finish_native_result(session, infrastructure_failed=False)
            return result
        self._finish_stock_result(session, exitstatus)
        return result

    def _finish_stock_result(self, session: pytest.Session, exitstatus: int) -> None:
        if exitstatus in {
            pytest.ExitCode.INTERRUPTED,
            pytest.ExitCode.INTERNAL_ERROR,
            pytest.ExitCode.USAGE_ERROR,
            pytest.ExitCode.NO_TESTS_COLLECTED,
        }:
            self.infrastructure_failed = True

        try:
            self.reports.write(self.result_path, self.infrastructure_failed)
        except (OSError, RuntimeError, ValueError):
            # Result evidence must never replace pytest's authoritative status.
            return

    def _finish_native_result(
        self, session: pytest.Session, *, infrastructure_failed: bool
    ) -> None:
        if self._native_completed:
            return
        if not self._native_accepted:
            # A rejected or incomplete preflight must preserve prior result
            # and history artifacts.
            return
        coverage_plugin = self.config.pluginmanager.getplugin("_cov")
        if (
            isinstance(coverage_plugin, VfyCovPlugin)
            and coverage_plugin._native_finalization_failed
        ):
            # The controller records published results in history even when
            # execution was incomplete. A failed coverage commit must publish neither.
            return
        if (
            (directory := os.environ.get("VFY_NATIVE_PARALLEL_DIR")) is not None
            and (index := os.environ.get("VFY_NATIVE_PARALLEL_INDEX")) is not None
            and self.result_path == str(Path(directory) / f"worker{index}.observer.json")
        ):
            # The supervisor consumes the worker transport, and the controller
            # publishes the merged observer result. Keep other destinations live.
            self._native_completed = True
            return
        exitstatus = session.exitstatus
        infrastructure_failed = (
            infrastructure_failed
            or self._native_infrastructure_failed
            or self.infrastructure_failed
            or exitstatus
            in {
                pytest.ExitCode.INTERRUPTED,
                pytest.ExitCode.INTERNAL_ERROR,
                pytest.ExitCode.USAGE_ERROR,
                pytest.ExitCode.NO_TESTS_COLLECTED,
            }
        )
        if self._native_result is not None:
            self._persist_native_result(self._native_result, infrastructure_failed)
            return
        try:
            self.reports.write(
                self.result_path,
                infrastructure_failed if is_native_xdist(self.config) else True,
            )
            self._native_completed = True
        except (OSError, RuntimeError, ValueError):
            return

    def _native_accept(self) -> None:
        """Allow validated native execution to publish vfy results."""
        if not self._native_bridge:
            raise RuntimeError("native pytest result bridge is not active")
        self._native_accepted = True

    def _native_complete(
        self,
        result: NativeRunResult,
        infrastructure_failed: bool,
    ) -> None:
        """Queue native results until Coverage has committed its data."""
        if not self._native_bridge:
            raise RuntimeError("native pytest result bridge is not active")
        self._native_infrastructure_failed = (
            self._native_infrastructure_failed or infrastructure_failed
        )
        if is_native_xdist(self.config) and not hasattr(self.config, "workerinput"):
            # Pytest can change the native base outcome when reporting subtests.
            # Its published reports own controller history and HTML, including -n0.
            return
        self._native_result = result
        if reporter := self._native_html_reporter():
            reporter._native_complete(result)

    def _native_html_reporter(self) -> _VfyHtmlReporter | None:
        # Reporting is an optional plug-in and must not be imported before pytest loads it.
        from .pytest_reporting import _VfyHtmlReporter  # noqa: PLC0415

        reporter = self.config.pluginmanager.getplugin("_vfy_html_reporter")
        return reporter if isinstance(reporter, _VfyHtmlReporter) else None

    def _native_retain_success_output(self) -> bool:
        """Retain successful streams only when vfy will render them."""
        return self._native_html_reporter() is not None or (
            hasattr(self.config, "workerinput")
            and bool(getattr(self.config.option, "htmlpath", None))
        )

    def _native_partial_result(self, result: NativeRunResult) -> None:
        """Retain completed native phases after an accepted runtime failure."""
        if not self._native_bridge:
            raise RuntimeError("native pytest result bridge is not active")
        self._native_infrastructure_failed = True
        if is_native_xdist(self.config) and not hasattr(self.config, "workerinput"):
            return
        self._native_result = result
        if reporter := self._native_html_reporter():
            reporter._native_complete(result)

    def _persist_native_result(self, result: NativeRunResult, infrastructure_failed: bool) -> None:
        try:
            result.write_vfy_result(self.result_path, infrastructure_failed)
        except (OSError, RuntimeError, ValueError):
            return
        self._native_completed = True


def _prepare_cov_source(values: list[str | bool]) -> list[str] | None:
    if True in values or "" in values:
        return None
    roots: list[str] = []
    for value in values:
        source = str(value)
        path = Path(source)
        if path.exists():
            roots.append(str(path.resolve()))
            continue
        # Resolve importable roots through metadata without importing dotted
        # parents before pytest loads project conftests.
        for resolved in source_root_paths([source]):
            resolved_path = Path(resolved)
            roots.append(str(resolved_path.resolve()) if resolved_path.exists() else resolved)
    return roots


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(
    early_config: pytest.Config, parser: pytest.Parser, args: list[str]
) -> None:
    """Activate requested coverage or private mutation selection."""
    options = early_config.known_args_namespace
    private_selection = (
        bool(options.no_cov)
        and os.environ.get("VFY_MUTATION_BASELINE_COVERAGE") == "disabled"
        and _native_coverage_bridge_enabled(early_config)
    )
    if (options.cov_source or private_selection) and not early_config.pluginmanager.hasplugin(
        "_cov"
    ):
        plugin = VfyCovPlugin(options, early_config)
        early_config.pluginmanager.register(plugin, "_cov")


class VfyCovPlugin:
    """Manage coverage collection across pytest's session and worker hooks."""

    def __init__(self, options: argparse.Namespace, config: pytest.Config) -> None:
        initialization_started = _profile_begin()
        self.options = options
        self.config = config
        purpose = os.environ.get("VFY_MUTATION_BASELINE_COVERAGE")
        self._enforce_thresholds = purpose not in {"implicit", "disabled"}
        self.coverage: Coverage | None = None
        self.report = StringIO()
        self.total: float | None = None
        self._started = False
        self._finished = False
        self._native_capability = _native_pytest_active(config)
        self._native_bridge = _native_coverage_bridge_enabled(config)
        self._selection_only = (
            bool(options.no_cov) and purpose == "disabled" and self._native_bridge
        )
        if self._native_capability and not self._native_bridge:
            raise pytest.UsageError(
                "native pytest vfy coverage requires the validated native test route"
            )
        self._native_accepted = False
        self._native_disabled_call = False
        self._native_finalization_failed = False
        self._is_worker = False
        self._worker_payloads: list[tuple[bytes, str]] = []
        self._worker_data_paths: list[str] = []
        self._worker_test_durations: list[tuple[str, float]] = []
        self._test_durations: dict[str, float] = {}
        # Controller result artifacts own history on the vfy test route.
        # Keep standalone timing and the xdist worker payload unchanged.
        self._record_controller_durations = not bool(os.getenv("VFY_TEST_RESULTS"))
        self._failed_workers: list[str] = []
        self._subprocess_scope = None
        self._native_child_data: tempfile.TemporaryDirectory[str] | None = None
        self._inherited_child_data: tempfile.TemporaryDirectory[str] | None = None
        self._fork_worker = False
        self._native_parallel = False
        self._automatic = False
        self._base_context = ""
        self._topdir = str(Path.cwd())
        self._config_file = _config_argument(options.cov_config)
        configured_options = (
            "cov_decisions",
            "cov_fail_under_decisions",
            "cov_fail_under_conditions",
            "cov_fail_under_mcdc",
            "cov_precision",
            "cov_fail_under",
        )
        arguments = {name: getattr(options, name) for name in configured_options}
        arguments.update(
            command="pytest",
            rcfile=None if self._config_file is True else str(self._config_file),
        )
        # The native resolver validates configuration and preserves pytest's explicit options.
        resolved = cast("ResolvedCommand", json.loads(resolve_cli_config(json.dumps(arguments))))
        self._config = resolved["config"]
        if not self._selection_only:
            for name in configured_options:
                setattr(options, name, resolved["arguments"][name])
        if os.environ.get("VFY_NATIVE_PARALLEL_DIR"):
            from ._pytest.parallel import (  # noqa: PLC0415 - only native workers load the engine
                prepare_worker_coverage,
            )

            prepare_worker_coverage(options)
        if not self._selection_only:
            _stage_watch_reports(options, self._config)
        self._source = _prepare_cov_source(options.cov_source)
        self._reports = dict(options.cov_report)
        if not self._reports:
            self._reports = {"term": None}
        elif set(self._reports) == {""}:
            self._reports = {}
        # Private selection owns this collector; public fixtures and reports stay disabled.
        self._disabled = bool(options.no_cov) and not self._selection_only

        self._is_distributed = bool(
            getattr(options, "numprocesses", False)
            or getattr(options, "distload", False)
            or getattr(options, "dist", "no") != "no"
        )
        # xdist workers may import test modules while their pytest
        # configuration is being assembled. Start before any worker
        # configuration hooks so import discovery sees those modules too.
        self._is_worker = hasattr(config, "workerinput")
        if self._is_worker and not self._disabled:
            self._start()
        if not self._disabled and not self._is_distributed:
            self._start()
        _profile_end("plugin_initialization", initialization_started)

    def _start(self) -> None:
        started = _profile_begin()
        if self._started:
            return
        automatic = active_coverage()
        if automatic is not None and self._is_worker and not self._native_bridge:
            # Pytest-cov replaces the worker's automatic startup collector; its
            # explicit collector measures later calls into startup modules.
            finish(save=False)
            automatic = None
        if automatic is not None and self._native_bridge:
            worker_input = getattr(self.config, "workerinput", {})
            inherited_data = worker_input.get("vfy_native_child_data_file")
            if (
                self._is_worker
                and is_native_xdist(self.config)
                and isinstance(inherited_data, str)
                and automatic.data.base_filename() == inherited_data
            ):
                # Execnet inherited this controller's staged subprocess collector.
                # Flush its bootstrap imports before the worker owns native phases.
                finish()
                automatic = None
            else:
                raise pytest.UsageError(
                    "native vfy coverage does not support an enclosing active Coverage session"
                )
        if automatic is not None:
            self.coverage = automatic
            self._automatic = True
            self._base_context = self.coverage.context
            self._started = True
            return
        from .api import Coverage  # noqa: PLC0415 - execution-only runs do not load coverage

        if Coverage.current() is not None:
            raise pytest.UsageError(
                "native vfy coverage does not support an enclosing active Coverage session"
            )
        self.coverage = Coverage(
            source=self._source,
            branch=self.options.cov_branch,
            decisions=self.options.cov_decisions,
            data_suffix=self._is_worker
            or (self._is_distributed and not is_native_xdist(self.config)),
            config_file=self._config_file,
        )
        if self._native_bridge:
            data_path = Path(self.coverage.get_data().data_filename())
            if not data_path.parent.is_dir():
                raise pytest.UsageError(
                    "native pytest vfy coverage requires an existing data-file parent "
                    "directory: "
                    f"{data_path.parent}"
                )
        # Global LINE/PY_START events cover pytest's imports. A second loader
        # hook can leave assertion-rewritten tests uninstrumented on CPython 3.12+.
        self._base_context = self.coverage.context
        if self._native_bridge and self.options.cov_append and not self._is_worker:
            # Keep existing data out of the collector, including the alias-aware
            # start path. Publication reads the append base inside its transaction.
            self.coverage.data._use_suffix(True)
        if not self._native_bridge and not self._is_worker:
            if not self.options.cov_append:
                self.coverage.erase()
            elif self._is_distributed:
                self.coverage.load()
            if not self._is_distributed:
                # Keep prior append data out of the incoming parent shard:
                # publication preserves it as the unremapped combine base.
                self.coverage.data._use_suffix(True)
        self.coverage.start()
        try:
            self._install_subprocess_environment()
        except BaseException:
            self.coverage.stop()
            self._cleanup_native_child_data()
            raise
        self._started = True
        _profile_end("collector_startup", started)

    def _native_accept(self) -> None:
        """Allow the native runner to publish coverage after suite preflight."""
        if not self._native_bridge:
            raise RuntimeError("native pytest coverage bridge is not active")
        self._native_accepted = True

    def _native_parallel_base(self) -> str | None:
        """Reserve worker shards inside this coverage session's staging directory."""
        if self._disabled:
            return None
        if not self._native_bridge or self.coverage is None or self._native_child_data is None:
            raise RuntimeError("native parallel coverage requires an active staging directory")
        self._native_parallel = True
        return str(
            Path(self._native_child_data.name) / Path(self.coverage.data.base_filename()).name
        )

    def _adopt_fork_worker(self, shard: str) -> None:
        """Make this forked child write its own shard and leave the controller's files alone.

        The controller's staging directory also holds every sibling's shard and
        the subprocess shards of all workers. Keep the inherited
        ``TemporaryDirectory`` referenced until the child exits with ``os._exit``:
        collecting it would run its finalizer and delete that directory.
        """
        if self.coverage is None or self._native_child_data is None:
            raise RuntimeError("a forked coverage worker requires an active staging directory")
        self.coverage._adopt_forked_data_file(shard)
        self._inherited_child_data, self._native_child_data = self._native_child_data, None
        self._fork_worker = True

    def _finish_fork_worker(self) -> None:
        """Publish the shard of a forked child; the controller combines and reports."""
        assert self.coverage is not None
        try:
            self.coverage.stop()
            self.coverage._prepare_save()
            self.coverage.data.write()
        finally:
            self._started = False

    def _native_phase(self, nodeid: str, phase: str, no_cover: bool) -> None:
        """Apply vfy's test context at the native runner's phase boundary."""
        if not self._native_bridge:
            raise RuntimeError("native pytest coverage bridge is not active")
        if self.coverage is None:
            return
        if phase not in {"setup", "run", "teardown"}:
            raise ValueError(f"invalid native pytest phase: {phase!r}")
        if self.coverage._started and (self._selection_only or self.options.cov_context == "test"):
            context = f"{nodeid}|{phase}"
            if self._base_context:
                context = f"{self._base_context}|{context}"
            self.coverage.switch_context(context)
        if phase == "run" and no_cover:
            self.coverage.stop()
            self._native_disabled_call = True

    def _native_phase_complete(self, phase: str) -> None:
        """Resume collection after a call excluded by ``no_cover``."""
        if phase != "run":
            raise ValueError(f"invalid native pytest phase completion: {phase!r}")
        if self._native_disabled_call:
            if self.coverage is None:
                raise RuntimeError("native pytest coverage ended during a no_cover call")
            self.coverage.start()
            self._native_disabled_call = False

    def _native_partial_result(self, result: NativeRunResult) -> None:
        """Forward completed native phases to vfy's registered result observer."""
        if not self._native_bridge:
            raise RuntimeError("native pytest coverage bridge is not active")
        observer = self.config.pluginmanager.getplugin("_vfy_run_observer")
        if not isinstance(observer, _VfyRunObserver) or not observer._native_bridge:
            raise RuntimeError("native pytest result observer is not registered")
        observer._native_partial_result(result)

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        if self._native_bridge and not self._finished and not self._disabled:
            self._abort_native_collection()

    def _install_subprocess_environment(self) -> None:
        assert self.coverage is not None
        child_data_file = None
        if self._native_bridge:
            # Collection can spawn children before native preflight accepts the suite.
            # Stage their shards so a rejection leaves the public data file intact.
            base = Path(self.coverage.data.base_filename())
            staging_parent = base.parent
            worker_input = getattr(self.config, "workerinput", {})
            if (
                self._is_worker
                and is_native_xdist(self.config)
                and worker_input.get("vfy_master_host") == socket.gethostname()
                and worker_input.get("vfy_master_topdir") == self._topdir
            ):
                controller_data = worker_input.get("vfy_native_child_data_file")
                if not isinstance(controller_data, str):
                    raise RuntimeError(
                        "native pytest-xdist worker lost its controller coverage staging directory"
                    )
                # A crashed worker cannot clean up its subprocess shards. The
                # collocated controller owns their enclosing staging directory.
                staging_parent = Path(controller_data).parent
            self._native_child_data = tempfile.TemporaryDirectory(
                prefix=".vfy-native-child-", dir=staging_parent
            )
            child_data_file = str(Path(self._native_child_data.name) / base.name)
        self._subprocess_scope = subprocess_environment(self.coverage, data_file=child_data_file)
        self._subprocess_scope.__enter__()

    def _restore_subprocess_environment(self) -> None:
        if self._subprocess_scope is not None:
            self._subprocess_scope.__exit__(None, None, None)
            self._subprocess_scope = None

    def _cleanup_native_child_data(self) -> None:
        if self._native_child_data is not None:
            self._native_child_data.cleanup()
            self._native_child_data = None

    @pytest.hookimpl(tryfirst=True)
    def pytest_configure(self, config: pytest.Config) -> None:
        self.config = config
        self._is_worker = hasattr(config, "workerinput")
        # Start before xdist workers import conftests and test modules during
        # configuration. The controller waits for sessionstart and does not trace tests.
        if (
            not self._disabled
            and not self._started
            and (self._is_worker or not self._is_distributed)
        ):
            self._start()

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        self.config = session.config
        self._is_worker = hasattr(session.config, "workerinput")
        if not self._disabled and not self._started:
            self._start()

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node: WorkerController) -> None:
        node.workerinput.update(
            {
                "vfy_master_host": socket.gethostname(),
                "vfy_master_topdir": self._topdir,
            }
        )
        if self._native_bridge and not self._disabled and is_xdist_controller(self.config):
            assert self.coverage is not None
            assert self._native_child_data is not None
            node.workerinput["vfy_native_child_data_file"] = str(
                Path(self._native_child_data.name) / Path(self.coverage.data.base_filename()).name
            )

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: WorkerController, error: object) -> None:
        output = getattr(node, "workeroutput", {})
        worker_id = str(getattr(node.gateway, "id", "unknown"))
        if "vfy_worker_id" not in output:
            self._failed_workers.append(worker_id)
            return
        payload = output.get("vfy_worker_data")
        if payload is not None:
            self._worker_payloads.append((payload, output["vfy_worker_topdir"]))
        elif self._native_bridge and is_native_xdist(self.config):
            self._worker_data_paths.append(output["vfy_worker_data_file"])
        if self._record_controller_durations:
            self._worker_test_durations.extend(output.get("vfy_test_durations", []))

    def _phase_context(self, item: pytest.Item, phase: str) -> None:
        if (
            self.coverage is not None
            and self.coverage._started
            and (self._selection_only or self.options.cov_context == "test")
        ):
            context = f"{item.nodeid}|{phase}"
            if self._base_context:
                context = f"{self._base_context}|{context}"
            self.coverage.switch_context(context)

    def pytest_runtest_setup(self, item: pytest.Item) -> None:
        self._phase_context(item, "setup")

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, Result[None], None]:
        self._phase_context(item, "run")
        # Custom ``pytest.Item`` subclasses have no fixture names.
        disabled = bool(
            item.get_closest_marker("no_cover") or "no_cover" in getattr(item, "fixturenames", ())
        )
        if disabled and self.coverage is not None:
            self.coverage.stop()
        yield
        if disabled and self.coverage is not None:
            self.coverage.start()

    def pytest_runtest_teardown(self, item: pytest.Item) -> None:
        self._phase_context(item, "teardown")

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Keep one Rust-history timing per test, including xdist workers."""
        if report.when == "call" and (self._is_worker or self._record_controller_durations):
            self._test_durations[report.nodeid] = max(0.0, float(report.duration) * 1000.0)

    def _combine_local_data(self) -> Coverage:
        assert self.coverage is not None
        base = Path(self.coverage.data.base_filename())
        from .api import Coverage  # noqa: PLC0415 - only coverage finalization needs the API

        combining = Coverage(
            data_file=str(base),
            source=self._source,
            branch=self.options.cov_branch,
            decisions=self.options.cov_decisions,
            config_file=self._config_file,
        )
        if base.is_file():
            combining.load()
        paths = []
        if self._is_distributed or not self._native_bridge:
            paths.append(str(base.parent))
        if self._native_child_data is not None:
            paths.append(self._native_child_data.name)
        combining.combine(paths, keep=False)
        return combining

    def _stop_collection(self) -> None:
        if self.coverage is None or not self._started:
            return
        if self._automatic:
            finish()
        else:
            self.coverage.stop()
            self.coverage.save()
        self._restore_subprocess_environment()
        self._started = False

    def _finish_worker(self, config: pytest.Config) -> None:
        assert self.coverage is not None
        if self._native_bridge and is_native_xdist(config):
            from .data import (  # noqa: PLC0415 - native coverage workers finalize their shards
                discover_parallel_files,
            )

            try:
                self.coverage.stop()
                self.coverage._prepare_save()
                child_data = self._native_child_data
                child_paths = (
                    discover_parallel_files(
                        str(Path(child_data.name) / Path(self.coverage.data.base_filename()).name)
                    )
                    if child_data is not None
                    else []
                )
                if child_paths:
                    self.coverage.combine(child_paths, strict=True)
                else:
                    self.coverage.data.write()
            finally:
                self._restore_subprocess_environment()
                self._started = False
                self._cleanup_native_child_data()
        else:
            self._stop_collection()
        # xdist attaches these protocol dictionaries only in a worker process.
        worker_input = getattr(config, "workerinput")  # noqa: B009 - xdist attaches this dictionary to worker Config objects
        worker_output = getattr(config, "workeroutput")  # noqa: B009 - xdist attaches this dictionary to worker Config objects
        collocated = (
            worker_input.get("vfy_master_host") == socket.gethostname()
            and worker_input.get("vfy_master_topdir") == self._topdir
        )
        worker_output["vfy_worker_id"] = worker_input.get("workerid", "worker")
        worker_output["vfy_test_durations"] = list(self._test_durations.items())
        if self._native_bridge and is_native_xdist(config):
            worker_output["vfy_worker_data_file"] = self.coverage.data.data_filename()
        if not collocated:
            if not (self._native_bridge and is_native_xdist(config)):
                self.coverage = self._combine_local_data()
            worker_output.update(
                {
                    "vfy_worker_topdir": self._topdir,
                    "vfy_worker_data": self.coverage.get_data().dumps(),
                }
            )

    def _finish_master(self) -> None:
        from .data import (  # noqa: PLC0415 - only coverage finalization needs storage
            discover_parallel_files,
        )

        started = _profile_begin()
        assert self.coverage is not None
        try:
            if self._native_bridge:
                self.coverage.stop()
                if is_native_xdist(self.config) and self._failed_workers:
                    raise RuntimeError(
                        "native pytest-xdist workers did not publish coverage: "
                        + ", ".join(self._failed_workers)
                    )
                observer = self.config.pluginmanager.getplugin("_vfy_run_observer")
                if (
                    is_native_xdist(self.config)
                    and isinstance(observer, _VfyRunObserver)
                    and observer.infrastructure_failed
                ):
                    raise RuntimeError("native pytest-xdist failed before coverage publication")
                if self._native_parallel and self._native_child_data is None:
                    raise RuntimeError("native parallel coverage lost its staged worker data")
                self.coverage._prepare_save()
                self._merge_worker_payloads()
                child_data = self._native_child_data
                paths = list(self._worker_data_paths)
                base = self.coverage.data.base_filename()
                if child_data is not None and (
                    self._native_parallel
                    or discover_parallel_files(str(Path(child_data.name) / Path(base).name))
                ):
                    paths.append(child_data.name)
                if self.options.cov_append:
                    self.coverage.data._publish(
                        paths,
                        append=True,
                        relative_files=self.coverage.relative_files,
                    )
                elif paths:
                    # Validate parent and child data before replacing the public database.
                    self.coverage.combine(paths, strict=True)
                else:
                    self.coverage.data.write()
            else:
                if not self._is_distributed and not self._automatic:
                    self.coverage.stop()
                    self.coverage._prepare_save()
                    self.coverage.data._publish(
                        [str(Path(self.coverage.data.base_filename()).parent)],
                        append=self.options.cov_append,
                        relative_files=self.coverage.relative_files,
                    )
                else:
                    self._stop_collection()
                    if self._is_distributed or discover_parallel_files(
                        self.coverage.data.base_filename()
                    ):
                        self.coverage = self._combine_local_data()
        finally:
            self._restore_subprocess_environment()
            self._started = False
            self._cleanup_native_child_data()
            for path in self._worker_data_paths:
                Path(path).unlink(missing_ok=True)
        if not self._native_bridge and self._merge_worker_payloads():
            self.coverage.save()
        history_path = os.getenv("VFY_TEST_HISTORY")
        if history_path and not os.getenv("VFY_TEST_RESULTS"):
            records = [*self._test_durations.items(), *self._worker_test_durations]
            if records:
                update_test_history(history_path, records)
        _profile_end("persistence", started)

    def _merge_worker_payloads(self) -> bool:
        from .data import CoverageData  # noqa: PLC0415 - remote coverage payloads need storage

        assert self.coverage is not None
        for payload, worker_topdir in self._worker_payloads:
            data = CoverageData(no_disk=True)
            data.loads(payload)

            aliases = [
                *[list(paths) for paths in self._config.get("paths", {}).values()],
                [self._topdir, worker_topdir],
            ]
            data._store = data._store.remap_paths(aliases)
            self.coverage.get_data().update(data)
        return bool(self._worker_payloads)

    def _precision(self) -> int:
        return int(self.options.cov_precision)

    def _fail_under(self) -> float | None:
        if not self._enforce_thresholds:
            return None
        value = self.options.cov_fail_under
        return None if value is None else float(value)

    def _write_reports(self) -> None:
        started = _profile_begin()
        from .pytest_reports import (  # noqa: PLC0415 - reporting is optional during pytest startup
            coverage_heading,
            write_pytest_reports,
        )

        assert self.coverage is not None
        if self._reports or self._fail_under():
            self.report.write(coverage_heading())
        self.total, semantic = write_pytest_reports(
            self.coverage,
            self._reports,
            self._config,
            self.report,
            precision=self._precision(),
            decisions=self.options.cov_decisions,
            decision_report=self.options.cov_decision_report,
            source=self._source or [],
            history_path=os.getenv("VFY_TEST_HISTORY"),
        )
        if semantic is not None:
            self._semantic = semantic
        _profile_end("reporting", started)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self._native_bridge and not self._native_accepted and not self._disabled:
            self._abort_native_collection()
            return
        if self._finished or self._disabled or self.coverage is None:
            return
        self._finished = True
        try:
            if self._is_worker:
                self._finish_worker(session.config)
                return
            if self._fork_worker:
                self._finish_fork_worker()
                return
            self._finish_master()
        except BaseException:
            if self._native_bridge:
                self._native_finalization_failed = True
            raise
        if self._selection_only:
            return
        self._finish_reports(session)

    def _finish_reports(self, session: pytest.Session) -> None:
        assert self.coverage is not None
        failed = bool(session.testsfailed)
        if not failed:
            from .pytest_reports import (  # noqa: PLC0415 - only selected runs rebuild coverage
                reconstruct_selected_coverage,
            )

            reconstruct_selected_coverage(self.coverage)
        should_report = (
            self._reports or self._fail_under() is not None or self.options.cov_decisions
        ) and not (failed and self.options.no_cov_on_fail)
        if should_report:
            try:
                with self.coverage._report_batch():
                    self._write_reports()
            except (OSError, VfyError, ValueError, re.error) as exc:
                self.total = 0.0
                self.report.write(f"WARNING: Failed to generate report: {exc}\n")
        threshold = self._fail_under()
        if (
            threshold is not None
            and not self.options.collectonly
            and self.total is not None
            and _coverage_threshold_failed(self.total, threshold, self._precision())
        ):
            reporter = session.config.pluginmanager.getplugin("terminalreporter")
            if reporter is not None:
                message = _coverage_failure_message(self.total, threshold, self._precision())
                reporter.write(f"\nERROR: {message}\n", red=True, bold=True)
            session.testsfailed += 1
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        if self.options.cov_decisions and hasattr(self, "_semantic") and self._enforce_thresholds:
            semantic = self._semantic
            thresholds = (
                (self.options.cov_fail_under_decisions, semantic.decision_percent, "decisions"),
                (self.options.cov_fail_under_conditions, semantic.condition_percent, "conditions"),
                (self.options.cov_fail_under_mcdc, semantic.mcdc_percent, "MC/DC"),
            )
            for minimum, actual, label in thresholds:
                if minimum is not None and actual < minimum:
                    session.exitstatus = pytest.ExitCode.TESTS_FAILED
                    self.report.write(
                        f"FAIL Required semantic {label} coverage of {minimum:g}% not reached; "
                        f"actual {actual:.2f}%\n"
                    )

    def _abort_native_collection(self) -> None:
        """Release preflight-only collection without replacing existing data."""
        if self.coverage is None or not self._started:
            return
        self.coverage.stop()
        self._restore_subprocess_environment()
        self._cleanup_native_child_data()
        self._started = False
        self._finished = True

    def pytest_terminal_summary(self, terminalreporter: TerminalReporter) -> None:
        if self._disabled or self.total is None:
            return
        terminalreporter.write_sep("=", "tests coverage")
        if self._failed_workers:
            terminalreporter.write(
                "Workers without coverage data: " + ", ".join(self._failed_workers) + "\n"
            )
        terminalreporter.write(self.report.getvalue())
        threshold = self._fail_under()
        if threshold is not None and threshold > 0:
            # pytest-cov labels the line with the unrounded total; the exit status
            # uses the rounded comparison in pytest_sessionfinish.
            failed = self.total < threshold
            prefix = "FAIL " if failed else ""
            reached = "not reached" if failed else "reached"
            terminalreporter.write(
                f"{prefix}Required test coverage of {threshold:g}% {reached}. "
                f"Total coverage: {self.total:.2f}%\n",
                red=failed,
                bold=failed,
                green=not failed,
            )


@pytest.fixture
def no_cover() -> None:
    """Disable coverage while the test call is executing."""


@pytest.fixture
def cov(request: pytest.FixtureRequest) -> Coverage | None:
    """Return the active vfy ``Coverage`` object."""
    plugin = request.config.pluginmanager.getplugin("_cov")
    if isinstance(plugin, VfyCovPlugin):
        return None if plugin.options.no_cov else plugin.coverage
    return _env_coverage


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Register the marker and support ``vfy coverage run -m pytest``."""
    global _env_automatic, _env_coverage
    if not config.pluginmanager.hasplugin("vfy.pytest_reporting"):
        _stage_external_pytest_reports(config)
    config.addinivalue_line("markers", "no_cover: disable coverage for this test")
    selection_path = os.getenv("VFY_CONFIRM_FAILURES") or os.getenv("VFY_SELECTED_TESTS")
    if hasattr(config, "workerinput"):
        selected = config.workerinput.get("vfy_selected_tests")
    elif selection_path:
        try:
            selected = json.loads(Path(selection_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise pytest.UsageError(f"Failed to read vfy selected tests: {selection_path}") from exc
        if not isinstance(selected, list) or not all(
            isinstance(nodeid, str) and nodeid for nodeid in selected
        ):
            raise pytest.UsageError(f"Invalid vfy selected tests: {selection_path}")
    else:
        selected = None
    if selected is not None:
        config.pluginmanager.register(_VfyTestSelection(selected), "_vfy_test_selection")
    ci_collection = (
        config.workerinput.get("vfy_ci_collection") if hasattr(config, "workerinput") else None
    )
    if ci_collection is None and os.getenv("VFY_CI_COLLECTION"):
        ci_collection = (
            os.getenv("VFY_CI_REQUEST"),
            os.environ["VFY_CI_FINAL"],
            os.environ["VFY_CI_COLLECTION"],
            os.getenv("VFY_CI_AUDIT") == "1",
            json.loads(os.getenv("VFY_CI_IGNORED", "[]")),
        )
    if ci_collection is not None:
        config.pluginmanager.register(_VfyCiCollection(*ci_collection), "_vfy_ci_collection")
    result_path = os.getenv("VFY_TEST_RESULTS")
    if result_path and (not hasattr(config, "workerinput") or is_native_xdist(config)):
        config.pluginmanager.register(
            _VfyRunObserver(result_path, config),
            "_vfy_run_observer",
        )
    if not os.getenv("VFY_CLI_ACTIVE") or not hasattr(config, "workerinput"):
        return
    if _env_coverage is not None:
        return
    _env_coverage = active_coverage()
    if _env_coverage is not None:
        _env_automatic = True
        return
    from .api import Coverage  # noqa: PLC0415 - environment-configured coverage is opt-in

    _env_coverage = Coverage(
        data_file=os.getenv("VFY_DATA_FILE", ".coverage"),
        data_suffix=True,
        branch=os.getenv("VFY_BRANCH") == "1",
        decisions=os.getenv("VFY_DECISIONS") == "1",
    )
    _env_coverage.start()


def pytest_runtest_setup(item: pytest.Item) -> None:
    if _env_coverage is not None and _env_coverage._started:
        _env_coverage.switch_context(item.nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, Result[None], None]:
    # Custom ``pytest.Item`` subclasses have no fixture names.
    disabled = bool(
        item.get_closest_marker("no_cover") or "no_cover" in getattr(item, "fixturenames", ())
    )
    if disabled and _env_coverage is not None:
        _env_coverage.stop()
    yield
    if disabled and _env_coverage is not None:
        _env_coverage.start()


def pytest_runtest_teardown(item: pytest.Item) -> None:
    if _env_coverage is not None and _env_coverage._started:
        _env_coverage.switch_context("")


def pytest_unconfigure(config: pytest.Config) -> None:
    global _env_automatic, _env_coverage
    if _env_coverage is None:
        return
    if _env_automatic:
        finish()
    else:
        _env_coverage.stop()
        _env_coverage.save()
    _env_coverage = None
    _env_automatic = False
