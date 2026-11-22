"""Plan the selected tests from the saved baseline and Git state."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from vfy.errors import NoDataError
from vfy.independent.configuration import INDEPENDENT_FULL_REASON
from vfy.native import (
    plan_test_selection,
)
from vfy.store_loader import _load_store

from .engine import TestEngine
from .plan import SelectionPlan, _complete_run_reason, _full_test_plan
from .profiling import _profile_begin, _profile_end
from .replay import _REPLAY_COMMIT_KEY, _ReplayEvidence, _TestInvocation


def _select_test_plan(
    args: argparse.Namespace,
    invocation: _TestInvocation,
    evidence: _ReplayEvidence | None,
    *,
    force_full: bool,
    request_path: Path | None = None,
) -> SelectionPlan:
    if getattr(args, "_test_engine", None) == TestEngine.INDEPENDENT and request_path is None:
        return _full_test_plan(
            INDEPENDENT_FULL_REASON,
            mode=args.mode,
        )
    if evidence is None:
        if not invocation.configuration.coverage_enabled:
            reason = "coverage is disabled; running the requested tests"
        elif args.mode == "full":
            reason = _complete_run_reason(force_full=force_full)
        else:
            reason = "pytest selection options require running the complete requested scope"
        return _full_test_plan(reason, mode=args.mode)
    replay_enabled = invocation.replay_enabled
    history_path = invocation.history_path
    source_roots = invocation.graph_roots
    location = invocation.location
    baseline = evidence.baseline
    state = evidence.state
    git_available = evidence.git_available
    if replay_enabled and git_available and baseline is not None:
        selection_started = _profile_begin()
        plan = SelectionPlan.from_rust(
            plan_test_selection(
                baseline.to_rust(),
                history_path,
                source_roots,
                state.patch,
                "safe",
                None,
                request_path=str(request_path) if request_path is not None else None,
                baseline_commit=baseline.metadata.get(_REPLAY_COMMIT_KEY),
                target_commit=state.head,
            )
        )
        reasons = list(plan.reasons)
        fallback_full = plan.fallback_full
        if state.untracked:
            fallback_full = True
            reasons.append("untracked files can change test discovery or runtime inputs")
        if state.deleted:
            fallback_full = True
            reasons.append("deleted or renamed files require a new full baseline")
        planned = {str(Path(path).resolve()) for path in plan.affected_files}
        changed = {str(Path(path).resolve()) for path in state.changed}
        if not changed <= planned:
            fallback_full = True
            reasons.append("Git status contains changes the source diff could not map safely")
        plan = replace(plan, fallback_full=fallback_full, reasons=tuple(dict.fromkeys(reasons)))
        _profile_end("selection", selection_started)
    elif getattr(args, "watch", False) and args.no_replay:
        plan = _full_test_plan(
            "watch selection is disabled without replay evidence; running the full suite"
        )
    elif args.mode == "full":
        plan = _full_test_plan(_complete_run_reason(force_full=force_full), mode="full")
    elif not git_available:
        plan = _full_test_plan("Git state is unavailable; running the full suite")
    elif replay_enabled:
        plan = _full_test_plan(evidence.baseline_reason or "a fresh baseline was requested")
    else:
        try:
            store = _load_store(args.data_file)
            plan = SelectionPlan.from_rust(
                plan_test_selection(
                    store.to_rust(), history_path, source_roots, None, args.mode, location
                )
            )
        except NoDataError:
            plan = _full_test_plan(
                "no prior context data is available; running a baseline suite", location=location
            )
    return plan
