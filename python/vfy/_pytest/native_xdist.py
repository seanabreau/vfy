"""Execute upstream pytest protocols with one live native worker session."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict, cast

import pytest
from _pytest._code.code import (
    ReprExceptionInfo,
    ReprFileLocation,
    ReprTracebackNative,
    TerminalRepr,
)
from _pytest.reports import TestReport

from vfy.native import _pytest

from . import native_pytest
from .plugin_support import restore_user_plugin_autoload
from .xdist_support import is_serial_xdist, is_xdist_controller, is_xdist_worker

if TYPE_CHECKING:
    from warnings import WarningMessage

    from vfy._pytest_native import NativeExecutionSession, _PytestPhaseReport


@dataclass(frozen=True)
class _WorkerExecution:
    executor: NativeExecutionSession
    indices: dict[pytest.Item, int]
    publisher: _PhasePublisher


class _HtmlCase(TypedDict):
    nodeid: str
    extras: list[dict[str, str]]


_execution_key = pytest.StashKey[_WorkerExecution]()


def _execution(session: pytest.Session) -> _WorkerExecution:
    config = session.config
    existing = config.stash.get(_execution_key, None)
    if existing is not None:
        return existing
    native_pytest._prepare_native_run(session)
    bridge = native_pytest._vfy_bridge_plugin_enabled(config)
    coverage, observer = native_pytest._native_bridge_adapters(session) if bridge else (None, None)
    publisher = _PhasePublisher()
    restore_user_plugin_autoload()
    executor = _pytest.NativeExecutionSession(
        session.items,
        capture=config.getoption("capture", "fd") != "no",
        vfy_coverage=coverage,
        vfy_results=observer,
        # Live pytest reports read capture sections from items. Keep the Rust
        # copy only for consumers that render or publish the execution record.
        retain_success_output=bool(
            os.environ.get("PYTEST_NATIVE_RESULT") or getattr(config.option, "htmlpath", None)
        ),
        retain_html_extras=bool(getattr(config.option, "htmlpath", None)),
        pytest_reporter=publisher if getattr(config.option, "htmlpath", None) else None,
        pytest_phase_reporter=(
            None if getattr(config.option, "htmlpath", None) else publisher.publish_phase
        ),
        pytest_protocol_reporter=publisher.publish_protocol if is_serial_xdist(config) else None,
        warning_filter_ownership="native" if is_serial_xdist(config) else "pytest_protocol",
    )
    execution = _WorkerExecution(
        executor, {item: index for index, item in enumerate(session.items)}, publisher
    )
    config.stash[_execution_key] = execution
    config.stash[native_pytest._cache_preflight_accepted_key] = True
    return execution


def _report_longrepr(
    item: pytest.Item,
    value: str | tuple[str, int, str] | TerminalRepr | None,
    canonical: str | None,
) -> str | tuple[str, int, str] | TerminalRepr | None:
    # A failure with a traceback arrives as pytest's own representation; only
    # diagnostics without one are rebuilt from text.
    if not isinstance(value, str) or canonical is None or value.startswith("[XPASS(strict)]"):
        return value
    file, line, _domain = item.location
    traceback = value if value.endswith("\n") else value + "\n"
    return ReprExceptionInfo(
        ReprTracebackNative(traceback.splitlines(keepends=True)),
        ReprFileLocation(str(file), (line or 0) + 1, canonical),
    )


class _PhasePublisher:
    """Publish one completed phase before Rust enters the next phase."""

    item: pytest.Item | None = None

    def publish_protocol(self, item: pytest.Item, event: Literal["start", "finish"]) -> None:
        if event == "start":
            self.item = item
            item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        else:
            item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)

    def __call__(self, result: _pytest.NativeRunResult, start: float, stop: float) -> bool:
        item = self.item
        assert item is not None
        report = result.last_pytest_report()
        extras: list[dict[str, str]] | None = (
            [] if getattr(item.config.option, "htmlpath", None) else None
        )
        if report[1] == "teardown" and extras is not None:
            # The paired Rust serializer owns this wire schema; json.loads erases its type.
            html_cases = cast(list[_HtmlCase], json.loads(result.html_results_json()))
            if html_cases:
                extras = html_cases[0]["extras"]
        return self.publish_phase(report, start, stop, extras=extras)

    def publish_phase(
        self,
        projection: _PytestPhaseReport,
        start: float,
        stop: float,
        *,
        extras: list[dict[str, str]] | None = None,
    ) -> bool:
        item = self.item
        assert item is not None
        nodeid, phase, outcome, seconds, longrepr, wasxfail, _sections, canonical = projection
        sections = [
            (f"Captured {key} {when}", content) for when, key, content in item._report_sections
        ]
        report = TestReport(
            nodeid=nodeid,
            location=item.location,
            keywords=dict.fromkeys(item.keywords, 1),
            outcome=outcome,
            longrepr=_report_longrepr(item, longrepr, canonical),
            when=phase,
            sections=sections,
            duration=seconds,
            start=start,
            stop=stop,
            user_properties=item.user_properties,
        )
        if extras is not None:
            report.__dict__["vfy_html_extras"] = extras
        if wasxfail is not None:
            report.wasxfail = wasxfail
        item.ihook.pytest_runtest_logreport(report=report)
        return bool(item.session.shouldfail or item.session.shouldstop)


def run_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> bool:
    """Run one xdist dispatch and publish pytest reports."""
    execution = _execution(item.session)
    execution.publisher.item = item
    index = execution.indices[item]
    next_index = None if nextitem is None else execution.indices[nextitem]
    hook = item.ihook
    hook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    try:
        warnings = execution.executor.execute_protocol(
            index,
            next_index,
            maxfail=native_pytest._native_maxfail(item.config.option.maxfail),
            initial_failures=item.session.testsfailed,
        )
    except _pytest.UnsupportedFeatureError as error:
        raise native_pytest.UnsupportedExtensionError(f"native pytest: {error}") from error
    finally:
        hook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    _publish_warning_messages(item.config, warnings)
    return True


def run_loop(session: pytest.Session) -> bool | None:
    """Keep the zero-worker suite local and leave distribution to upstream xdist."""
    if is_serial_xdist(session.config):
        return _run_serial(session)
    if is_xdist_controller(session.config) and native_pytest._vfy_bridge_plugin_enabled(
        session.config
    ):
        native_pytest._accept_native_child_results(session)
    return None


def _run_serial(session: pytest.Session) -> bool:
    """Run disabled-xdist execution with native protocol ownership and pytest reports."""
    if session.config.option.collectonly:
        native_pytest._prepare_native_run(session)
        return True
    execution = _execution(session)
    try:
        warnings = execution.executor.execute_suite(
            maxfail=native_pytest._native_maxfail(session.config.option.maxfail),
            initial_failures=session.testsfailed,
        )
    except _pytest.UnsupportedFeatureError as error:
        raise native_pytest.UnsupportedExtensionError(f"native pytest: {error}") from error
    _publish_warning_messages(session.config, warnings)
    if session.shouldfail:
        raise session.Failed(session.shouldfail)
    if session.shouldstop:
        raise session.Interrupted(session.shouldstop)
    return True


def _publish_warnings(
    config: pytest.Config,
    result: _pytest.NativeRunResult | NativeExecutionSession,
    *,
    when: Literal["runtest", "config"] = "runtest",
) -> None:
    _publish_warning_messages(config, result.take_warning_messages(), when=when)


def _publish_warning_messages(
    config: pytest.Config,
    messages: list[tuple[str, WarningMessage]],
    *,
    when: Literal["runtest", "config"] = "runtest",
) -> None:
    for nodeid, message in messages:
        config.hook.pytest_warning_recorded.call_historic(
            kwargs={
                "warning_message": message,
                "when": when,
                "nodeid": nodeid if when == "runtest" else "",
                "location": None,
            }
        )


def finish_session(session: pytest.Session) -> None:
    """Unwind the worker's native fixture graph before coverage stops."""
    config = session.config
    execution = config.stash.get(_execution_key, None)
    if execution is None:
        if not is_xdist_worker(config) or session.exitstatus in {
            pytest.ExitCode.INTERNAL_ERROR,
            pytest.ExitCode.USAGE_ERROR,
        }:
            return
        # Idle workers still validate their collected suite before accepting shard output.
        execution = _execution(session)
    try:
        result = execution.executor.finish()
    except BaseException:
        _publish_warnings(config, execution.executor, when="config")
        execution.publisher.item = None
        del config.stash[_execution_key]
        raise
    execution.publisher.item = None
    _publish_warnings(config, result, when="config")
    del config.stash[_execution_key]
    config.stash[native_pytest._result_key] = result
    destination = os.environ.get("PYTEST_NATIVE_RESULT")
    if destination and native_pytest._native_record_environment() is None:
        worker_input = getattr(config, "workerinput", None)
        if isinstance(worker_input, dict):
            destination = f"{destination}.{worker_input['workerid']}"
        result.write_json(destination)
    if native_pytest._vfy_bridge_plugin_enabled(config):
        native_pytest._accept_native_child_results(session)
        observer = config.pluginmanager.getplugin("_vfy_run_observer")
        if observer is not None:
            observer._native_complete(result, bool(result.collected == 0))
