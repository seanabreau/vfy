"""Render documentation charts from parity-gated benchmark summaries."""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import statistics
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class ProjectRow:
    project: str
    scope: str
    reference_median_ms: float
    candidate_median_ms: float
    reference_samples: int
    candidate_samples: int
    warmups: int
    status: str
    source: str
    baseline_median_ms: float | None = None
    baseline_samples: int | None = None
    display_scope: str | None = None
    filegroups_median_ms: float | None = None
    filegroups_samples: int | None = None
    full_cases: int | None = None
    reference_cases: int | None = None
    candidate_cases: int | None = None
    context_status: str | None = None


@dataclass(frozen=True)
class ProjectChart:
    kind: str
    title: str
    route: str
    context: str
    reference: str
    candidate: str
    rows: tuple[ProjectRow, ...]


@dataclass(frozen=True)
class StockOnlyRow:
    """A four-worker stock route whose native routes fail a verified support gate."""

    project: str
    scope: str
    reference_median_ms: float
    reference_samples_ms: tuple[float, ...]
    native_status: str
    reason: str
    wheel_sha256: str
    controller_revision: str
    upstream_revision: str


@dataclass(frozen=True)
class MemoryValues:
    samples_bytes: tuple[int, ...]
    median_bytes: float


@dataclass(frozen=True)
class MemoryRow:
    project: str
    scope: str
    plain: MemoryValues
    vfy: MemoryValues
    pytest_cov: MemoryValues
    wheel_sha256: str
    controller_revision: str
    upstream_revision: str


README_RESOURCE_PROJECTS = ("Packaging", "Rich", "attrs")
README_RESOURCE_ROUTES = (
    "plain pytest",
    "vfy",
    "pytest-cov",
    "stock-xdist-4",
    "pytest-native-j4",
)


@dataclass(frozen=True)
class ResourceValues:
    time_samples_ms: tuple[float, float]
    time_median_ms: float
    pss_samples_bytes: tuple[int, int]
    pss_median_bytes: float


@dataclass(frozen=True)
class ResourceRow:
    project: str
    scope: str
    cases: int
    routes: tuple[ResourceValues, ...]
    controller_revision: str
    source_revision: str
    upstream_revision: str
    wheel_sha256: str
    collector_sha256: str
    runtime: tuple[str, str, str, str, str]


def _readme_resource_values(record: object, project: str, route: str) -> ResourceValues:
    if not isinstance(record, dict) or set(record) != {
        "time_samples_ms",
        "time_median_ms",
        "peak_tree_pss_samples_bytes",
        "peak_tree_pss_median_bytes",
    }:
        raise ValueError(f"{project} {route} requires time and tree PSS samples")
    raw_time = record["time_samples_ms"]
    raw_pss = record["peak_tree_pss_samples_bytes"]
    if (
        not isinstance(raw_time, list)
        or len(raw_time) != 2
        or not isinstance(raw_pss, list)
        or len(raw_pss) != 2
    ):
        raise ValueError(f"{project} {route} requires two paired time and PSS samples")
    time_samples = tuple(_required_positive_number(value, "time_samples_ms") for value in raw_time)
    pss_samples = tuple(_memory_bytes(value, "peak_tree_pss_samples_bytes") for value in raw_pss)
    time_median = _required_positive_number(record["time_median_ms"], "time_median_ms")
    pss_median = record["peak_tree_pss_median_bytes"]
    if not math.isclose(time_median, statistics.median(time_samples), rel_tol=1e-9, abs_tol=1e-6):
        raise ValueError(f"{project} {route} time median differs from samples")
    if (
        type(pss_median) not in (int, float)
        or not math.isfinite(pss_median)
        or pss_median != statistics.median(pss_samples)
    ):
        raise ValueError(f"{project} {route} PSS median differs from samples")
    return ResourceValues(
        (time_samples[0], time_samples[1]),
        time_median,
        (pss_samples[0], pss_samples[1]),
        float(pss_median),
    )


def _readme_resource_identity(raw: object, project: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"{project} requires measured build and collector provenance")
    identity: dict[str, str] = {}
    for key in ("wheel_sha256", "collector_sha256"):
        value = _required_text(raw.get(key), key, max_length=64)
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"{project} requires {key} as SHA-256")
        identity[key] = value
    for key in ("controller_revision", "source_revision", "upstream_revision"):
        value = _required_text(raw.get(key), key, max_length=40)
        if re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValueError(f"{project} requires {key} as a full Git revision")
        identity[key] = value
    return identity


def _readme_resource_runtime(raw: object, project: str) -> tuple[str, str, str, str, str]:
    keys = ("python", "implementation", "pytest", "pytest_cov", "vfy")
    if not isinstance(raw, dict) or set(raw) != set(keys):
        raise ValueError(f"{project} requires the complete measured runtime inventory")
    return (
        _required_text(raw["python"], "python", max_length=100),
        _required_text(raw["implementation"], "implementation", max_length=100),
        _required_text(raw["pytest"], "pytest", max_length=100),
        _required_text(raw["pytest_cov"], "pytest_cov", max_length=100),
        _required_text(raw["vfy"], "vfy", max_length=100),
    )


def _readme_resource_row(raw: object) -> ResourceRow:
    if not isinstance(raw, dict):
        raise ValueError("README resource row must be an object")
    project = _required_text(raw.get("project"), "project", max_length=26)
    cases = raw.get("cases")
    if type(cases) is not int or cases < 1:
        raise ValueError(f"{project} requires a positive full-suite case count")
    if (
        raw.get("parity") != "exact"
        or type(raw.get("warmups")) is not int
        or raw["warmups"] != 0
        or type(raw.get("runs")) is not int
        or raw["runs"] != 2
    ):
        raise ValueError(f"{project} requires exact parity and two measured runs")
    gates = raw.get("gates")
    if not isinstance(gates, dict) or (
        gates.get("serial_coverage") != "exact" or gates.get("parallel_junit") != "passed"
    ):
        raise ValueError(f"{project} requires serial coverage and parallel JUnit gates")
    route_records = raw.get("routes")
    if not isinstance(route_records, dict) or set(route_records) != set(README_RESOURCE_ROUTES):
        raise ValueError(f"{project} requires the same five complete command routes")
    identity = _readme_resource_identity(raw.get("provenance"), project)
    provenance = raw["provenance"]
    return ResourceRow(
        project=project,
        scope=_required_text(raw.get("scope"), "scope", max_length=500),
        cases=cases,
        routes=tuple(
            _readme_resource_values(route_records[route], project, route)
            for route in README_RESOURCE_ROUTES
        ),
        controller_revision=identity["controller_revision"],
        source_revision=identity["source_revision"],
        upstream_revision=identity["upstream_revision"],
        wheel_sha256=identity["wheel_sha256"],
        collector_sha256=identity["collector_sha256"],
        runtime=_readme_resource_runtime(provenance.get("runtime"), project),
    )


def parse_readme_resources(payload: object) -> tuple[ResourceRow, ...]:
    """Validate matched time and sampled whole-tree PSS measurements for the README."""

    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ValueError("README resources require schema 1")
    if (
        payload.get("metric") != "peak_tree_pss_bytes"
        or payload.get("method") != "linux-process-tree-smaps-rollup"
        or payload.get("sample_interval_ms") != 20
    ):
        raise ValueError("README resources require 20 ms sampled whole-tree PSS")
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) != len(README_RESOURCE_PROJECTS):
        raise ValueError("README resources require Packaging, Rich, and attrs rows")
    rows: dict[str, ResourceRow] = {}
    for raw in raw_rows:
        row = _readme_resource_row(raw)
        if row.project not in README_RESOURCE_PROJECTS or row.project in rows:
            raise ValueError("README resources require distinct Packaging, Rich, and attrs rows")
        rows[row.project] = row
    if set(rows) != set(README_RESOURCE_PROJECTS):
        raise ValueError("README resources require Packaging, Rich, and attrs rows")
    if (
        len(
            {
                (row.source_revision, row.wheel_sha256, row.collector_sha256, row.runtime)
                for row in rows.values()
            }
        )
        != 1
    ):
        raise ValueError("README resources use mixed vfy wheels, PSS collectors, or runtimes")
    return tuple(rows[project] for project in README_RESOURCE_PROJECTS)


def _memory_bytes(value: object, field: str) -> int:
    # A Python test process below 1 MiB is implausible; this also catches ms
    # accidentally passed as bytes. The upper bound rejects unit mixups.
    if type(value) is not int or not 1_048_576 <= value <= 1_099_511_627_776:
        raise ValueError(f"{field} must be plausible positive memory bytes")
    return value


def parse_memory_summary(payload: object) -> tuple[MemoryRow, ...]:
    """Qualify serial, parity-checked Linux child peak RSS samples for display."""

    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ValueError("memory summary requires schema 1")
    if (
        payload.get("metric") != "largest_process_peak_rss_bytes"
        or payload.get("method") != "linux-single-child-rusage"
        or payload.get("route") != "serial-stock-pytest-coverage"
    ):
        raise ValueError(
            "memory summary requires serial stock pytest and largest-process Linux RSS"
        )
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) < 2:
        raise ValueError("memory summary requires at least two project rows")
    rows: list[MemoryRow] = []
    seen: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise ValueError("memory row must be an object")
        project = _required_text(raw.get("project"), "project", max_length=26)
        if project in seen:
            raise ValueError(f"duplicate memory project: {project}")
        seen.add(project)
        if raw.get("parity") != "exact" or raw.get("warmups") != 0 or raw.get("runs") != 2:
            raise ValueError(f"{project} memory row lacks exact parity or two measured runs")
        values = raw.get("values")
        if not isinstance(values, dict) or set(values) != {"plain pytest", "vfy", "pytest-cov"}:
            raise ValueError(f"{project} memory row requires all three serial routes")
        parsed: dict[str, MemoryValues] = {}
        for name, record in values.items():
            if not isinstance(record, dict) or set(record) != {"samples_bytes", "median_bytes"}:
                raise ValueError(f"{project} {name} requires byte samples and median")
            raw_samples = record["samples_bytes"]
            if not isinstance(raw_samples, list) or len(raw_samples) != 2:
                raise ValueError(f"{project} {name} requires two RSS samples")
            samples = tuple(
                _memory_bytes(sample, f"{project} {name} sample") for sample in raw_samples
            )
            median = record["median_bytes"]
            if (
                type(median) not in (int, float)
                or not math.isfinite(median)
                or median != statistics.median(samples)
            ):
                raise ValueError(f"{project} {name} RSS median differs from samples")
            parsed[name] = MemoryValues(samples, float(median))
        provenance = raw.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError(f"{project} requires memory provenance")
        wheel = _required_text(provenance.get("wheel_sha256"), "wheel_sha256", max_length=64)
        if re.fullmatch(r"[0-9a-f]{64}", wheel) is None:
            raise ValueError(f"{project} requires a release wheel SHA-256")
        rows.append(
            MemoryRow(
                project=project,
                scope=_required_text(raw.get("scope"), "scope", max_length=500),
                plain=parsed["plain pytest"],
                vfy=parsed["vfy"],
                pytest_cov=parsed["pytest-cov"],
                wheel_sha256=wheel,
                controller_revision=_required_text(
                    provenance.get("controller_revision"), "controller_revision", max_length=40
                ),
                upstream_revision=_required_text(
                    provenance.get("upstream_revision"), "upstream_revision", max_length=40
                ),
            )
        )
    return tuple(rows)


def parse_stock_only_rows(
    payload: object, coverage: ProjectChart, parallel: ProjectChart
) -> tuple[StockOnlyRow, ...]:
    """Qualify stock xdist timings without constructing missing native measurements."""

    if coverage.kind != "coverage" or parallel.kind != "parallel":
        raise ValueError("stock-only rows require coverage and parallel charts")
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ValueError("stock-only summary requires schema 1")
    raw_rows = payload.get("stock_only_rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("stock-only summary requires rows")
    coverage_names = {row.project for row in coverage.rows}
    parallel_names = {row.project for row in parallel.rows}
    rows: list[StockOnlyRow] = []
    seen: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise ValueError("stock-only row must be an object")
        project = _required_text(raw.get("project"), "project", max_length=26)
        if project in seen or project in parallel_names or project not in coverage_names:
            raise ValueError(f"{project} lacks a distinct paired coverage row")
        seen.add(project)
        if (
            raw.get("route") != parallel.route
            or raw.get("workers") != 4
            or raw.get("capture") != "no"
            or raw.get("report") != "JUnit"
            or raw.get("parity") != "passed"
            or raw.get("native_status")
            not in {"unsupported_required_plugins", "unsupported_conftest_plugins"}
        ):
            raise ValueError(f"{project} stock-only route or support gate is unqualified")
        if any(
            field in raw
            for field in (
                "candidate_median_ms",
                "candidate_samples_ms",
                "filegroups_median_ms",
                "filegroups_samples_ms",
                "native_xdist_median_ms",
                "native_filegroups_median_ms",
            )
        ):
            raise ValueError(f"{project} unsupported native routes cannot have timing data")
        raw_samples = raw.get("reference_samples_ms")
        if not isinstance(raw_samples, list) or len(raw_samples) < 2:
            raise ValueError(f"{project} requires at least two stock xdist samples")
        sample_count = raw.get("reference_samples")
        if type(sample_count) is not int or sample_count != len(raw_samples):
            raise ValueError(f"{project} stock xdist sample count differs from raw samples")
        samples = tuple(
            _required_positive_number(value, "reference_samples_ms") for value in raw_samples
        )
        median = _required_positive_number(raw.get("reference_median_ms"), "reference_median_ms")
        if not math.isclose(median, statistics.median(samples), rel_tol=1e-9, abs_tol=1e-6):
            raise ValueError(f"{project} stock xdist median differs from raw samples")
        wheel = _required_text(raw.get("wheel_sha256"), "wheel_sha256", max_length=64)
        if len(wheel) != 64 or any(character not in "0123456789abcdef" for character in wheel):
            raise ValueError(f"{project} requires a release wheel SHA-256")
        rows.append(
            StockOnlyRow(
                project=project,
                scope=_required_text(raw.get("scope"), "scope", max_length=500),
                reference_median_ms=median,
                reference_samples_ms=samples,
                native_status=raw["native_status"],
                reason=_required_text(raw.get("reason"), "reason", max_length=500),
                wheel_sha256=wheel,
                controller_revision=_required_text(
                    raw.get("controller_revision"), "controller_revision", max_length=40
                ),
                upstream_revision=_required_text(
                    raw.get("upstream_revision"), "upstream_revision", max_length=40
                ),
            )
        )
    return tuple(rows)


def parse_readme_stock_rows(
    payload: object,
    coverage: ProjectChart,
    parallel: ProjectChart,
    requested: tuple[str, ...] | None,
) -> tuple[StockOnlyRow, ...]:
    """Qualify the stock rows used by this render; default renders qualify all."""

    if requested is None:
        return parse_stock_only_rows(payload, coverage, parallel)
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ValueError("stock-only summary requires schema 1")
    raw_rows = payload.get("stock_only_rows")
    if (
        not isinstance(raw_rows, list)
        or not raw_rows
        or not all(isinstance(row, dict) for row in raw_rows)
    ):
        raise ValueError("stock-only summary requires object rows")
    selected = [row for row in raw_rows if row.get("project") in requested]
    if not selected:
        return ()
    return parse_stock_only_rows({**payload, "stock_only_rows": selected}, coverage, parallel)


FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif"
WIDTH = 640


def _required_text(value: object, field: str, *, max_length: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty text")
    result = value.strip()
    if len(result) > max_length:
        raise ValueError(f"{field} exceeds chart label width")
    return result


def _required_positive_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a positive finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field} must be a positive finite number")
    return number


def _required_samples(value: object, field: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return value


def _qualified_row_status(raw_row: dict[str, object], kind: str, project: str) -> tuple[str, int]:
    status = raw_row.get("status")
    if raw_row.get("parity") != "passed" or status not in {"publication", "diagnostic"}:
        raise ValueError(f"{project} lacks qualified status or passing parity")
    assert isinstance(status, str)
    minimum = 2 if status == "diagnostic" else 30 if kind == "mutation" else 15
    return status, minimum


def _optional_series(raw_row: dict[str, object], kind: str, project: str) -> tuple[object, ...]:
    baseline_median = raw_row.get("baseline_median_ms")
    baseline_samples = raw_row.get("baseline_samples")
    filegroups_median = raw_row.get("filegroups_median_ms")
    filegroups_samples = raw_row.get("filegroups_samples")
    if (baseline_median is None) != (baseline_samples is None):
        raise ValueError(f"{project} requires both plain-pytest median and samples")
    if kind != "coverage" and baseline_median is not None:
        raise ValueError(f"{project} plain-pytest control belongs only in coverage charts")
    if (filegroups_median is None) != (filegroups_samples is None):
        raise ValueError(f"{project} requires both file-group median and samples")
    if (kind == "parallel") != (filegroups_median is not None):
        raise ValueError(f"{project} file-group timing belongs in parallel chart rows")
    return baseline_median, baseline_samples, filegroups_median, filegroups_samples


def _selection_cases(
    raw_row: dict[str, object], kind: str, project: str
) -> tuple[int, int, int] | None:
    fields = ("full_cases", "reference_cases", "candidate_cases")
    if kind != "selection":
        if any(field in raw_row for field in fields):
            raise ValueError(f"{project} selected cases belong only in selection charts")
        return None
    full, reference, candidate = (
        _required_samples(raw_row.get(field), field, minimum=1) for field in fields
    )
    if reference > candidate or candidate > full:
        raise ValueError(f"{project} selected cases must satisfy testmon ≤ vfy ≤ full suite")
    return full, reference, candidate


def _check_row_sample_counts(row: ProjectRow) -> None:
    if (
        row.reference_samples != row.candidate_samples
        or (row.baseline_samples is not None and row.baseline_samples != row.reference_samples)
        or (row.filegroups_samples is not None and row.filegroups_samples != row.reference_samples)
    ):
        raise ValueError(f"{row.project} tool sample counts must match")


def parse_project_charts(payload: object) -> tuple[ProjectChart, ...]:
    """Accept only parity-gated comparisons with matching routes and sample floors."""

    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ValueError("project chart summary requires schema 1")
    raw_charts = payload.get("charts")
    if not isinstance(raw_charts, list) or not raw_charts:
        raise ValueError("project chart summary requires charts")
    charts: list[ProjectChart] = []
    seen_kinds: set[str] = set()
    for raw_chart in raw_charts:
        if not isinstance(raw_chart, dict):
            raise ValueError("each project chart must be an object")
        kind = _required_text(raw_chart.get("kind"), "kind")
        if kind not in {"coverage", "selection", "mutation", "parallel"} or kind in seen_kinds:
            raise ValueError(f"unsupported or duplicate project chart kind: {kind}")
        seen_kinds.add(kind)
        title = _required_text(raw_chart.get("title"), "title", max_length=38)
        route = _required_text(raw_chart.get("route"), "route", max_length=70)
        context = _required_text(raw_chart.get("context"), "context", max_length=70)
        reference = _required_text(raw_chart.get("reference"), "reference", max_length=18)
        candidate = _required_text(raw_chart.get("candidate"), "candidate", max_length=18)
        raw_rows = raw_chart.get("rows")
        if not isinstance(raw_rows, list) or len(raw_rows) < 2:
            raise ValueError(f"{kind} chart requires at least two project rows")
        rows: list[ProjectRow] = []
        seen_projects: set[str] = set()
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                raise ValueError(f"{kind} rows must be objects")
            project = _required_text(raw_row.get("project"), "project", max_length=26)
            if project in seen_projects:
                raise ValueError(f"duplicate project in {kind} chart: {project}")
            seen_projects.add(project)
            if raw_row.get("route") != route:
                raise ValueError(f"{project} route differs from {kind} chart route")
            status, minimum = _qualified_row_status(raw_row, kind, project)
            baseline_median, baseline_samples, filegroups_median, filegroups_samples = (
                _optional_series(raw_row, kind, project)
            )
            selected_cases = _selection_cases(raw_row, kind, project)
            context_status = raw_row.get("context_status")
            if kind == "selection" and context_status not in {"exact", "differs"}:
                raise ValueError(f"{project} requires per-test context status")
            if kind != "selection" and context_status is not None:
                raise ValueError(f"{project} per-test context status belongs to selection")
            rows.append(
                ProjectRow(
                    project=project,
                    scope=_required_text(
                        raw_row.get("scope"),
                        "scope",
                        max_length=500 if "display_scope" in raw_row else 70,
                    ),
                    reference_median_ms=_required_positive_number(
                        raw_row.get("reference_median_ms"), "reference_median_ms"
                    ),
                    candidate_median_ms=_required_positive_number(
                        raw_row.get("candidate_median_ms"), "candidate_median_ms"
                    ),
                    reference_samples=_required_samples(
                        raw_row.get("reference_samples"),
                        "reference_samples",
                        minimum=minimum,
                    ),
                    candidate_samples=_required_samples(
                        raw_row.get("candidate_samples"),
                        "candidate_samples",
                        minimum=minimum,
                    ),
                    warmups=_required_samples(
                        raw_row.get("warmups"),
                        "warmups",
                        minimum=0 if status == "diagnostic" else 3,
                    ),
                    status=status,
                    source=_required_text(raw_row.get("source"), "source"),
                    baseline_median_ms=(
                        _required_positive_number(baseline_median, "baseline_median_ms")
                        if baseline_median is not None
                        else None
                    ),
                    baseline_samples=(
                        _required_samples(baseline_samples, "baseline_samples", minimum=minimum)
                        if baseline_samples is not None
                        else None
                    ),
                    display_scope=(
                        _required_text(raw_row["display_scope"], "display_scope", max_length=50)
                        if "display_scope" in raw_row
                        else None
                    ),
                    filegroups_median_ms=(
                        _required_positive_number(filegroups_median, "filegroups_median_ms")
                        if filegroups_median is not None
                        else None
                    ),
                    filegroups_samples=(
                        _required_samples(filegroups_samples, "filegroups_samples", minimum=minimum)
                        if filegroups_samples is not None
                        else None
                    ),
                    full_cases=selected_cases[0] if selected_cases is not None else None,
                    reference_cases=selected_cases[1] if selected_cases is not None else None,
                    candidate_cases=selected_cases[2] if selected_cases is not None else None,
                    context_status=context_status if isinstance(context_status, str) else None,
                )
            )
            _check_row_sample_counts(rows[-1])
        has_controls = any(row.baseline_median_ms is not None for row in rows)
        if (
            kind == "coverage"
            and has_controls
            and not all(row.baseline_median_ms is not None for row in rows)
        ):
            raise ValueError("coverage chart requires plain-pytest controls for every row")
        charts.append(ProjectChart(kind, title, route, context, reference, candidate, tuple(rows)))
    return tuple(charts)


def render_project_chart(chart: ProjectChart) -> str:
    """Render elapsed-time bars or normalized selection ratios."""

    if chart.kind == "coverage" and all(row.baseline_median_ms is not None for row in chart.rows):
        return render_coverage_project_chart(chart)
    if chart.kind == "parallel":
        return render_parallel_project_chart(chart)
    if chart.kind in {"mutation", "selection"}:
        return render_two_route_elapsed_chart(chart)

    ratios = [row.reference_median_ms / row.candidate_median_ms for row in chart.rows]
    if any(not math.isfinite(ratio) or ratio <= 0 for ratio in ratios):
        raise ValueError("project chart median ratios must be finite and positive")
    low_power = min(-1, math.floor(math.log2(min(ratios))))
    high_power = max(1, math.ceil(math.log2(max(ratios))))
    plot_left, plot_right = 24, 520
    row_top = 154
    row_height = 126 if any(row.baseline_median_ms is not None for row in chart.rows) else 108
    bottom = row_top + len(chart.rows) * row_height
    height = bottom + 67

    def x(ratio: float) -> float:
        return plot_left + (math.log2(ratio) - low_power) / (high_power - low_power) * (
            plot_right - plot_left
        )

    escaped_title = html.escape(chart.title)
    escaped_route = html.escape(chart.route)
    escaped_context = html.escape(chart.context)
    reference = html.escape(chart.reference)
    candidate = html.escape(chart.candidate)
    descriptions = "; ".join(
        f"{html.escape(row.project)} ({html.escape(row.scope)}): "
        f"{reference} {row.reference_median_ms / 1000:.3f} seconds, "
        f"{candidate} {row.candidate_median_ms / 1000:.3f} seconds, "
        + (
            f"plain pytest {row.baseline_median_ms / 1000:.3f} seconds, "
            if row.baseline_median_ms is not None
            else ""
        )
        + f"ratio {ratio:.3f} times, source {html.escape(row.source)}"
        for row, ratio in zip(chart.rows, ratios, strict=True)
    )
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="chart-title chart-desc">',
        f'<title id="chart-title">{escaped_title}</title>',
        f'<desc id="chart-desc">{escaped_route}. {escaped_context}. '
        f"Reference median divided by candidate median; 1\u00d7 is equal; "
        f"right of 1\u00d7 means {candidate} is faster. {descriptions}.</desc>",
        "<style>.surface{fill:#fafafa}.ink{fill:#262626}.muted{fill:#626262}"
        ".rule{stroke:#dedede}.faster{fill:#e85d04;stroke:#e85d04}"
        ".slower{fill:#525252;stroke:#525252}"
        "@media(prefers-color-scheme:dark){.surface{fill:#202020}.ink{fill:#f5f5f5}"
        ".muted{fill:#d4d4d4}.rule{stroke:#525252}.faster{fill:#ff8736}"
        ".slower{fill:#a3a3a3}}</style>",
        '<rect class="surface" width="100%" height="100%" rx="8"/>',
        f'<text class="ink" x="24" y="37" font-size="22" font-weight="700" '
        f'font-family="{FONT}">{escaped_title}</text>',
        f'<text class="muted" x="24" y="61" font-size="16" '
        f'font-family="{FONT}">{escaped_route}</text>',
        f'<text class="muted" x="24" y="82" font-size="16" '
        f'font-family="{FONT}">{escaped_context}</text>',
        f'<text class="muted" x="24" y="111" font-size="16" '
        f'font-family="{FONT}">Speedup = {reference} median ÷ {candidate} median</text>',
        f'<text class="muted" x="616" y="134" text-anchor="end" font-size="16" '
        f'font-family="{FONT}">Right of 1\u00d7 = faster</text>',
        '<line class="rule" x1="24" y1="140" x2="616" y2="140"/>',
    ]
    for power in range(low_power, high_power + 1):
        ratio = 2.0**power
        tick_x = x(ratio)
        for index in range(len(chart.rows)):
            top = row_top + index * row_height
            lines.append(
                f'<line class="rule" x1="{tick_x:.1f}" y1="{top + 48}" '
                f'x2="{tick_x:.1f}" y2="{top + 74}"/>'
            )
        lines.append(
            f'<text class="muted" x="{tick_x:.1f}" y="{bottom + 14}" '
            f'text-anchor="middle" font-size="16" font-family="{FONT}">{ratio:g}\u00d7</text>'
        )
    anchor = x(1.0)
    for index in range(len(chart.rows)):
        top = row_top + index * row_height
        lines.append(
            f'<line x1="{anchor:.1f}" y1="{top + 48}" '
            f'x2="{anchor:.1f}" y2="{top + 74}" stroke="#e85d04" '
            'stroke-width="1.5" stroke-dasharray="4 4"/>'
        )
    for index, (row, ratio) in enumerate(zip(chart.rows, ratios, strict=True)):
        top = row_top + index * row_height
        midpoint = top + 61
        label = html.escape(row.project)
        scope = html.escape(row.display_scope or row.scope)
        color_class = "faster" if ratio >= 1 else "slower"
        ratio_label = f"{ratio:.2f}\u00d7 faster" if ratio >= 1 else f"{1 / ratio:.2f}\u00d7 slower"
        bar_left = min(anchor, x(ratio))
        bar_width = abs(x(ratio) - anchor)
        lines.extend(
            [
                f'<text class="ink" x="24" y="{top + 20}" font-size="18" '
                f'font-weight="700" font-family="{FONT}">{label}</text>',
                f'<text class="muted" x="24" y="{top + 38}" font-size="16" '
                f'font-family="{FONT}">{scope}</text>',
                f'<rect class="{color_class}" x="{bar_left:.1f}" y="{midpoint - 6}" '
                f'width="{bar_width:.1f}" height="12" rx="3" stroke="none"/>',
                f'<circle class="{color_class}" cx="{x(ratio):.1f}" cy="{midpoint}" r="7"/>',
                f'<text class="ink" x="616" y="{midpoint + 6}" text-anchor="end" '
                f'font-size="19" font-weight="700" font-family="{FONT}">{ratio_label}</text>',
                f'<text class="muted" x="24" y="{top + 91}" font-size="16" '
                f'font-family="{FONT}">{candidate} {row.candidate_median_ms / 1000:.2f} s '
                f"· {reference} {row.reference_median_ms / 1000:.2f} s</text>",
            ]
        )
        if row.baseline_median_ms is not None:
            lines.append(
                f'<text class="muted" x="24" y="{top + 111}" font-size="16" '
                f'font-family="{FONT}">plain pytest {row.baseline_median_ms / 1000:.2f} s '
                "(uninstrumented)</text>"
            )
        if index < len(chart.rows) - 1:
            lines.append(
                f'<line class="rule" x1="24" y1="{top + row_height - 5}" '
                f'x2="616" y2="{top + row_height - 5}"/>'
            )
    lines.extend(
        [
            f'<text class="muted" x="320" y="{bottom + 42}" text-anchor="middle" '
            f'font-size="16" font-family="{FONT}">Median wall time · base-two speedup scale</text>',
            "</svg>",
        ]
    )
    return "\n".join(lines) + "\n"


def _unit_scale(peak: float) -> tuple[float, float]:
    """Choose a readable zero-origin tick step and maximum for one project."""

    target_step = peak / 5
    unit = float(10 ** math.floor(math.log10(target_step)))
    step = float(next(size * unit for size in (1, 2, 5, 10) if size * unit >= target_step))
    return step, float(math.ceil(peak / step) * step)


def _seconds_scale(peak_ms: float) -> tuple[float, float]:
    return _unit_scale(peak_ms / 1000)


def render_two_route_elapsed_chart(chart: ProjectChart) -> str:
    """Compare mutation or affected-test reruns on zero-origin elapsed-time bars."""

    selection = chart.kind == "selection"
    plot_left, plot_right = 155, 510
    row_top, row_height = 158, 176
    bottom = row_top + len(chart.rows) * row_height
    height = bottom + (84 if selection else 62)
    title = html.escape(chart.title)
    route = html.escape(chart.route)
    context = html.escape(chart.context)
    descriptions = "; ".join(
        f"{html.escape(row.project)} ({html.escape(row.scope)}"
        + (f"; {html.escape(row.display_scope)}" if row.display_scope else "")
        + "): "
        f"{html.escape(chart.reference)} {row.reference_median_ms / 1000:.3f} seconds, "
        f"{html.escape(chart.candidate)} {row.candidate_median_ms / 1000:.3f} seconds, "
        + (
            f"selected {row.reference_cases} by {html.escape(chart.reference)}, "
            f"{row.candidate_cases} by {html.escape(chart.candidate)}, "
            f"of {row.full_cases} full-suite tests, "
            f"per-test context status: {row.context_status}, "
            if selection
            else ""
        )
        + f"median ratio {row.reference_median_ms / row.candidate_median_ms:.3f} times, "
        f"source {html.escape(row.source)}"
        for row in chart.rows
    )
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="chart-title chart-desc">',
        f'<title id="chart-title">{title}</title>',
        f'<desc id="chart-desc">{route}. {context}. Each project has its own '
        f"zero-origin elapsed-seconds scale; shorter bars are faster. "
        + (
            "vfy writes full coverage JSON, schema-7 data and JUnit; pytest-testmon writes JUnit. "
            if selection
            else ""
        )
        + f"{descriptions}.</desc>",
        "<style>.surface{fill:#fafafa}.ink{fill:#262626}.muted{fill:#626262}"
        ".rule{stroke:#dedede}.reference{fill:#525252}.vfy{fill:#e85d04}"
        "@media(prefers-color-scheme:dark){.surface{fill:#202020}.ink{fill:#f5f5f5}"
        ".muted{fill:#d4d4d4}.rule{stroke:#525252}.reference{fill:#a3a3a3}"
        ".vfy{fill:#ff8736}}</style>",
        '<rect class="surface" width="100%" height="100%" rx="8"/>',
        f'<text class="ink" x="24" y="38" font-size="22" font-weight="700" '
        f'font-family="{FONT}">{title}</text>',
        f'<text class="muted" x="24" y="65" font-size="16" font-family="{FONT}">{route}</text>',
        f'<text class="muted" x="24" y="89" font-size="16" font-family="{FONT}">{context}</text>',
        f'<text class="muted" x="24" y="121" font-size="16" '
        f'font-family="{FONT}">'
        f"{'Affected rerun' if selection else 'Complete mutation run'} · lower is faster</text>",
        '<line class="rule" x1="24" y1="135" x2="616" y2="135"/>',
    ]

    def x(milliseconds: float, maximum: float) -> float:
        return plot_left + (plot_right - plot_left) * milliseconds / (maximum * 1000)

    for index, row in enumerate(chart.rows):
        top = row_top + index * row_height
        ratio = row.reference_median_ms / row.candidate_median_ms
        ratio_label = f"{ratio:.2f}\u00d7 faster" if ratio >= 1 else f"{1 / ratio:.2f}\u00d7 slower"
        if selection:
            scope = (
                f"Selected {row.candidate_cases:,} vfy / {row.reference_cases:,} testmon"
                f" of {row.full_cases:,} tests"
            )
        else:
            scope = row.display_scope or row.scope.rsplit(" · ", 1)[-1]
            if len(scope) > 36:
                count = re.search(r"\b[\d,]+ (?:exact )?(?:body )?(?:mutations?|mutants?)\b", scope)
                scope = count.group() if count else "Selected mutations"
        step, maximum = _seconds_scale(max(row.reference_median_ms, row.candidate_median_ms))
        lines.extend(
            [
                f'<text class="ink" x="24" y="{top + 16}" font-size="19" '
                f'font-weight="700" font-family="{FONT}">{html.escape(row.project)}</text>',
                f'<text class="muted" x="24" y="{top + 35}" font-size="16" '
                f'font-family="{FONT}">{html.escape(scope)}</text>',
                f'<text class="ink" x="616" y="{top + 18}" text-anchor="end" '
                f'font-size="18" font-weight="700" font-family="{FONT}">{ratio_label}</text>',
            ]
        )
        for tick_index in range(round(maximum / step) + 1):
            tick = tick_index * step
            tick_x = x(tick * 1000, maximum)
            lines.extend(
                [
                    f'<line class="rule" x1="{tick_x:.1f}" y1="{top + 43}" '
                    f'x2="{tick_x:.1f}" y2="{top + 102}"/>',
                    f'<text class="muted" x="{tick_x:.1f}" y="{top + 132}" '
                    f'text-anchor="middle" font-size="16" font-family="{FONT}">{tick:g}</text>',
                ]
            )
        for offset, label, median, style in (
            (56, chart.reference, row.reference_median_ms, "reference"),
            (84, chart.candidate, row.candidate_median_ms, "vfy"),
        ):
            center = top + offset
            lines.extend(
                [
                    f'<text class="muted" x="24" y="{center + 5}" font-size="16" '
                    f'font-family="{FONT}">{html.escape(label)}</text>',
                    f'<rect class="{style}" x="{plot_left}" y="{center - 9}" '
                    f'width="{x(median, maximum) - plot_left:.1f}" height="18" rx="3"/>',
                    f'<text class="ink" x="616" y="{center + 6}" text-anchor="end" '
                    f'font-size="17" font-weight="600" font-family="{FONT}">'
                    f"{median / 1000:.2f} s</text>",
                ]
            )
        if index < len(chart.rows) - 1:
            lines.append(
                f'<line class="rule" x1="24" y1="{top + row_height - 5}" '
                f'x2="616" y2="{top + row_height - 5}"/>'
            )
    lines.extend(
        [
            f'<text class="muted" x="320" y="{bottom + 36}" text-anchor="middle" '
            f'font-size="16" font-family="{FONT}">'
            "Median seconds · each project has its own zero-origin scale</text>",
        ]
    )
    if selection:
        lines.append(
            f'<text class="muted" x="320" y="{bottom + 57}" text-anchor="middle" '
            f'font-size="16" font-family="{FONT}">'
            "vfy JSON + aggregate coverage match oracle; contexts may differ</text>"
        )
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def render_coverage_project_chart(chart: ProjectChart) -> str:
    """Show complete command times on separate zero-origin project scales."""

    if any(row.baseline_median_ms is None for row in chart.rows):
        raise ValueError("coverage time bars require plain-pytest controls")
    plot_left, plot_right = 155, 510
    row_top, row_height = 158, 188
    bottom = row_top + len(chart.rows) * row_height
    height = bottom + 62
    title = html.escape(chart.title)
    route = html.escape(chart.route)
    context = html.escape(chart.context)
    descriptions = "; ".join(
        f"{html.escape(row.project)} ({html.escape(row.scope)}): "
        f"plain pytest {cast(float, row.baseline_median_ms) / 1000:.3f} seconds, "
        f"{html.escape(chart.candidate)} {row.candidate_median_ms / 1000:.3f} seconds, "
        f"{html.escape(chart.reference)} "
        f"{row.reference_median_ms / 1000:.3f} seconds, complete-command ratio "
        f"{row.reference_median_ms / row.candidate_median_ms:.3f} times, "
        f"source {html.escape(row.source)}"
        for row in chart.rows
    )

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="chart-title chart-desc">',
        f'<title id="chart-title">{title}</title>',
        f'<desc id="chart-desc">{route}. {context}. Each project has its own '
        f"zero-origin elapsed-seconds scale; longer bars are slower within a "
        f"project. {descriptions}.</desc>",
        "<style>.surface{fill:#fafafa}.ink{fill:#262626}.muted{fill:#626262}"
        ".rule{stroke:#dedede}.vfy{fill:#e85d04}.reference{fill:#525252}"
        ".plain{fill:#a3a3a3}"
        "@media(prefers-color-scheme:dark){.surface{fill:#202020}.ink{fill:#f5f5f5}"
        ".muted{fill:#d4d4d4}.rule{stroke:#525252}.vfy{fill:#ff8736}"
        ".reference{fill:#a3a3a3}.plain{fill:#737373}}</style>",
        '<rect class="surface" width="100%" height="100%" rx="8"/>',
        f'<text class="ink" x="24" y="38" font-size="22" font-weight="700" '
        f'font-family="{FONT}">{title}</text>',
        f'<text class="muted" x="24" y="65" font-size="16" font-family="{FONT}">{route}</text>',
        f'<text class="muted" x="24" y="89" font-size="16" font-family="{FONT}">{context}</text>',
        f'<text class="muted" x="24" y="121" font-size="16" '
        f'font-family="{FONT}">Complete command time · lower is faster</text>',
        '<line class="rule" x1="24" y1="135" x2="616" y2="135"/>',
    ]

    def x(milliseconds: float, max_seconds: float) -> float:
        return plot_left + (plot_right - plot_left) * milliseconds / (max_seconds * 1000)

    for index, row in enumerate(chart.rows):
        top = row_top + index * row_height
        ratio = row.reference_median_ms / row.candidate_median_ms
        ratio_label = f"{ratio:.2f}\u00d7 faster" if ratio >= 1 else f"{1 / ratio:.2f}\u00d7 slower"
        step, maximum = _seconds_scale(
            max(
                row.reference_median_ms,
                row.candidate_median_ms,
                cast(float, row.baseline_median_ms),
            )
        )

        lines.extend(
            [
                f'<text class="ink" x="24" y="{top + 16}" font-size="19" '
                f'font-weight="700" font-family="{FONT}">{html.escape(row.project)}</text>',
                f'<text class="muted" x="24" y="{top + 35}" font-size="16" '
                f'font-family="{FONT}">{html.escape(row.display_scope or row.scope)}</text>',
                f'<text class="ink" x="616" y="{top + 18}" text-anchor="end" '
                f'font-size="18" font-weight="700" font-family="{FONT}">{ratio_label}</text>',
            ]
        )
        for tick_index in range(round(maximum / step) + 1):
            tick = tick_index * step
            tick_x = x(tick * 1000, maximum)
            lines.extend(
                [
                    f'<line class="rule" x1="{tick_x:.1f}" y1="{top + 43}" '
                    f'x2="{tick_x:.1f}" y2="{top + 102}"/>',
                    f'<text class="muted" x="{tick_x:.1f}" y="{top + 160}" '
                    f'text-anchor="middle" font-size="16" font-family="{FONT}">{tick:g}</text>',
                ]
            )
        for offset, label, median, style in (
            (56, "plain pytest", cast(float, row.baseline_median_ms), "plain"),
            (84, "vfy", row.candidate_median_ms, "vfy"),
            (112, "pytest-cov", row.reference_median_ms, "reference"),
        ):
            center = top + offset
            lines.extend(
                [
                    f'<text class="muted" x="24" y="{center + 5}" font-size="16" '
                    f'font-family="{FONT}">{label}</text>',
                    f'<rect class="{style}" x="{plot_left}" y="{center - 9}" '
                    f'width="{x(median, maximum) - plot_left:.1f}" height="18" rx="3"/>',
                    f'<text class="ink" x="616" y="{center + 6}" text-anchor="end" '
                    f'font-size="17" font-weight="600" font-family="{FONT}">'
                    f"{median / 1000:.2f} s</text>",
                ]
            )
        if index < len(chart.rows) - 1:
            lines.append(
                f'<line class="rule" x1="24" y1="{top + row_height - 5}" '
                f'x2="616" y2="{top + row_height - 5}"/>'
            )
    lines.extend(
        [
            f'<text class="muted" x="320" y="{bottom + 36}" text-anchor="middle" '
            f'font-size="16" font-family="{FONT}">'
            "Median seconds · each project has its own zero-origin scale</text>",
            "</svg>",
        ]
    )
    return "\n".join(lines) + "\n"


def render_parallel_project_chart(chart: ProjectChart) -> str:
    """Compare pytest-xdist with vfy's file scheduler per project."""

    if any(row.filegroups_median_ms is None for row in chart.rows):
        raise ValueError("parallel chart requires file-group timing for each project")
    plot_left, plot_right = 190, 510
    row_top, row_height = 158, 178
    bottom = row_top + len(chart.rows) * row_height
    height = bottom + 62
    title = html.escape(chart.title)
    route = html.escape(chart.route)
    context = html.escape(chart.context)
    descriptions = "; ".join(
        f"{html.escape(row.project)} ({html.escape(row.scope)}): "
        f"pytest-xdist {row.reference_median_ms / 1000:.3f} seconds, "
        "vfy native workers (file scheduler) "
        f"{cast(float, row.filegroups_median_ms) / 1000:.3f} seconds, "
        "file-group ratio "
        f"{row.reference_median_ms / cast(float, row.filegroups_median_ms):.3f} times, "
        f"source {html.escape(row.source)}"
        for row in chart.rows
    )
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="chart-title chart-desc">',
        f'<title id="chart-title">{title}</title>',
        f'<desc id="chart-desc">{route}. {context}. Each project has its own '
        f"zero-origin elapsed-seconds scale. {descriptions}.</desc>",
        "<style>.surface{fill:#fafafa}.ink{fill:#262626}.muted{fill:#626262}"
        ".rule{stroke:#dedede}.reference{fill:#525252}.vfy{fill:#e85d04}"
        "@media(prefers-color-scheme:dark){.surface{fill:#202020}.ink{fill:#f5f5f5}"
        ".muted{fill:#d4d4d4}.rule{stroke:#525252}.reference{fill:#a3a3a3}"
        ".vfy{fill:#ff8736}}</style>",
        '<rect class="surface" width="100%" height="100%" rx="8"/>',
        f'<text class="ink" x="24" y="38" font-size="22" font-weight="700" '
        f'font-family="{FONT}">{title}</text>',
        f'<text class="muted" x="24" y="65" font-size="16" font-family="{FONT}">{route}</text>',
        f'<text class="muted" x="24" y="89" font-size="16" font-family="{FONT}">{context}</text>',
        f'<text class="muted" x="24" y="121" font-size="16" '
        f'font-family="{FONT}">Complete command time · lower is faster</text>',
        '<line class="rule" x1="24" y1="135" x2="616" y2="135"/>',
    ]

    def x(milliseconds: float, max_seconds: float) -> float:
        return plot_left + (plot_right - plot_left) * milliseconds / (max_seconds * 1000)

    def comparison(reference: float, candidate: float) -> str:
        ratio = reference / candidate
        return f"{ratio:.2f}\u00d7 faster" if ratio >= 1 else f"{1 / ratio:.2f}\u00d7 slower"

    for index, row in enumerate(chart.rows):
        top = row_top + index * row_height
        filegroups = cast(float, row.filegroups_median_ms)
        step, maximum = _seconds_scale(max(row.reference_median_ms, filegroups))
        lines.extend(
            [
                f'<text class="ink" x="24" y="{top + 16}" font-size="19" '
                f'font-weight="700" font-family="{FONT}">{html.escape(row.project)}</text>',
                f'<text class="muted" x="24" y="{top + 35}" font-size="16" '
                f'font-family="{FONT}">{html.escape(row.display_scope or row.scope)}</text>',
            ]
        )
        for tick_index in range(round(maximum / step) + 1):
            tick = tick_index * step
            tick_x = x(tick * 1000, maximum)
            lines.extend(
                [
                    f'<line class="rule" x1="{tick_x:.1f}" y1="{top + 43}" '
                    f'x2="{tick_x:.1f}" y2="{top + 102}"/>',
                    f'<text class="muted" x="{tick_x:.1f}" y="{top + 131}" '
                    f'text-anchor="middle" font-size="16" font-family="{FONT}">{tick:g}</text>',
                ]
            )
        for offset, label, median, style in (
            (56, "pytest-xdist", row.reference_median_ms, "reference"),
            (84, "vfy native workers", filegroups, "vfy"),
        ):
            center = top + offset
            lines.extend(
                [
                    f'<text class="muted" x="24" y="{center + 5}" font-size="16" '
                    f'font-family="{FONT}">{label}</text>',
                    f'<rect class="{style}" x="{plot_left}" y="{center - 9}" '
                    f'width="{x(median, maximum) - plot_left:.1f}" height="18" rx="3"/>',
                    f'<text class="ink" x="616" y="{center + 6}" text-anchor="end" '
                    f'font-size="17" font-weight="600" font-family="{FONT}">'
                    f"{median / 1000:.2f} s</text>",
                ]
            )
        groups_result = comparison(row.reference_median_ms, filegroups)
        lines.append(
            f'<text class="muted" x="24" y="{top + 154}" font-size="16" '
            f'font-family="{FONT}">vs pytest-xdist: vfy native workers {groups_result}</text>'
        )
        if index < len(chart.rows) - 1:
            lines.append(
                f'<line class="rule" x1="24" y1="{top + row_height - 5}" '
                f'x2="616" y2="{top + row_height - 5}"/>'
            )
    lines.extend(
        [
            f'<text class="muted" x="320" y="{bottom + 36}" text-anchor="middle" '
            f'font-size="16" font-family="{FONT}">'
            "Median seconds · each project has its own zero-origin scale</text>",
            "</svg>",
        ]
    )
    return "\n".join(lines) + "\n"


def render_readme_overview(
    coverage: ProjectChart,
    parallel: ProjectChart,
    stock_only: tuple[StockOnlyRow, ...] = (),
) -> str:
    """Group all measured commands on a shared elapsed-time scale per project."""
    if coverage.kind != "coverage" or parallel.kind != "parallel":
        raise ValueError("README overview requires coverage and parallel charts")
    coverage_rows = {row.project: row for row in coverage.rows}
    if any(row.project not in coverage_rows for row in parallel.rows) or any(
        row.project not in coverage_rows for row in stock_only
    ):
        raise ValueError("parallel projects require matching coverage measurements")
    if {row.project for row in parallel.rows} & {row.project for row in stock_only}:
        raise ValueError("stock-only project also has native parallel measurements")
    groups: tuple[ProjectRow | StockOnlyRow, ...] = (*parallel.rows, *stock_only)
    height = 110 + len(groups) * 244
    support_notes = "; ".join(
        f"{html.escape(row.project)} native routes are unsupported because "
        f"{html.escape(row.reason)}"
        for row in stock_only
    )
    support_suffix = f" {support_notes}." if support_notes else ""
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="{height}" '
        f'viewBox="0 0 640 {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Coverage and parallel tests on real projects</title>',
        '<desc id="desc">Complete-command median seconds on CPython 3.12, Linux. '
        "Each project groups its measured commands on one zero-origin scale. "
        "Labels identify serial coverage, serial plain pytest, and four-worker "
        "tests without coverage, using vfy's file scheduler when supported."
        f"{support_suffix}</desc>",
        "<style>.surface{fill:#fafafa}.ink{fill:#262626}.muted{fill:#626262}"
        ".rule{stroke:#dedede}.reference{fill:#525252}.vfy{fill:#e85d04}"
        ".plain{fill:#a3a3a3}"
        "@media(prefers-color-scheme:dark){.surface{fill:#202020}.ink{fill:#f5f5f5}"
        ".muted{fill:#d4d4d4}.rule{stroke:#525252}.reference{fill:#a3a3a3}"
        ".vfy{fill:#ff8736}.plain{fill:#737373}}</style>",
        '<rect class="surface" width="100%" height="100%" rx="8"/>',
        f'<g font-family="{FONT}">',
    ]

    def label(
        value: str,
        x: float,
        y: int,
        *,
        size: int = 16,
        bold: bool = False,
        muted: bool = False,
        anchor: str = "start",
    ) -> None:
        lines.append(
            f'<text class="{"muted" if muted else "ink"}" x="{x}" y="{y}" '
            f'font-size="{size}" font-weight="{700 if bold else 400}" '
            f'text-anchor="{anchor}">{html.escape(value)}</text>'
        )

    label("Coverage and parallel tests", 24, 34, bold=True)
    label("Complete-command medians · seconds · lower is faster", 24, 59, muted=True)
    for project_index, parallel_row in enumerate(groups):
        row = coverage_rows[parallel_row.project]
        if row.baseline_median_ms is None:
            raise ValueError("README overview requires plain pytest controls")
        if isinstance(parallel_row, ProjectRow) and parallel_row.filegroups_median_ms is None:
            raise ValueError("README overview requires native file-scheduler measurements")
        native_groups = (
            parallel_row.filegroups_median_ms if isinstance(parallel_row, ProjectRow) else None
        )
        series = (
            (coverage.reference, "serial · coverage on", row.reference_median_ms, "reference"),
            ("vfy coverage", "serial · coverage on", row.candidate_median_ms, "vfy"),
            ("pytest", "serial · coverage off", row.baseline_median_ms, "plain"),
            (
                parallel.reference,
                "4 workers · coverage off",
                parallel_row.reference_median_ms,
                "reference",
            ),
            (
                "vfy native workers",
                "4 workers · coverage off",
                native_groups,
                "vfy",
            ),
        )
        top = 92 + project_index * 244
        label(row.project, 24, top, bold=True, size=18)
        step, maximum = _seconds_scale(max(item[2] for item in series if item[2] is not None))
        for index, (name, condition, value, style) in enumerate(series):
            y = top + 15 + index * 36
            label(name, 24, y + 12, size=15)
            label(condition, 24, y + 27, size=12, muted=True)
            if value is None:
                label("Unsupported plug-ins", 616, y + 18, size=13, muted=True, anchor="end")
            else:
                width = 300 * value / (maximum * 1000)
                lines.append(
                    f'<rect class="{style}" x="210" y="{y + 3}" '
                    f'width="{width:.3f}" height="19" rx="2"/>'
                )
                label(f"{value / 1000:.2f} s", 535, y + 18, size=15)
        axis_y = top + 204
        lines.append(
            f'<line class="rule" x1="210" y1="{axis_y - 15}" x2="510" y2="{axis_y - 15}"/>'
        )
        for tick in range(round(maximum / step) + 1):
            value = tick * step
            label(
                f"{value:g}",
                210 + 300 * value / maximum,
                axis_y,
                size=12,
                muted=True,
                anchor="middle",
            )
    label(
        "Median seconds · each project has its own zero-origin scale",
        24,
        height - 14,
        size=14,
        muted=True,
    )
    lines.extend(["</g>", "</svg>"])
    return "\n".join(lines) + "\n"


def render_readme_resources(rows: tuple[ResourceRow, ...]) -> str:
    """Pair elapsed-time bars with a separately scaled tree-PSS column."""

    if tuple(row.project for row in rows) != README_RESOURCE_PROJECTS:
        raise ValueError("README resource chart requires Packaging, Rich, and attrs")
    mib = 1_048_576
    row_height = 240
    height = 143 + len(rows) * row_height + 72
    details = "; ".join(
        f"{html.escape(row.project)} ({html.escape(row.scope)}, {row.cases:,} cases): "
        + ", ".join(
            f"{route} {value.time_median_ms / 1000:.3f} seconds and "
            f"{value.pss_median_bytes / mib:.2f} MiB"
            for route, value in zip(README_RESOURCE_ROUTES, row.routes, strict=True)
        )
        for row in rows
    )
    metadata = {
        "metric": "peak_tree_pss_bytes",
        "method": "linux-process-tree-smaps-rollup",
        "sample_interval_ms": 20,
        "projects": [
            {
                "project": row.project,
                "controller_revision": row.controller_revision,
                "source_revision": row.source_revision,
                "upstream_revision": row.upstream_revision,
                "wheel_sha256": row.wheel_sha256,
                "collector_sha256": row.collector_sha256,
                "runtime": dict(
                    zip(
                        ("python", "implementation", "pytest", "pytest_cov", "vfy"),
                        row.runtime,
                        strict=True,
                    )
                ),
            }
            for row in rows
        ],
    }
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="{height}" '
        f'viewBox="0 0 640 {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Test time and memory on real projects</title>',
        '<desc id="desc">Five complete command routes per project, each with two measured runs. '
        "Elapsed time is from the memory-monitored runs; sampling can add overhead. "
        "Time and memory use separate zero-origin scales within each project. "
        "Memory is peak sampled proportional set size across simultaneously live child "
        "and descendant processes, excluding the collector, at target 20 ms intervals. "
        "Sampled peaks can miss shorter spikes. Serial vfy and pytest-cov include coverage; "
        f"the other three routes do not. {details}.</desc>",
        '<metadata id="benchmark-provenance">'
        f"{html.escape(json.dumps(metadata, sort_keys=True, separators=(',', ':')), quote=False)}"
        "</metadata>",
        "<style>.surface{fill:#fafafa}.ink{fill:#262626}.muted{fill:#626262}"
        ".rule{stroke:#dedede}.reference{fill:#525252}.vfy{fill:#e85d04}"
        ".plain{fill:#a3a3a3}.memory{opacity:.72}"
        "@media(prefers-color-scheme:dark){.surface{fill:#202020}.ink{fill:#f5f5f5}"
        ".muted{fill:#d4d4d4}.rule{stroke:#525252}.reference{fill:#a3a3a3}"
        ".vfy{fill:#ff8736}.plain{fill:#737373}}</style>",
        '<rect class="surface" width="100%" height="100%" rx="8"/>',
        f'<g font-family="{FONT}">',
    ]

    def label(
        value: str,
        x: float,
        y: float,
        *,
        size: int = 14,
        bold: bool = False,
        muted: bool = False,
        anchor: str = "start",
    ) -> None:
        lines.append(
            f'<text class="{"muted" if muted else "ink"}" x="{x}" y="{y}" '
            f'font-size="{size}" font-weight="{700 if bold else 400}" '
            f'text-anchor="{anchor}">{html.escape(value)}</text>'
        )

    label("Test time and memory", 24, 34, size=19, bold=True)
    label("Complete commands · lower is better", 24, 58, muted=True)
    label(
        "Serial · coverage on except plain control    |    4 workers · no coverage",
        24,
        80,
        size=12,
        muted=True,
    )
    label("Time with sampling · s", 174, 105, size=13, muted=True)
    label("Peak memory · MiB", 435, 105, size=13, muted=True)
    for index, row in enumerate(rows):
        top = 143 + index * row_height
        label(row.project, 24, top, size=18, bold=True)
        label(f"{row.cases:,} cases", 616, top, size=13, muted=True, anchor="end")
        series = (
            ("pytest-cov", row.routes[2], "reference"),
            ("vfy coverage", row.routes[1], "vfy"),
            ("plain pytest", row.routes[0], "plain"),
            ("pytest-xdist", row.routes[3], "reference"),
            ("vfy native workers", row.routes[4], "vfy"),
        )
        _, time_max = _unit_scale(max(value.time_median_ms for _, value, _ in series) / 1000)
        _, pss_max = _unit_scale(max(value.pss_median_bytes for _, value, _ in series) / mib)
        for route_index, (name, value, style) in enumerate(series):
            y = top + 22 + route_index * 33
            label(name, 24, y + 15)
            time_width = 172 * value.time_median_ms / (time_max * 1000)
            pss_width = 75 * value.pss_median_bytes / (pss_max * mib)
            lines.append(
                f'<rect class="{style}" x="174" y="{y + 2}" '
                f'width="{time_width:.3f}" height="19" rx="2"/>'
            )
            lines.append(
                f'<rect class="{style} memory" x="435" y="{y + 6}" '
                f'width="{pss_width:.3f}" height="11" rx="2"/>'
            )
            label(f"{value.time_median_ms / 1000:.2f} s", 420, y + 16, anchor="end")
            label(f"{value.pss_median_bytes / mib:.0f} MiB", 616, y + 16, anchor="end")
        lines.append(f'<line class="rule" x1="24" y1="{top + 116}" x2="616" y2="{top + 116}"/>')
        axis_y = top + 213
        for left, right, maximum in (
            (174, 346, time_max),
            (435, 510, pss_max),
        ):
            lines.append(
                f'<line class="rule" x1="{left}" y1="{axis_y - 16}" '
                f'x2="{right}" y2="{axis_y - 16}"/>'
            )
            for tick in (0, maximum / 2, maximum):
                label(
                    f"{tick:g}",
                    left + (right - left) * tick / maximum,
                    axis_y,
                    size=11,
                    muted=True,
                    anchor="middle",
                )
        if index < len(rows) - 1:
            lines.append(
                f'<line class="rule" x1="24" y1="{top + row_height - 18}" '
                f'x2="616" y2="{top + row_height - 18}"/>'
            )
    label(
        "Peak memory: sampled tree PSS (shared pages apportioned)",
        24,
        height - 55,
        size=13,
        muted=True,
    )
    label(
        "Time includes memory sampling · scales reset per project",
        24,
        height - 37,
        size=13,
        muted=True,
    )
    label(
        "Historical exploratory medians · 2 runs per route · no warmups",
        24,
        height - 17,
        size=13,
        muted=True,
    )
    lines.extend(["</g>", "</svg>"])
    return "\n".join(lines) + "\n"


def render_memory_chart(rows: tuple[MemoryRow, ...]) -> str:
    """Show serial command peak RSS medians on a separate MiB scale per project."""

    if len(rows) < 2:
        raise ValueError("memory chart requires at least two qualified projects")
    mib = 1_048_576
    row_height = 184
    height = 112 + len(rows) * row_height + 43
    evidence = "; ".join(
        f"{html.escape(row.project)} ({html.escape(row.scope)}): "
        f"plain {row.plain.median_bytes / mib:.2f} MiB, "
        f"vfy {row.vfy.median_bytes / mib:.2f} MiB, "
        f"pytest-cov {row.pytest_cov.median_bytes / mib:.2f} MiB"
        for row in rows
    )
    provenance = [
        {
            "project": row.project,
            "upstream_revision": row.upstream_revision,
            "controller_revision": row.controller_revision,
            "wheel_sha256": row.wheel_sha256,
        }
        for row in rows
    ]
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="{height}" '
        f'viewBox="0 0 640 {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Serial test command memory on real projects</title>',
        '<desc id="desc">Median of two measured Linux ru_maxrss peaks per tool. '
        "Each peak is the largest individual process, including waited descendants; "
        "it is not aggregate concurrent process-tree memory. "
        "Plain pytest runs without coverage; vfy and pytest-cov run with coverage. "
        f"Per-project zero-origin MiB scales. {evidence}.</desc>",
        f'<metadata id="benchmark-provenance">'
        f"{html.escape(json.dumps(provenance), quote=False)}</metadata>",
        "<style>.surface{fill:#fafafa}.ink{fill:#262626}.muted{fill:#626262}"
        ".rule{stroke:#dedede}.reference{fill:#525252}.vfy{fill:#e85d04}"
        ".plain{fill:#a3a3a3}"
        "@media(prefers-color-scheme:dark){.surface{fill:#202020}.ink{fill:#f5f5f5}"
        ".muted{fill:#d4d4d4}.rule{stroke:#525252}.reference{fill:#a3a3a3}"
        ".vfy{fill:#ff8736}.plain{fill:#737373}}</style>",
        '<rect class="surface" width="100%" height="100%" rx="8"/>',
        f'<g font-family="{FONT}">',
    ]

    def label(
        value: str,
        x: float,
        y: float,
        *,
        size: int = 15,
        bold: bool = False,
        muted: bool = False,
        anchor: str = "start",
    ) -> None:
        lines.append(
            f'<text class="{"muted" if muted else "ink"}" x="{x}" y="{y}" '
            f'font-size="{size}" font-weight="{700 if bold else 400}" '
            f'text-anchor="{anchor}">{html.escape(value)}</text>'
        )

    label("Memory during serial test commands", 24, 34, size=18, bold=True)
    label("Peak RSS per run · median MiB · lower is better", 24, 60, size=14, muted=True)
    for project_index, row in enumerate(rows):
        top = 110 + project_index * row_height
        label(row.project, 24, top, size=18, bold=True)
        series = (
            ("plain pytest", row.plain.median_bytes, "plain"),
            ("vfy coverage", row.vfy.median_bytes, "vfy"),
            ("pytest-cov", row.pytest_cov.median_bytes, "reference"),
        )
        step, maximum = _unit_scale(max(value for _, value, _ in series) / mib)
        for index, (name, value, style) in enumerate(series):
            y = top + 18 + index * 32
            label(name, 24, y + 15)
            width = 300 * value / (maximum * mib)
            lines.append(
                f'<rect class="{style}" x="210" y="{y + 2}" '
                f'width="{width:.3f}" height="20" rx="2"/>'
            )
            label(f"{value / mib:.1f} MiB", 616, y + 17, anchor="end")
        axis_y = top + 139
        lines.append(
            f'<line class="rule" x1="210" y1="{axis_y - 15}" x2="510" y2="{axis_y - 15}"/>'
        )
        for tick in range(round(maximum / step) + 1):
            value = tick * step
            label(
                f"{value:g}",
                210 + 300 * value / maximum,
                axis_y,
                size=12,
                muted=True,
                anchor="middle",
            )
    label(
        "MiB · each project has its own zero-origin scale",
        24,
        height - 18,
        size=13,
        muted=True,
    )
    lines.extend(["</g>", "</svg>"])
    return "\n".join(lines) + "\n"


def select_readme_projects(
    coverage: ProjectChart,
    parallel: ProjectChart,
    stock_only: tuple[StockOnlyRow, ...],
    projects: tuple[str, ...] | None,
) -> tuple[ProjectChart, ProjectChart, tuple[StockOnlyRow, ...]]:
    """Select complete project groups without changing their source order."""

    coverage_names = {row.project for row in coverage.rows}
    if projects is None:
        return (
            coverage,
            replace(
                parallel, rows=tuple(row for row in parallel.rows if row.project in coverage_names)
            ),
            tuple(row for row in stock_only if row.project in coverage_names),
        )
    if len(projects) < 2:
        raise ValueError("README project selection requires at least two projects")
    if len(set(projects)) != len(projects):
        raise ValueError("README project selection contains duplicates")
    qualified = {row.project for row in parallel.rows} | {row.project for row in stock_only}
    unknown = set(projects) - qualified
    if unknown:
        raise ValueError(f"unknown qualified README project: {', '.join(sorted(unknown))}")
    incomplete = set(projects) - coverage_names
    if incomplete:
        raise ValueError(
            f"incomplete README group lacks matching coverage: {', '.join(sorted(incomplete))}"
        )
    selected = set(projects)
    return (
        replace(coverage, rows=tuple(row for row in coverage.rows if row.project in selected)),
        replace(parallel, rows=tuple(row for row in parallel.rows if row.project in selected)),
        tuple(row for row in stock_only if row.project in selected),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "docs/assets"
    )
    parser.add_argument(
        "--summary-json",
        type=Path,
        help="retained publication summary for real-project comparison charts",
    )
    parser.add_argument(
        "--memory-summary-json",
        type=Path,
        help="qualified serial peak RSS summary for the real-project memory chart",
    )
    parser.add_argument(
        "--readme-resources-json",
        type=Path,
        help="matched elapsed-time and whole-tree memory summary for one README chart",
    )
    parser.add_argument(
        "--readme-overview",
        type=Path,
        metavar="RESULTS",
        help="combine retained coverage and parallel comparisons for the README",
    )
    parser.add_argument(
        "--readme-project",
        action="append",
        metavar="PROJECT",
        help="include a qualified project in the README overview (repeat for each project)",
    )
    args = parser.parse_args()
    if args.readme_project and not args.readme_overview:
        parser.error("--readme-project requires --readme-overview")
    if args.memory_summary_json is not None and (
        args.summary_json is not None
        or args.readme_overview
        or args.readme_resources_json is not None
    ):
        parser.error("--memory-summary-json cannot be combined with other chart modes")
    if args.readme_resources_json is not None and (
        args.summary_json is not None or args.readme_overview
    ):
        parser.error("--readme-resources-json cannot be combined with other chart modes")
    if args.readme_overview:
        if args.summary_json is not None:
            parser.error("--readme-overview cannot be combined with --summary-json")
        root = args.readme_overview
        coverage = parse_project_charts(json.loads((root / "coverage/summary.json").read_text()))[0]
        parallel = parse_project_charts(
            json.loads((root / "parallel/chart-summary.json").read_text())
        )[0]
        stock_path = root / "parallel/stock-only-chart-summary.json"
        requested = tuple(args.readme_project) if args.readme_project else None
        stock_only = (
            parse_readme_stock_rows(
                json.loads(stock_path.read_text()),
                coverage,
                parallel,
                requested or tuple(row.project for row in coverage.rows),
            )
            if stock_path.is_file()
            else ()
        )
        try:
            coverage, parallel, stock_only = select_readme_projects(
                coverage, parallel, stock_only, requested
            )
        except ValueError as error:
            parser.error(str(error))
        rendered = {"readme-overview.svg": render_readme_overview(coverage, parallel, stock_only)}
    elif args.readme_resources_json is not None:
        rows = parse_readme_resources(
            json.loads(args.readme_resources_json.read_text(encoding="utf-8"))
        )
        rendered = {"readme-benchmarks.svg": render_readme_resources(rows)}
    elif args.memory_summary_json is not None:
        rows = parse_memory_summary(
            json.loads(args.memory_summary_json.read_text(encoding="utf-8"))
        )
        rendered = {"memory-projects.svg": render_memory_chart(rows)}
    elif args.summary_json is not None:
        payload = json.loads(args.summary_json.read_text(encoding="utf-8"))
        charts = parse_project_charts(payload)
        rendered = {f"{chart.kind}-projects.svg": render_project_chart(chart) for chart in charts}
    else:
        parser.error("select a chart mode and provide its result summary")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for filename, svg in rendered.items():
        (args.output_dir / filename).write_text(svg, encoding="utf-8")


if __name__ == "__main__":
    main()
