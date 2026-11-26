"""Resolve pytest report options and write coverage reports."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from vfy.errors import NoDataError
from vfy.independent.configuration import PytestConfiguration as _PytestConfiguration
from vfy.invocation_preparation import pytest_option as _pytest_option
from vfy.native import (
    NativeTestInvocation,
    pytest_coverage_failure_message,
    pytest_coverage_threshold_failed,
)
from vfy.output import OutputPolicy
from vfy.pytest_options import coverage_config_argument, validate_report

from .engine import _pytest_configuration, _resolve_pytest_configuration

if TYPE_CHECKING:
    from vfy.config_types import Config
    from vfy.data import CoverageStore


def _controller_message(args: argparse.Namespace, text: str) -> None:
    OutputPolicy.from_args(args).notice(text)


def _replay_cache(history: str) -> Path:
    return Path(history).resolve().parent / "replay.coverage"


def _pytest_reports(pytest_args: Sequence[str]) -> dict[str, str | None]:
    values: list[str] = []
    for index, argument in enumerate(pytest_args):
        if argument.startswith("--cov-report="):
            values.append(argument.partition("=")[2])
        elif argument == "--cov-report" and index + 1 < len(pytest_args):
            values.append(pytest_args[index + 1])
    if not values:
        return {"term": None}
    reports: dict[str, str | None] = {}
    for value in values:
        report, destination = validate_report(value)
        if report in {"markdown", "markdown-append"} and destination is None:
            destination = "coverage.md"
        reports[report] = destination
    return {} if set(reports) == {""} else reports


def _pytest_report_outputs(
    pytest_args: list[str],
    config: Config,
    configuration: _PytestConfiguration | None = None,
) -> list[str]:
    outputs: list[str] = []
    if configuration is None:
        configuration = _resolve_pytest_configuration(pytest_args)
    effective_args = configuration.effective_args
    reports = _pytest_reports(effective_args)
    defaults = {
        "annotate": None,
        "html": config.get("html", {}).get("directory", "htmlcov"),
        "xml": config.get("xml", {}).get("output", "coverage.xml"),
        "json": config.get("json", {}).get("output", "coverage.json"),
        "markdown": "coverage.md",
        "markdown-append": "coverage.md",
        "lcov": config.get("lcov", {}).get("output", "coverage.lcov"),
    }
    for report, destination in reports.items():
        if report in defaults:
            output = destination or defaults[report]
            if output:
                outputs.append(str(Path(output).resolve()))
    decision_report = _pytest_option(effective_args, "--cov-decision-report")
    if decision_report and decision_report.startswith("json"):
        _report, _separator, destination = decision_report.partition(":")
        outputs.append(str(Path(destination or "vfy-decisions.json").resolve()))
    html_report = _pytest_option(effective_args, "--html")
    if html_report:
        report_path = Path(html_report).resolve()
        outputs.extend((str(report_path), str(report_path.parent / "assets")))
    return outputs


def _write_coverage_reports(
    args: argparse.Namespace,
    config: Config,
    pytest_args: list[str],
    source_roots: list[str],
    store: CoverageStore,
) -> bool:
    from vfy.api import Coverage  # noqa: PLC0415 - reporting owns the compatibility adapter
    from vfy.pytest_reports import (  # noqa: PLC0415 - load report rendering only when requested
        write_pytest_reports,
    )

    reports = _pytest_reports(pytest_args)
    threshold_value = _pytest_option(pytest_args, "--cov-fail-under")
    threshold = (
        float(threshold_value)
        if threshold_value is not None
        else config.get("report", {}).get("fail_under")
    )
    decisions = "--cov-decisions" in pytest_args or bool(
        config.get("vfy", {}).get("run", {}).get("decisions", False)
    )
    if not (reports or threshold or decisions):
        return True
    cov_config = _pytest_option(pytest_args, "--cov-config")
    coverage = Coverage(
        data_file=args.data_file,
        source=NativeTestInvocation.coverage_report_sources(pytest_args, source_roots),
        decisions=decisions,
        config_file=(
            coverage_config_argument(cov_config)
            if cov_config
            else args.rcfile or (not getattr(args, "isolated", False))
        ),
    )
    coverage.data._store = store
    precision_value = _pytest_option(pytest_args, "--cov-precision")
    precision = (
        int(precision_value)
        if precision_value is not None
        else int(config.get("report", {}).get("precision", 0))
    )
    try:
        with coverage._report_batch():
            total, semantic = write_pytest_reports(
                coverage,
                reports,
                config,
                sys.stdout,
                precision=precision,
                decisions=decisions,
                decision_report=_pytest_option(pytest_args, "--cov-decision-report"),
                source=coverage.source,
                history_path=str(Path(args.history).resolve()),
            )
    except NoDataError as error:
        print(f"\nWARNING: Failed to generate report: {error}\n")
        total, semantic = 0.0, None
    if os.environ.get("VFY_MUTATION_BASELINE_COVERAGE") == "implicit":
        return True
    threshold_failed = threshold is not None and pytest_coverage_threshold_failed(
        total, float(threshold), precision
    )
    if threshold_failed:
        print(f"\nERROR: {pytest_coverage_failure_message(total, float(threshold), precision)}")
    if threshold is not None and float(threshold) > 0:
        # pytest-cov labels the line with the unrounded total; the exit status
        # uses the rounded comparison.
        missed = total < float(threshold)
        print(
            f"{'FAIL ' if missed else ''}Required test coverage of {float(threshold):g}% "
            f"{'not reached' if missed else 'reached'}. Total coverage: {total:.2f}%"
        )
    if threshold_failed:
        return False
    if semantic is None:
        return True
    for name, actual in (
        ("decisions", semantic.decision_percent),
        ("conditions", semantic.condition_percent),
        ("mcdc", semantic.mcdc_percent),
    ):
        value = _pytest_option(pytest_args, f"--cov-fail-under-{name}")
        if value is None:
            value = config.get("vfy", {}).get("check", {}).get(f"fail_under_{name}")
        if value is not None and actual < float(value):
            print(
                f"FAIL Required semantic {name} coverage of {float(value):g}% not reached; "
                f"actual {actual:.2f}%",
                file=sys.stderr,
            )
            return False
    return True


def _pytest_generated_directories(
    pytest_args: list[str], root: Path, configuration: _PytestConfiguration | None = None
) -> set[Path]:
    """Find pytest cache and temporary roots that are outside the defaults."""
    projection = (
        configuration.projection
        if configuration is not None
        else _pytest_configuration(pytest_args, invocation_dir=root)
    )
    values = {Path(projection["cache_dir"])}
    if projection["basetemp"] is not None:
        values.add(Path(projection["basetemp"]))
    resolved_root = root.resolve()
    return {path.resolve() for path in values if path.resolve() != resolved_root}
