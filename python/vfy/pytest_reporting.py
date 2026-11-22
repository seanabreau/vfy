"""Optional built-in HTML and metadata reporting for vfy."""

from __future__ import annotations

import json
import os
import platform
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from .native import PytestHtmlReport
from .pytest_html import extras as extras
from .pytest_metadata import metadata_key
from .watch_reports import stage_external_pytest_reports

if TYPE_CHECKING:
    from pluggy import Result

    from ._pytest_native import NativeRunResult


class _VfyHtmlHooks:
    @pytest.hookspec
    def pytest_html_report_title(self, report: object) -> None:
        """Change ``report.title`` before vfy renders the document."""

    @pytest.hookspec
    def pytest_html_results_summary(
        self, prefix: list[str], summary: list[str], postfix: list[str]
    ) -> None:
        """Add trusted HTML fragments around the result table."""

    @pytest.hookspec
    def pytest_html_results_table_header(self, cells: list[str]) -> None:
        """Append trusted HTML headers to the result table."""

    @pytest.hookspec
    def pytest_html_results_table_row(self, report: pytest.TestReport, cells: list[str]) -> None:
        """Append trusted HTML cells to a result row."""


class _HtmlDocument:
    def __init__(self) -> None:
        self.title = "vfy test results"


def _independent_runner() -> bool:
    protocol = sys.modules.get("vfy.independent.protocol")
    return protocol is not None and pytest.Config is protocol.Config


class _VfyHtmlReporter:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.reports: dict[
            tuple[str, str], dict[str, pytest.TestReport | pytest.CollectReport]
        ] = {}
        self._native_results: NativeRunResult | None = None
        self._awaiting_independent_result = _independent_runner()
        self.writer = PytestHtmlReport(str(config.option.htmlpath))

    def _native_complete(self, result: NativeRunResult) -> None:
        """Retain Rust execution results until the shared renderer consumes them."""
        # Coverage is loaded independently by pytest before these lifecycle hooks run.
        from .pytest_plugin import _native_coverage_bridge_enabled  # noqa: PLC0415

        if not _native_coverage_bridge_enabled(self.config):
            raise RuntimeError("native pytest HTML bridge is not active")
        self._native_results = result

    def _independent_complete(self, result: NativeRunResult) -> None:
        """Render the completed Rust result and merged worker metadata."""
        self._native_results = result
        self.config.stash[metadata_key].update(result.worker_metadata_records())
        self.reports.clear()
        self._awaiting_independent_result = False
        self._write()

    def _native_progress(self, result: NativeRunResult) -> None:
        self._native_complete(result)
        try:
            self._write()
        finally:
            self._native_results = None

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when in {"setup", "call", "teardown"}:
            # Each-mode xdist executes the same node ID once in every worker.
            key = (str(report.nodeid), str(getattr(report, "worker_id", "")))
            self.reports.setdefault(key, {})[str(report.when)] = report
            if report.when == "teardown" and self.config.getini("generate_report_on_test"):
                self._write()

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed or report.skipped:
            self.reports.setdefault((report.nodeid, ""), {})["collect"] = report

    def _result(
        self, nodeid: str, phases: dict[str, pytest.TestReport | pytest.CollectReport]
    ) -> dict[str, object]:
        report = next(
            (item for item in phases.values() if item.failed or item.skipped),
            phases.get("call") or next(iter(phases.values())),
        )
        cells: list[str] = []
        self.config.hook.pytest_html_results_table_row(report=report, cells=cells)
        return {
            "nodeid": nodeid,
            "worker_id": str(getattr(report, "worker_id", "")),
            "phases": [
                {
                    "when": when,
                    "outcome": item.outcome,
                    "duration": getattr(item, "duration", 0.0),
                    "wasxfail": hasattr(item, "wasxfail"),
                    "details": [
                        getattr(item, "longreprtext", "") if not item.passed else "",
                        getattr(item, "capstdout", ""),
                        getattr(item, "capstderr", ""),
                        getattr(item, "caplog", ""),
                    ],
                }
                for when, item in phases.items()
            ],
            "extras": [
                extra for item in phases.values() for extra in getattr(item, "vfy_html_extras", [])
            ],
            "cells": cells,
        }

    def _write_report(self) -> None:
        document = _HtmlDocument()
        self.config.hook.pytest_html_report_title(report=document)
        prefix: list[str] = []
        summary: list[str] = []
        postfix: list[str] = []
        headers: list[str] = []
        self.config.hook.pytest_html_results_table_header(cells=headers)
        self.config.hook.pytest_html_results_summary(
            prefix=prefix, summary=summary, postfix=postfix
        )
        metadata = self.config.stash[metadata_key]
        results = [
            self._result(nodeid, phases)
            for (nodeid, _worker_id), phases in self.reports.items()
            if self._native_results is None or "collect" in phases
        ]
        payload = {
            "title": document.title,
            "metadata": sorted((str(key), str(value)) for key, value in metadata.items()),
            "redact": list(self.config.getini("environment_table_redact_list")),
            "results": results,
            "css": list(self.config.option.css),
            "self_contained": bool(self.config.option.self_contained_html),
            "prefix": prefix,
            "summary": summary,
            "postfix": postfix,
            "headers": headers,
        }
        encoded = json.dumps(payload, separators=(",", ":"))
        if self._native_results is None:
            self.writer.write(encoded)
        elif sys.platform == "win32":
            raise RuntimeError("HTML rendering requires Linux or macOS")
        else:
            self.writer.write_native(encoded, self._native_results)
        self.reports = {
            nodeid: phases
            for nodeid, phases in self.reports.items()
            if "teardown" not in phases and "collect" not in phases
        }

    def _write(self) -> None:
        try:
            self._write_report()
        except (OSError, RuntimeError, ValueError) as exc:
            terminal = self.config.pluginmanager.getplugin("terminalreporter")
            if terminal is not None:
                terminal.write_line(f"vfy failed to write HTML report: {exc}", red=True)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self._awaiting_independent_result:
            return
        # Delay peer plug-in imports until pytest has registered explicit loads.
        from .pytest_plugin import _native_coverage_bridge_enabled  # noqa: PLC0415

        if (
            _native_coverage_bridge_enabled(self.config)
            and self._native_results is None
            and not self.reports
        ):
            return
        self._write()


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    replacements = (
        "html",
        "html_fixtures",
        "pytest_html.plugin",
        "pytest_html.fixtures",
        "metadata",
        "pytest_metadata.plugin",
    )
    for name, plugin in pluginmanager.list_name_plugin():
        if (
            plugin is not None
            and plugin is not sys.modules[__name__]
            and (
                name in replacements
                or (isinstance(plugin, ModuleType) and plugin.__name__ in replacements)
            )
        ):
            raise pytest.UsageError(
                f"vfy replaces the {name!r} plug-in; remove its explicit load "
                "and load -p vfy.pytest_reporting before pytest collects conftests"
            )
    for name in replacements:
        if name != "html" or not _independent_runner():
            pluginmanager.set_blocked(name)
    pluginmanager.add_hookspecs(_VfyHtmlHooks)


def pytest_addoption(parser: pytest.Parser) -> None:
    html = parser.getgroup("vfy-html", "vfy HTML result reporting")
    html.addoption("--html", action="store", dest="htmlpath", metavar="PATH")
    html.addoption(
        "--self-contained-html",
        action="store_true",
        dest="self_contained_html",
        help="Embed vfy's stylesheet in the HTML report.",
    )
    html.addoption(
        "--css",
        action="append",
        default=[],
        metavar="PATH",
        help="Append a stylesheet to the HTML report (multi-allowed).",
    )
    html.addoption(
        "--metadata",
        action="append",
        default=[],
        nargs=2,
        metavar=("KEY", "VALUE"),
        help="Add an environment metadata entry (multi-allowed).",
    )
    html.addoption("--metadata-from-json", action="append", default=[], metavar="JSON")
    html.addoption("--metadata-from-json-file", action="append", default=[], metavar="PATH")
    parser.addini(
        "environment_table_redact_list",
        "regular expressions matching metadata keys whose values are hidden",
        type="linelist",
    )
    parser.addini(
        "generate_report_on_test",
        "update the HTML report after each completed test",
        type="bool",
        default=False,
    )


@pytest.fixture(scope="session")
def metadata(pytestconfig: pytest.Config) -> dict[str, object]:
    """Return the mutable metadata displayed in vfy's HTML report."""
    return pytestconfig.stash[metadata_key]


@pytest.fixture(name="extras")
def html_extras(request: pytest.FixtureRequest) -> list[dict[str, str]]:
    """Collect JSON-safe HTML report attachments for one test."""
    values: list[dict[str, str]] = []
    request.node.__dict__["_vfy_html_extras"] = values
    return values


class _VfyHtmlExtras:
    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_makereport(
        self, item: pytest.Item, call: pytest.CallInfo[None]
    ) -> Generator[None, Result[pytest.TestReport], None]:
        outcome = yield
        report = outcome.get_result()
        report.__dict__["vfy_html_extras"] = [
            *getattr(report, "extras", getattr(report, "extra", [])),
            *(getattr(item, "_vfy_html_extras", []) if report.when == "teardown" else []),
        ]


def _configured_metadata(config: pytest.Config) -> dict[str, object]:
    values: dict[str, object] = {
        "Python": sys.version.split()[0],
        "Platform": platform.platform(),
    }
    for key, value in config.option.metadata:
        values[str(key)] = str(value)
    documents: list[object] = []
    for encoded in config.option.metadata_from_json:
        try:
            documents.append(json.loads(encoded))
        except ValueError as exc:
            raise pytest.UsageError(f"Invalid --metadata-from-json value: {exc}") from exc
    for filename in config.option.metadata_from_json_file:
        try:
            documents.append(json.loads(Path(filename).read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise pytest.UsageError(
                f"Failed to load metadata JSON file {filename!r}: {exc}"
            ) from exc
    for document in documents:
        if not isinstance(document, dict):
            raise pytest.UsageError("pytest metadata JSON must contain an object")
        values.update({str(key): value for key, value in document.items()})
    return values


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    if "VFY_NATIVE_PARALLEL_DIR" not in os.environ:
        stage_external_pytest_reports(config)
    config.stash[metadata_key] = _configured_metadata(config)
    html_enabled = bool(config.option.htmlpath)
    if not html_enabled and "VFY_NATIVE_PARALLEL_DIR" in os.environ and not _independent_runner():
        from ._pytest.parallel import worker_html_enabled  # noqa: PLC0415 - parallel worker setup

        # Native pytest workers clear htmlpath while retaining attachment transport.
        html_enabled = worker_html_enabled(config)
    if html_enabled:
        config.pluginmanager.register(_VfyHtmlExtras(), "_vfy_html_extras")
    if (
        config.option.htmlpath
        and not hasattr(config, "workerinput")
        and "VFY_NATIVE_PARALLEL_DIR" not in os.environ
    ):
        if (
            _independent_runner()
            and config.getini("generate_report_on_test")
            and int(config.getoption("workers", 1)) > 1
        ):
            raise pytest.UsageError("vfy HTML generate_report_on_test requires --workers=1")
        config.pluginmanager.register(_VfyHtmlReporter(config), "_vfy_html_reporter")
