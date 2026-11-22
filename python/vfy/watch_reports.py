"""Shared projection of resolved coverage report options into Rust watch staging."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .native import stage_watch_outputs

if TYPE_CHECKING:
    import pytest

    from .config_types import Config


class CoverageReportOptions(Protocol):
    cov_report: dict[str, str | None]
    cov_decision_report: str | None


def stage_external_pytest_reports(config: pytest.Config) -> None:
    """Stage external report and cache destinations before their writers run."""
    if hasattr(config, "workerinput"):
        return
    stage = os.environ.get("VFY_WATCH_STAGE")
    if stage:
        xmlpath = getattr(config.option, "xmlpath", None)
        if xmlpath:
            config.option.xmlpath = stage_watch_outputs(stage, [("junit", xmlpath)])[0][1]
        htmlpath = getattr(config.option, "htmlpath", None)
        if htmlpath:
            config.option.htmlpath = stage_watch_outputs(stage, [("pytest-html", str(htmlpath))])[
                0
            ][1]
        # Resolve custom pytest cache paths through pytest's own configuration.
        cache_path = str(Path(config.rootpath) / config.getini("cache_dir"))
        stage_watch_outputs(stage, [("cache", cache_path)])
    confirmation_stage = os.environ.get("VFY_CONFIRMATION_STAGE")
    if confirmation_stage:
        if getattr(config.option, "xmlpath", None):
            config.option.xmlpath = str(Path(confirmation_stage) / "confirmation.xml")
        if getattr(config.option, "htmlpath", None):
            config.option.htmlpath = str(Path(confirmation_stage) / "confirmation.html")


def stage_coverage_reports(options: CoverageReportOptions, coverage_config: Config) -> None:
    """Pass pytest-resolved output options to Rust before coverage writes them."""
    stage = os.environ.get("VFY_WATCH_STAGE")
    if not stage:
        return

    defaults = {
        "html": coverage_config.get("html", {}).get("directory", "htmlcov"),
        "xml": coverage_config.get("xml", {}).get("output", "coverage.xml"),
        "json": coverage_config.get("json", {}).get("output", "coverage.json"),
        "lcov": coverage_config.get("lcov", {}).get("output", "coverage.lcov"),
        "markdown": "coverage.md",
        "markdown-append": "coverage.md",
    }
    outputs = [
        (kind, destination or defaults[kind])
        for kind, destination in options.cov_report.items()
        if kind in defaults
    ]
    for kind, destination in stage_watch_outputs(stage, outputs):
        options.cov_report[kind] = destination
    if "annotate" in options.cov_report:
        destination = options.cov_report["annotate"]
        kind = "annotate" if destination else "annotate-default"
        options.cov_report["annotate"] = stage_watch_outputs(
            stage, [(kind, destination or str(Path.cwd()))]
        )[0][1]
    if options.cov_decision_report:
        kind, _, destination = options.cov_decision_report.partition(":")
        if kind == "json":
            target = stage_watch_outputs(
                stage, [("decisions", destination or "vfy-decisions.json")]
            )[0][1]
            options.cov_decision_report = f"json:{target}"
