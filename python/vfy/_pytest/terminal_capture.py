"""Terminal capture reports and the final summary rendered from the Rust result."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Literal, override

import pytest
from _pytest.config import Config
from _pytest.reports import CollectReport, TestReport
from _pytest.terminal import TerminalReporter, running_on_ci

from . import parallel
from .native_junit import _junit_retains_passing_output
from .native_state import NativeRunResult, _result_key


def _terminal_reports_success_capture(config: Config) -> bool:
    terminal = config.pluginmanager.getplugin("terminalreporter")
    return bool(
        terminal is not None
        and (terminal.hasopt("P") or terminal.hasopt("X"))
        and config.option.tbstyle != "no"
        and not config.option.no_summary
    )


def _publish_terminal_capture_reports(
    config: Config,
    result: NativeRunResult,
    terminal: TerminalReporter,
) -> None:
    """Feed Rust-projected sections and reports to pytest's capture renderer."""
    if config.option.tbstyle == "no" or config.option.no_summary:
        return
    capture_reports, subtest_sections = result.terminal_capture_reports(
        terminal.reportchars if _terminal_reports_success_capture(config) else "",
        subtest_verbosity=config.get_verbosity("subtests"),
    )
    passed_reports = []
    xpassed_reports = []
    teardown = _hydrate_serial_subtest_captures(terminal, subtest_sections)
    forwarded_subtests: dict[str, list[TestReport]] = {}
    for report in terminal.stats.get("xpassed", ()):
        if isinstance(report, TestReport) and hasattr(report, "context"):
            forwarded_subtests.setdefault(report.nodeid, []).append(report)
    forwarded = {nodeid: iter(reports) for nodeid, reports in forwarded_subtests.items()}
    for (
        nodeid,
        location,
        wasxfail,
        context,
        call_sections,
        teardown_sections,
    ) in capture_reports:
        existing = (
            next(forwarded[nodeid], None) if context is not None and nodeid in forwarded else None
        )
        if existing is not None:
            existing.sections = call_sections
            vars(existing)["_vfy_native_capture"] = True
        else:
            report_class = TestReport if context is None else _NativeSubtestCaptureReport
            report = report_class(
                nodeid=nodeid,
                location=location,
                keywords={},
                outcome="passed",
                longrepr=None,
                when="call",
                sections=call_sections,
            )
            if isinstance(report, _NativeSubtestCaptureReport):
                report.context = context or ""
            if wasxfail is None:
                passed_reports.append(report)
            else:
                report.wasxfail = wasxfail
                vars(report)["_vfy_native_capture"] = True
                xpassed_reports.append(report)
        if teardown_sections:
            teardown[nodeid] = location, teardown_sections
    if passed_reports:
        terminal._add_stats("passed", passed_reports)
    if xpassed_reports:
        terminal._add_stats("xpassed", xpassed_reports)
    if teardown:
        terminal._add_stats(
            "",
            [
                TestReport(
                    nodeid=nodeid,
                    location=location,
                    keywords={},
                    outcome="passed",
                    longrepr=None,
                    when="teardown",
                    sections=sections,
                )
                for nodeid, (location, sections) in teardown.items()
            ],
        )


class _NativeSubtestCaptureReport(TestReport):
    """Adapt a Rust-formatted subtest context to pytest's report headline."""

    context: str

    @property
    @override
    def head_line(self) -> str | None:
        return f"{self.location[2]} {self.context}" if self.location is not None else None


def _hydrate_serial_subtest_captures(
    terminal: TerminalReporter,
    sections: Sequence[tuple[str, list[tuple[str, str]], list[tuple[str, str]]]],
) -> dict[str, tuple[tuple[str, int | None, str], list[tuple[str, str]]]]:
    """Attach Rust-projected capture to already-forwarded unittest reports."""
    teardown = {}
    serial_subtests: dict[str, list[TestReport]] = {}
    for reports in terminal.stats.values():
        for report in reports:
            if isinstance(report, TestReport) and hasattr(report, "context"):
                serial_subtests.setdefault(report.nodeid, []).append(report)
    for nodeid, setup_sections, teardown_sections in sections:
        reports = serial_subtests.get(nodeid, ())
        for report in reports:
            report.sections = setup_sections.copy()
        if reports and teardown_sections and (location := reports[0].location) is not None:
            teardown[nodeid] = location, teardown_sections
    return teardown


def _capture_summary_chars(config: Config) -> str:
    """Return the report characters pytest's passing-capture summary renders."""
    if not _terminal_reports_success_capture(config):
        return ""
    terminal = config.pluginmanager.getplugin("terminalreporter")
    return "".join(char for char in "PX" if terminal is not None and terminal.hasopt(char))


def _retains_native_capture_locations(config: Config) -> bool:
    # JUnit passing captures do not consume locations, but terminal/native APIs do.
    return (
        _terminal_reports_success_capture(config)
        or bool(getattr(config.option, "htmlpath", None))
        or parallel.worker_html_enabled(config)
        or bool(os.environ.get("PYTEST_NATIVE_RESULT"))
        or os.environ.get("VFY_NATIVE_PARALLEL_RETAIN_CAPTURE_LOCATIONS") == "1"
    )


def _retains_native_success_output(config: Config) -> bool:
    return _junit_retains_passing_output(config) or _terminal_reports_success_capture(config)


def _render_native_terminal_summary(
    terminalreporter: TerminalReporter, config: Config, result: NativeRunResult
) -> None:
    """Supply pytest's final summary from the Rust-owned aggregate."""
    if config.option.no_summary and tuple(map(int, pytest.version_tuple[:2])) >= (9, 2):
        return
    stats = terminalreporter.stats
    # Serial unittest callbacks already give pytest its subtest reports. Worker
    # reports stay private, so the merged Rust result owns their rendering.
    subtests_forwarded = any(
        isinstance(report, TestReport)
        and hasattr(report, "context")
        and not isinstance(report, _NativeSubtestCaptureReport)
        for reports in stats.values()
        for report in reports
    )
    subtest_verbosity = config.get_verbosity("subtests")
    if config.option.tbstyle != "no":
        outcomes: tuple[tuple[Literal["failed", "xfailed"], bool], ...] = (
            ("failed", bool(result.failed or result.errors)),
            ("xfailed", bool(getattr(config.option, "xfail_tb", False) and result.xfailed)),
        )
        for outcome, enabled in outcomes:
            if not enabled:
                continue
            for heading, entries in result.failure_sections(
                config.option.showcapture,
                include_subtests=not subtests_forwarded,
                outcome=outcome,
                subtest_verbosity=subtest_verbosity,
            ):
                terminalreporter.write_sep("=", heading)
                for headline, detail, sections in entries:
                    terminalreporter.write_sep("_", headline, red=True, bold=True)
                    terminalreporter.write(detail)
                    for name, content in sections:
                        terminalreporter.write_sep("-", name)
                        terminalreporter.write_line(content.removesuffix("\n"))
    summary = result.terminal_summary(
        {
            "durations": config.option.durations,
            "durations_min": config.option.durations_min,
            "verbosity": config.get_verbosity(),
            "fullwidth": terminalreporter._tw.fullwidth,
            "long_summary": (running_on_ci() or config.option.verbose >= 2)
            and not getattr(config.option, "force_short_summary", False),
            "subtests_forwarded": subtests_forwarded,
            "xpasses_forwarded": any(
                getattr(report, "_vfy_native_capture", False) for report in stats.get("xpassed", ())
            ),
            "subtest_verbosity": subtest_verbosity,
            "reportchars": terminalreporter.reportchars.replace("p", "")
            if _terminal_reports_success_capture(config) and terminalreporter.hasopt("P")
            else terminalreporter.reportchars,
            "collection_skipped": sum(
                isinstance(report, CollectReport) for report in stats.get("skipped", ())
            ),
            "collection_errors": sum(
                isinstance(report, CollectReport) for report in stats.get("error", ())
            ),
            "warning_count": len(stats.get("warnings", ())),
            "deselected_count": len(stats.get("deselected", ())),
        }
    )
    duration_heading = summary["duration_heading"]
    duration_lines = summary["duration_lines"]
    summary_parts = summary["summary_parts"]
    main_color = summary["main_color"]
    if summary["short_lines"]:
        terminalreporter.write_sep("=", "short test summary info")
        for line in summary["short_lines"]:
            terminalreporter.write_line(line)
    if duration_heading is not None:
        terminalreporter.write_sep("=", duration_heading)
        for line in duration_lines:
            terminalreporter.write_line(line)
    if (
        config.option.xmlpath
        and config.get_verbosity() >= 0
        and config.stash.get(_result_key, None) is result
    ):
        xmlpath = os.path.abspath(os.path.expandvars(os.path.expanduser(config.option.xmlpath)))
        terminalreporter.write_sep("-", f"generated xml file: {xmlpath}")

    def build_summary_stats_line() -> tuple[list[tuple[str, dict[str, bool]]], str]:
        parts = [(text, {color: True, "bold": bold}) for text, color, bold in summary_parts]
        return parts, main_color

    vars(terminalreporter)["build_summary_stats_line"] = build_summary_stats_line
