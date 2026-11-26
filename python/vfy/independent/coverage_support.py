"""Coverage lifecycle adapter for the independent test frontend."""

from __future__ import annotations

import json
import os
import warnings
from collections.abc import Generator, Mapping, Sequence
from contextlib import ExitStack
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Protocol, cast

from vfy.errors import VfyError
from vfy.native import (
    _pytest,
    pytest_coverage_failure_message as _coverage_failure_message,
    pytest_coverage_threshold_failed as _coverage_threshold_failed,
    resolve_cli_config,
)
from vfy.process_startup import active_coverage, child_options, finish, subprocess_environment
from vfy.pytest_options import (
    CoverageOptionParser,
    coverage_config_argument,
    register_coverage_options,
)
from vfy.pytest_reports import coverage_heading, reconstruct_selected_coverage, write_pytest_reports
from vfy.watch_reports import stage_coverage_reports

from .facade import PytestWarning, fixture, hookimpl

if TYPE_CHECKING:
    from vfy._pytest_native import NativeIndependentRequest
    from vfy.api import Coverage
    from vfy.config_types import ResolvedCommand

    from .protocol import Config, Item, Session
    from .terminal import TerminalReporter


class CoverageOptions(Protocol):
    """Values constructed by the shared coverage argument declarations."""

    cov_source: list[str | bool]
    cov_config: str
    cov_branch: bool | None
    cov_decisions: bool
    cov_append: bool
    cov_context: str | None
    cov_report: dict[str, str | None]
    cov_precision: int | None
    cov_fail_under: float | None
    cov_fail_under_decisions: float | None
    cov_fail_under_conditions: float | None
    cov_fail_under_mcdc: float | None
    cov_decision_report: str | None
    no_cov: bool
    no_cov_on_fail: bool


class PytestCovWarning(PytestWarning):
    """Base category for independent coverage integration warnings."""


class CovDisabledWarning(PytestCovWarning):
    """Coverage was requested after the option disabling it."""


class CovReportWarning(PytestCovWarning):
    """Measured coverage could not produce the requested reports."""


def pytest_addoption(parser: CoverageOptionParser) -> None:
    """Register the same options as vfy's pytest-cov integration."""
    register_coverage_options(parser)


@hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: Config, args: Sequence[str]) -> None:
    """Start before conftests can import measured product modules."""
    if os.environ.get("VFY_MUTATION_REPLAY") == "1":
        return
    options = cast("CoverageOptions", early_config.known_args_namespace)
    if options.no_cov and os.environ.get("VFY_MUTATION_BASELINE_COVERAGE") == "disabled":
        coverage = active_coverage()
        if coverage is None:
            raise _pytest.IndependentUsageError(
                "private mutation baseline requires its provisioned selection collector"
            )
        early_config.pluginmanager.register(CoverageContexts(coverage), "_cov_contexts")
    if options.cov_source and not early_config.pluginmanager.hasplugin("_cov"):
        plugin = IndependentCoverage(options)
        disabled = False
        for argument in args:
            if argument == "--no-cov":
                disabled = True
            elif argument.startswith("--cov") and disabled:
                plugin.warn_disabled = True
                break
        early_config.pluginmanager.register(plugin, "_cov")
        early_config.add_cleanup(plugin.close)
        if options.cov_context == "test" and plugin.coverage is not None:
            early_config.pluginmanager.register(CoverageContexts(plugin.coverage), "_cov_contexts")


def pytest_configure(config: Config) -> None:
    """Keep the no-cover marker available during unmeasured sessions."""
    config.addinivalue_line("markers", "no_cover: disable coverage for this test.")
    plugin = config.pluginmanager.getplugin("_cov")
    if (
        isinstance(plugin, IndependentCoverage)
        and plugin.coverage is not None
        and not plugin._worker
    ):
        stage_coverage_reports(plugin.options, plugin.config)


@fixture
def no_cover() -> None:
    """Disable coverage for the test call when this fixture is requested."""


@fixture
def cov(request: NativeIndependentRequest) -> Coverage | None:
    """Return this session's active vfy coverage object."""
    config = cast("Config", request.config)
    plugin = config.pluginmanager.getplugin("_cov")
    return plugin.coverage if isinstance(plugin, IndependentCoverage) else None


class CoverageContexts:
    """Apply the public setup, call, and teardown context hooks."""

    def __init__(self, coverage: Coverage) -> None:
        self.coverage = coverage
        self._base_context = coverage.context

    def _phase_context(self, item: Item, phase: str) -> None:
        # pytest-cov switches contexts only while coverage is started.
        if not self.coverage._started:
            return
        value = f"{item.nodeid}|{phase}"
        self.coverage.switch_context(
            f"{self._base_context}|{value}" if self._base_context else value
        )

    def pytest_runtest_setup(self, item: Item) -> None:
        self._phase_context(item, "setup")

    def pytest_runtest_call(self, item: Item) -> None:
        self._phase_context(item, "run")

    def pytest_runtest_teardown(self, item: Item) -> None:
        self._phase_context(item, "teardown")


class IndependentCoverage:
    """Bind native coverage collection to independent public lifecycle hooks."""

    def __init__(self, options: CoverageOptions) -> None:
        self.options = options
        self._enforce_thresholds = os.environ.get("VFY_MUTATION_BASELINE_COVERAGE") != "implicit"
        self.report = StringIO()
        self.total: float | None = None
        self.coverage: Coverage | None = None
        self._scope = ExitStack()
        self._finished = False
        self._worker = "VFY_NATIVE_PARALLEL_DIR" in os.environ
        self.warn_disabled = False
        if options.no_cov:
            return
        self._initial_directory: Path = Path.cwd()
        if self._worker:
            self.coverage = active_coverage()
            if self.coverage is None:
                raise _pytest.UnsupportedFeatureError(
                    "independent worker coverage requires a parent-provisioned shard collector"
                )
            return
        from vfy.api import Coverage  # noqa: PLC0415 - unmeasured sessions avoid collector startup.

        if Coverage.current() is not None:
            raise _pytest.IndependentUsageError(
                "independent vfy coverage does not support an enclosing active Coverage session"
            )
        config_file = coverage_config_argument(options.cov_config)
        names = (
            "cov_decisions",
            "cov_fail_under_decisions",
            "cov_fail_under_conditions",
            "cov_fail_under_mcdc",
            "cov_precision",
            "cov_fail_under",
        )
        arguments = {name: getattr(options, name) for name in names}
        arguments.update(command="pytest", rcfile=None if config_file is True else config_file)
        resolved = cast("ResolvedCommand", json.loads(resolve_cli_config(json.dumps(arguments))))
        self.config = resolved["config"]
        self._display_threshold = options.cov_fail_under
        for name in names:
            setattr(options, name, resolved["arguments"][name])
        if self._display_threshold is None:
            self._display_threshold = options.cov_fail_under
        source = (
            None if True in options.cov_source else [str(value) for value in options.cov_source]
        )
        self.coverage = Coverage(
            source=source,
            branch=options.cov_branch,
            decisions=options.cov_decisions,
            data_suffix=True,
            config_file=config_file,
        )
        child_directory = self._scope.enter_context(
            TemporaryDirectory(prefix="vfy-independent-cov-")
        )
        self._child_data = str(
            Path(child_directory) / Path(self.coverage.data.base_filename()).name
        )
        try:
            self.coverage.start()
            if "multiprocessing" in self.coverage.concurrency:
                from vfy.multiprocessing_support import (  # noqa: PLC0415 - only multiprocessing runs need child staging.
                    measure_children,
                )

                measure_children(self.coverage, data_file=self._child_data)
            if "subprocess" in self.coverage.get_option("run:patch"):
                self._scope.enter_context(
                    subprocess_environment(self.coverage, data_file=self._child_data)
                )
        except BaseException:
            self.coverage.stop()
            self._scope.close()
            raise

    def worker_options(self) -> Mapping[str, object] | None:
        """Provision owned workers independently of ordinary subprocess collection."""
        if self.coverage is None or self._worker:
            return None
        return child_options(self.coverage, self._child_data)

    @hookimpl(wrapper=True)
    def pytest_runtest_call(self, item: Item) -> Generator[None, object, object]:
        disabled = bool(item.get_closest_marker("no_cover") or "no_cover" in item.fixturenames)
        if disabled and self.coverage is not None:
            self.coverage.stop()
        try:
            return (yield)
        finally:
            if disabled and self.coverage is not None:
                self.coverage.start()

    def pytest_sessionstart(self, session: Session) -> None:
        """Retain the session used by the native execution boundary."""
        self.session = session

    @hookimpl(wrapper=True)
    def pytest_runtestloop(self, session: Session) -> Generator[None, object, object]:
        result = yield
        self._finish(session)
        return result

    def _finish(self, session: Session) -> None:
        if self._finished or self.coverage is None:
            return
        with ExitStack() as directory_scope:
            try:
                previous_directory = Path.cwd()
            except OSError:
                previous_directory = None
            os.chdir(self._initial_directory)
            if previous_directory is not None:
                directory_scope.callback(os.chdir, previous_directory)
            if self._worker:
                finish()
                self._finished = True
                return
            try:
                self.coverage.stop()
                self.coverage._prepare_save()
                self.coverage.data._publish(
                    [str(Path(self._child_data).parent)],
                    append=self.options.cov_append,
                    relative_files=self.coverage.relative_files,
                )
                if not session.testsfailed:
                    reconstruct_selected_coverage(self.coverage)
                if not (session.testsfailed and self.options.no_cov_on_fail):
                    self._write_reports(session)
            finally:
                self._finished = True
                self.coverage.stop()
                self._scope.close()

    def _write_reports(self, session: Session) -> None:
        assert self.coverage is not None
        reports = dict(self.options.cov_report) or {"term": None}
        if set(reports) == {""}:
            reports = {}
        if not (reports or self.options.cov_fail_under or self.options.cov_decisions):
            return
        precision = self.options.cov_precision
        assert precision is not None
        reporter = cast(
            "TerminalReporter | None", session.config.pluginmanager.getplugin("terminalreporter")
        )
        if reports or self.options.cov_fail_under:
            self.report.write(coverage_heading())
        try:
            total, semantic = write_pytest_reports(
                self.coverage,
                reports,
                self.config,
                self.report,
                precision=precision,
                decisions=self.options.cov_decisions,
                decision_report=self.options.cov_decision_report,
                source=self.coverage.source,
                ignore_errors=True,
            )
        except VfyError as error:
            message = f"Failed to generate report: {error}\n"
            if reporter is not None:
                reporter.write(f"\nWARNING: {message}\n", red=True, bold=True)
            warnings.warn(CovReportWarning(message), stacklevel=1)
            total, semantic = 0.0, None
        self.total = total
        threshold = self.options.cov_fail_under if self._enforce_thresholds else None
        if threshold is not None and _coverage_threshold_failed(total, threshold, precision):
            if reporter is not None:
                reporter.write(
                    f"\nERROR: {_coverage_failure_message(total, threshold, precision)}\n",
                    red=True,
                    bold=True,
                )
            session.testsfailed += 1
            session.exitstatus = 1
        if semantic is not None and self._enforce_thresholds:
            for threshold, actual in (
                (self.options.cov_fail_under_decisions, semantic.decision_percent),
                (self.options.cov_fail_under_conditions, semantic.condition_percent),
                (self.options.cov_fail_under_mcdc, semantic.mcdc_percent),
            ):
                if threshold is not None and actual < threshold:
                    session.testsfailed += 1
                    session.exitstatus = 1

    def pytest_terminal_summary(self, terminalreporter: TerminalReporter) -> None:
        if self.options.no_cov:
            if self.warn_disabled:
                terminalreporter.write_sep("=", "tests coverage")
                message = "Coverage disabled via --no-cov switch!"
                terminalreporter.write(f"WARNING: {message}\n", red=True, bold=True)
                warnings.warn(CovDisabledWarning(message), stacklevel=1)
            return
        if self.total is None:
            return
        report = self.report.getvalue()
        threshold = self.options.cov_fail_under if self._enforce_thresholds else None
        if report or (threshold is not None and threshold > 0):
            terminalreporter.write_sep("=", "tests coverage")
        if report:
            terminalreporter.write(report)
        if threshold is not None and threshold > 0:
            precision = self.options.cov_precision
            assert precision is not None
            failed = self.total < threshold
            prefix = "FAIL " if failed else ""
            reached = "not reached" if failed else "reached"
            terminalreporter.write(
                f"{prefix}Required test coverage of {self._display_threshold}% {reached}. "
                f"Total coverage: {self.total:.2f}%\n",
                red=failed,
                bold=failed,
                green=not failed,
            )

    def close(self) -> None:
        if not self._finished:
            if self._worker and self.coverage is not None:
                finish()
            if self.coverage is not None:
                self.coverage.stop()
            self._scope.close()
