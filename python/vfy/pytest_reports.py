"""Shared pytest-cov report orchestration for full and replayed runs."""

from __future__ import annotations

import json
import os
import shutil
import sys
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from .errors import NoDataError
from .native import (
    CoverageData as ExtensionCoverageData,
    append_report_output,
    pytest_coverage_heading,
    read_ci_plan,
    write_report_output,
)
from .pytest_options import TERM_REPORTS

if TYPE_CHECKING:
    from .api import Coverage
    from .config_types import Config
    from .semantic_reports import DecisionReport


class NullWriter:
    def write(self, text: str) -> int:
        return len(text)


def coverage_heading() -> str:
    """Return the platform heading pytest-cov writes before its reports."""
    width, _height = shutil.get_terminal_size(fallback=(80, 24))
    version = "{}.{}.{}-{}-{}".format(*sys.version_info[:5])
    title = f"coverage: platform {sys.platform}, python {version}"
    return pytest_coverage_heading(title, width) + "\n\n"


def reconstruct_selected_coverage(coverage: Coverage) -> None:
    """Replace a selected run's data with the complete store rebuilt from its baseline.

    The controller stages the baseline and the destination in the file named by
    ``VFY_REPLAY_MERGE``. An independent CI run binds the plan after collection;
    every other route stages it with the baseline.
    """
    specification_path = os.environ.get("VFY_REPLAY_MERGE")
    if not specification_path:
        return
    specification = json.loads(Path(specification_path).read_text(encoding="utf-8"))
    plan = specification.get("plan") or read_ci_plan(os.environ["VFY_CI_FINAL"])
    if plan["fallback_full"]:
        return
    from .data import CoverageStore  # noqa: PLC0415 - only selected runs rebuild coverage

    selected_data = coverage.data
    complete = CoverageStore(
        rust_data=ExtensionCoverageData.replay_baseline(
            specification["baseline"],
            selected_data._store.to_rust(),
            list(plan["selected"]),
            list(plan["affected_files"]),
            {
                path: [tuple(shift) for shift in shifts]
                for path, shifts in plan["scoped_files"].items()
            },
        )
    )
    complete.write(specification["complete"])
    selected_data._store = complete


def write_pytest_reports(
    coverage: Coverage,
    reports: dict[str, str | None],
    config: Config,
    stream: TextIO,
    *,
    precision: int,
    decisions: bool,
    decision_report: str | None,
    source: list[str],
    history_path: str | None = None,
    ignore_errors: bool | None = None,
) -> tuple[float, DecisionReport | None]:
    """Render pytest reports from one already-complete coverage dataset.

    Raise `NoDataError` when nothing was measured; pytest-cov reports that as a
    failed report with a total of zero.
    """
    if not coverage.get_data().measured_files():
        raise NoDataError("No data to report.")

    total: float | None = None
    skip_covered = "skip-covered" in reports.values()
    if TERM_REPORTS & reports.keys():
        total = coverage.report(
            file=stream,
            show_missing="term-missing" in reports,
            skip_covered=skip_covered,
            precision=precision,
            ignore_errors=ignore_errors,
        )
    if "annotate" in reports:
        destination = reports["annotate"]
        coverage.annotate(directory=destination, ignore_errors=ignore_errors)
        message = (
            f"Coverage annotated source written to dir {destination}"
            if destination
            else "Coverage annotated source written next to source"
        )
        stream.write(f"{message}\n")
    if "html" in reports:
        settings = config.get("html", {})
        destination = reports["html"] or settings.get("directory", "htmlcov")
        total = coverage.html_report(
            directory=destination,
            title=settings.get("title", "Coverage report"),
            show_contexts=settings.get("show_contexts", False),
            skip_covered=settings.get("skip_covered"),
            skip_empty=settings.get("skip_empty"),
            ignore_errors=ignore_errors,
        )
        stream.write(f"Coverage HTML written to dir {destination}\n")
    if "xml" in reports:
        destination = reports["xml"] or config.get("xml", {}).get("output", "coverage.xml")
        total = coverage.xml_report(outfile=destination, ignore_errors=ignore_errors)
        stream.write(f"Coverage XML written to file {destination}\n")
    if "json" in reports:
        settings = config.get("json", {})
        destination = reports["json"] or settings.get("output", "coverage.json")
        total = coverage.json_report(
            outfile=destination,
            pretty_print=settings.get("pretty_print", False),
            show_contexts=settings.get("show_contexts", False),
            ignore_errors=ignore_errors,
        )
        stream.write(f"Coverage JSON written to file {destination}\n")
    for report_type, mode in (("markdown", "w"), ("markdown-append", "a")):
        if report_type not in reports:
            continue
        destination = Path(reports[report_type] or "coverage.md")
        report_stream = StringIO()
        coverage.report(
            file=report_stream,
            output_format="markdown",
            precision=precision,
            ignore_errors=ignore_errors,
        )
        commit = append_report_output if mode == "a" else write_report_output
        commit(report_stream.getvalue(), str(destination))
        verb = "appended to" if mode == "a" else "written to"
        stream.write(f"Coverage Markdown information {verb} file {destination}\n")
    if "lcov" in reports:
        destination = reports["lcov"] or config.get("lcov", {}).get("output", "coverage.lcov")
        total = coverage.lcov_report(outfile=destination, ignore_errors=ignore_errors)
        stream.write(f"Coverage LCOV written to file {destination}\n")
    if total is None:
        total = coverage.report(
            file=NullWriter(),
            output_format="total",
            precision=precision,
            ignore_errors=ignore_errors,
        )

    semantic: DecisionReport | None = None
    if decisions:
        from .semantic_reports import (  # noqa: PLC0415 - ordinary reports do not use semantic analysis
            build_semantic_report,
            render_semantic_text,
            semantic_json,
        )

        semantic = build_semantic_report(coverage._report_store(), source_roots=source)
        report_type, _separator, destination = (decision_report or "term").partition(":")
        if report_type == "json":
            destination = destination or "vfy-decisions.json"
            write_report_output(semantic_json(semantic, pretty=True), destination)
            stream.write(f"Semantic decision report written to {destination}\n")
        elif report_type == "term":
            stream.write(render_semantic_text(semantic, precision, history_path=history_path))
        else:
            raise ValueError(f"invalid semantic report type: {report_type!r}")
    return total, semantic
