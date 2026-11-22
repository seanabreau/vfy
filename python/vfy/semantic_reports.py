"""Semantic decision reports backed by Rust observations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .data import (
    ConditionRange,
    CoverageStore,
)
from .errors import DataError, NoDataError
from .native import (
    SemanticReport,
    build_semantic_report as build_rust_report,
    render_semantic_json as render_rust_json,
    render_semantic_text as render_rust_text,
)


@dataclass(frozen=True)
class DecisionRow:
    path: str
    line: int
    expression: str
    kind: str
    decisions_covered: int
    decisions_total: int
    conditions_covered: int
    conditions_total: int
    mcdc_covered: int
    mcdc_total: int
    missing: tuple[str, ...]
    key: str = ""
    source_hash: str = ""
    logic: str = ""
    condition_lines: tuple[int, ...] = ()
    condition_ranges: tuple[ConditionRange, ...] = ()
    vectors: tuple[tuple[str, str, bool], ...] = ()
    outcomes: tuple[tuple[str, bool, tuple[str, ...]], ...] = ()


@dataclass(frozen=True)
class DecisionReport:
    rows: tuple[DecisionRow, ...]
    decisions_covered: int
    decisions_total: int
    conditions_covered: int
    conditions_total: int
    mcdc_covered: int
    mcdc_total: int
    _rust_report: SemanticReport = field(kw_only=True, repr=False, compare=False)

    @property
    def decision_percent(self) -> float:
        return _percent(self.decisions_covered, self.decisions_total)

    @property
    def condition_percent(self) -> float:
        return _percent(self.conditions_covered, self.conditions_total)

    @property
    def mcdc_percent(self) -> float:
        return _percent(self.mcdc_covered, self.mcdc_total)


def _percent(covered: int, total: int) -> float:
    return 100.0 if total == 0 else covered * 100.0 / total


def build_semantic_report(
    store: CoverageStore,
    *,
    include: Sequence[str] | None = None,
    omit: Sequence[str] | None = None,
    contexts: Sequence[str] | None = None,
    source_roots: Sequence[str] | None = None,
    exclude_decisions: Sequence[str] | None = None,
) -> DecisionReport:
    """Adapt one Rust semantic report into the public Python model."""
    rust_data = store.semantic_rust_data or store.to_rust()
    try:
        report = build_rust_report(
            rust_data,
            include or [],
            omit or [],
            contexts or [],
            source_roots or [],
            exclude_decisions or [],
        )
    except ValueError as exc:
        message = str(exc)
        if message.startswith("no semantic decision data found"):
            raise NoDataError(message) from exc
        if message.startswith("semantic data for ") and " is stale;" in message:
            raise DataError(message) from exc
        raise

    rows = tuple(
        DecisionRow(
            row.path,
            row.line,
            row.expression,
            row.kind,
            row.decisions_covered,
            row.decisions_total,
            row.conditions_covered,
            row.conditions_total,
            row.mcdc_covered,
            row.mcdc_total,
            tuple(row.missing),
            key=row.key,
            source_hash=row.source_hash,
            logic=row.logic,
            condition_lines=tuple(row.condition_lines),
            condition_ranges=tuple(
                ConditionRange(index, *values) for index, values in enumerate(row.condition_ranges)
            ),
            vectors=tuple(row.vectors),
            outcomes=tuple(
                (label, covered, tuple(contexts)) for label, covered, contexts in row.outcomes
            ),
        )
        for row in report.rows
    )
    return DecisionReport(
        rows,
        report.decisions_covered,
        report.decisions_total,
        report.conditions_covered,
        report.conditions_total,
        report.mcdc_covered,
        report.mcdc_total,
        _rust_report=report,
    )


def render_semantic_text(
    report: DecisionReport,
    precision: int = 1,
    *,
    history_path: str | None = None,
) -> str:
    return render_rust_text(report._rust_report, precision, history_path)


def semantic_json(report: DecisionReport, *, pretty: bool = False) -> str:
    return render_rust_json(report._rust_report, pretty)
