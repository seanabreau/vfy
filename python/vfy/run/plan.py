"""Selection plan data and its user-facing description."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vfy.native_types import (
        GitReplayRecord,
        SelectionRecord,
        TestMode,
    )


@dataclass(frozen=True)
class GitReplayState:
    """Immutable Git state returned by the Rust selector."""

    head: str
    clean: bool
    changed: tuple[str, ...]
    untracked: bool
    deleted: bool
    patch: str

    @classmethod
    def from_rust(cls, value: GitReplayRecord) -> GitReplayState:
        return cls(
            value["head"],
            value["clean"],
            tuple(value["changed"]),
            value["untracked"],
            value["deleted"],
            value["patch"],
        )


@dataclass(frozen=True)
class SelectionPlan:
    """Immutable execution plan returned by Rust test selection."""

    mode: TestMode
    selected: tuple[str, ...]
    all_tests: tuple[str, ...]
    durations: Mapping[str, float]
    affected_files: tuple[str, ...]
    affected_functions: int
    affected_branches: int
    selected_seconds: float | None
    full_seconds: float | None
    fallback_full: bool
    reasons: tuple[str, ...]
    location: str | None
    scoped_files: Mapping[str, tuple[tuple[int, int, int], ...]] = MappingProxyType({})

    @classmethod
    def from_rust(cls, value: SelectionRecord) -> SelectionPlan:
        return cls(
            value["mode"],
            tuple(value["selected"]),
            tuple(value["all_tests"]),
            MappingProxyType(dict(value["durations"])),
            tuple(value["affected_files"]),
            value["affected_functions"],
            value["affected_branches"],
            value["selected_seconds"],
            value["full_seconds"],
            value["fallback_full"],
            tuple(value["reasons"]),
            value["location"],
            MappingProxyType(
                {path: tuple(shifts) for path, shifts in value["scoped_files"].items()}
            ),
        )


_SKIPPED_TESTS_NOTICE = "Tests outside the selection were skipped; `vfy test --all` runs everything"


def _selection_message(plan: SelectionPlan, *, collected: bool = False) -> str:
    selected = list(plan.selected)
    all_tests = list(plan.all_tests)
    lines = []
    affected = plan.affected_files
    if affected:
        lines.append("Changed code: " + ", ".join(affected))
        lines.append(
            f"Affected code: {plan.affected_functions} functions, {plan.affected_branches} branches"
        )
    if plan.location:
        lines.append(f"Tests reaching {plan.location}: {len(all_tests)}")
    elif plan.fallback_full:
        lines.append("Running the full suite")
        if all_tests:
            lines.append(f"Known tests before collection: {len(all_tests)}")
        else:
            lines.append("Known tests before collection: unknown")
    else:
        inventory = (
            "after collection" if collected else "from the saved baseline; collection follows"
        )
        lines.append(f"Selected {len(selected)} / {len(all_tests)} tests ({inventory})")
        if len(selected) < len(all_tests):
            lines.append(_SKIPPED_TESTS_NOTICE)
    selected_seconds = plan.selected_seconds
    full_seconds = plan.full_seconds
    if selected_seconds is not None:
        lines.append(f"Estimated selected suite: {selected_seconds:.2f}s")
    if full_seconds is not None:
        lines.append(f"Estimated full suite: {full_seconds:.2f}s")
    lines.extend(f"Note: {reason}" for reason in plan.reasons)
    return "\n".join(lines)


def _complete_run_reason(*, force_full: bool) -> str:
    if force_full:
        return "watch recovery requires a full run"
    return "the complete suite was requested"


def _full_test_plan(
    reason: str, *, location: str | None = None, mode: TestMode = "safe"
) -> SelectionPlan:
    return SelectionPlan(
        mode=mode,
        selected=(),
        all_tests=(),
        durations=MappingProxyType({}),
        affected_files=(),
        affected_functions=0,
        affected_branches=0,
        selected_seconds=None,
        full_seconds=None,
        fallback_full=True,
        reasons=(reason,),
        location=location,
    )
