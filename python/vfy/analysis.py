"""Compatibility adapter for the Rust-backed Ruff source analyzer."""

from __future__ import annotations

from dataclasses import dataclass, field

from .native import analyze_file

DEFAULT_EXCLUDES = [
    r"#\s*(pragma|PRAGMA)[:\s]?\s*(no|NO)\s*(cover|COVER)",
    r"^\s*(((async )?def .*?)?[\])]+(\s*->.*?)?:\s*)?\.\.\.\s*(#|$)",
    r"if (typing\.)?TYPE_CHECKING:",
]
DEFAULT_PARTIALS = [r"#\s*(pragma|PRAGMA)[:\s]?\s*(no|NO)\s*(branch|BRANCH)"]


@dataclass
class FileAnalysis:
    path: str
    executable: set[int] = field(default_factory=set)
    excluded: set[int] = field(default_factory=set)
    possible_arcs: set[tuple[int, int]] = field(default_factory=set)
    no_branch: set[int] = field(default_factory=set)
    source_missing: bool = False
    source_error_kind: str | None = None
    source_error_detail: str | None = None


def analyze_source(
    path: str,
    *,
    excludes: list[str] | None = None,
    partials: list[str] | None = None,
    collect_arcs: bool = True,
) -> FileAnalysis:
    rust_analysis = analyze_file(
        path,
        DEFAULT_EXCLUDES if excludes is None else excludes,
        collect_arcs,
        DEFAULT_PARTIALS if partials is None else partials,
    )
    return FileAnalysis(
        path=path,
        executable=set(rust_analysis.executable),
        excluded=set(rust_analysis.excluded),
        possible_arcs={tuple(arc) for arc in rust_analysis.possible_arcs},
        no_branch=set(rust_analysis.no_branch),
        source_missing=rust_analysis.source_missing,
        source_error_kind=rust_analysis.source_error_kind,
        source_error_detail=rust_analysis.source_error_detail,
    )
