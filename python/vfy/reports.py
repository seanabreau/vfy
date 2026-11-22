"""Filesystem adapters for the Rust report model and renderers."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from .analysis import DEFAULT_EXCLUDES, DEFAULT_PARTIALS
from .data import CoverageStore, FileCoverage
from .errors import NoDataError, _source_failure
from .native import (
    ReportCounts,
    ReportRow,
    build_plugin_report_row,
    build_report_rows,
    build_report_rows_from_data,
    render_annotation,
    render_json,
    render_lcov,
    render_markdown,
    render_terminal,
    render_total,
    render_xml,
    report_totals,
    select_plugin_report_paths,
    select_report_rows,
    write_annotation_report,
    write_html_report as rust_write_html_report,
    write_report_output,
)
from .plugin import FileReporter
from .plugin_support import Plugins

__all__ = [
    "ReportRow",
    "build_rows",
    "render_annotation",
    "render_json",
    "render_lcov",
    "render_markdown",
    "render_terminal",
    "render_total",
    "render_xml",
    "select_rows",
    "totals",
    "write_annotation_report",
    "write_html_report",
    "write_output",
]


def build_rows(
    store: CoverageStore,
    *,
    include: Sequence[str] | None = None,
    omit: Sequence[str] | None = None,
    excludes: Sequence[str] | None = None,
    partials: Sequence[str] | None = None,
    contexts: Sequence[str] | None = None,
    plugins: Plugins | None = None,
    source_roots: Sequence[str] | None = None,
    relative_files: bool | None = None,
    requested_paths: Sequence[str] | None = None,
    ignore_errors: bool = False,
    line_contexts: bool = True,
) -> list[ReportRow]:
    if excludes is None:
        excludes = DEFAULT_EXCLUDES
    if partials is None:
        partials = DEFAULT_PARTIALS
    rust_data = store.rust_data
    if rust_data is not None and not rust_data.has_tracers():
        rows = build_report_rows_from_data(
            rust_data,
            include or [],
            omit or [],
            excludes or [],
            contexts or [],
            partials or [],
            source_roots or [],
            relative_files,
            requested_paths,
            line_contexts=line_contexts,
        )
        return _validate_source_rows(rows, ignore_errors)
    ordinary = [
        (
            path,
            sorted(file.lines),
            sorted(file.arcs),
            [(context, sorted(lines)) for context, lines in sorted(file.contexts.items())],
            [(context, sorted(arcs)) for context, arcs in sorted(file.context_arcs.items())],
        )
        for path, file in sorted(store.files.items())
        if not file.tracer
    ]
    tracer_paths = [path for path, file in store.files.items() if file.tracer]
    rows = build_report_rows(
        ordinary,
        store.has_arcs,
        include or [],
        [*(omit or []), *tracer_paths],
        excludes or [],
        contexts or [],
        partials or [],
        source_roots or [],
        relative_files,
        requested_paths,
        line_contexts=line_contexts,
    )
    custom_reporters: dict[str, list[FileReporter]] = {}
    python_reporter_paths: list[str] = []
    for path in sorted(tracer_paths):
        file = store.files[path]
        if plugins is None:
            raise ValueError(f"file tracer {file.tracer!r} requires its registered plug-in")
        plugin = plugins.get(file.tracer)
        reporter = plugin.file_reporter(path)
        if reporter == "python":
            python_reporter_paths.append(path)
            continue
        custom_reporters.setdefault(reporter.filename, []).append(reporter)
    if python_reporter_paths:
        python_files = [store.files[path] for path in python_reporter_paths]
        rows.extend(
            build_report_rows(
                [
                    (
                        file.path,
                        sorted(file.lines),
                        sorted(file.arcs),
                        [
                            (context, sorted(lines))
                            for context, lines in sorted(file.contexts.items())
                        ],
                        [
                            (context, sorted(arcs))
                            for context, arcs in sorted(file.context_arcs.items())
                        ],
                    )
                    for file in python_files
                ],
                store.has_arcs,
                include or [],
                omit or [],
                excludes or [],
                contexts or [],
                partials or [],
                relative_files=relative_files,
                requested_paths=python_reporter_paths,
                line_contexts=line_contexts,
            )
        )
    selected_reporter_paths = select_plugin_report_paths(
        list(custom_reporters), include or [], omit or [], relative_files
    )
    for reporter_path, data_path in selected_reporter_paths:
        file = store.files.get(data_path, FileCoverage(data_path))
        for reporter in custom_reporters[reporter_path]:
            rows.append(_build_custom_row(reporter, file, store.has_arcs, contexts or []))
    rows.sort(key=lambda row: row.path)
    return _validate_source_rows(rows, ignore_errors)


def _build_custom_row(
    reporter: FileReporter, file: FileCoverage, has_arcs: bool, contexts: Sequence[str]
) -> ReportRow:
    lines = set(reporter.translate_lines(file.lines))
    arcs = set(reporter.translate_arcs(file.arcs))
    return build_plugin_report_row(
        (
            reporter.filename,
            sorted(lines),
            sorted(arcs),
            [
                (context, sorted(reporter.translate_lines(values)))
                for context, values in sorted(file.contexts.items())
            ],
            [
                (context, sorted(reporter.translate_arcs(values)))
                for context, values in sorted(file.context_arcs.items())
            ],
        ),
        has_arcs,
        sorted(reporter.lines()),
        sorted(reporter.excluded_lines()),
        sorted(reporter.arcs()),
        sorted(reporter.no_branch_lines()),
        contexts,
        reporter.source(),
        file.path,
        reporter.relative_filename(),
        reporter.exit_counts() if has_arcs else {},
    )


def _validate_source_rows(rows: list[ReportRow], ignore_errors: bool) -> list[ReportRow]:
    valid: list[ReportRow] = []
    for row in rows:
        if not row.source_missing:
            valid.append(row)
            continue
        if ignore_errors:
            continue
        raise _source_failure(row.path, row.source_error_kind, row.source_error_detail)
    if ignore_errors and rows and not valid:
        raise NoDataError("No data to report.")
    return valid


def totals(rows: list[ReportRow]) -> ReportCounts:
    return report_totals(rows)


def select_rows(
    rows: list[ReportRow],
    *,
    skip_covered: bool = False,
    skip_empty: bool = False,
    sort: str | None = None,
) -> list[ReportRow]:
    return select_report_rows(rows, skip_covered, skip_empty, sort)


def write_html_report(
    rows: list[ReportRow],
    directory: str,
    *,
    title: str = "Coverage report",
    precision: int = 1,
    show_contexts: bool = False,
    extra_css: str | None = None,
    skip_covered: bool = False,
    skip_empty: bool = False,
) -> None:
    rust_write_html_report(
        rows, directory, title, precision, show_contexts, extra_css, skip_covered, skip_empty
    )


def write_output(content: str, destination: str | None) -> None:
    if not destination or destination == "-":
        sys.stdout.write(content)
        return
    write_report_output(content, destination)
