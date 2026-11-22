"""Run `vfy test` once in CI baseline, plan-only, or audit modes."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vfy.errors import VfyError
from vfy.native import (
    ci_plan_collection_scoped,
    ci_plan_source_current,
    prepare_test_import_graph,
    publish_ci_plan,
    read_ci_plan,
)

from .execution import (
    _CONTROLLER_ENVIRONMENT,
    _configure_native_bridge_environment,
    _record_execution_evidence,
)

if TYPE_CHECKING:
    from vfy.config_types import Config


from .arguments import _selected_pytest_arguments
from .ci_baseline import (
    _export_ci_baseline,
    _prepare_ci_evidence,
    _publish_ci_diagnostics,
    _publish_test_evidence,
    _read_test_coverage,
)
from .engine import TestEngine, _select_test_engine
from .plan import SelectionPlan, _selection_message
from .profiling import _profile_begin, _profile_end
from .replay import (
    _capture_green_source_identity,
    _prepare_test_invocation,
    _replay_complete_path,
    _selected_run_reconstructed,
    _stage_replay_merge,
    _TestInvocation,
    _unsaved_selection_status,
)
from .reports import _controller_message


def _run_pytest(
    args: argparse.Namespace,
    invocation: _TestInvocation,
    pytest_args: list[str],
    environment: dict[str, str],
    stage: Path,
) -> int:
    """Run pytest once, or twice when a scoped collection disagrees with its baseline.

    A scoped run that collects different tests than the baseline removes its own
    scope before running or publishing anything, so the repeat collects every file.
    """
    request_path = stage / "request.json"
    scoped = ci_plan_collection_scoped(str(request_path))
    completed = invocation.controller.run(
        pytest_args,
        environment,
        stdout="stderr" if args.plan_only and not args.plan_output else "inherit",
    )
    if (
        not scoped
        or (stage / "plan.json").is_file()
        or ci_plan_collection_scoped(str(request_path))
    ):
        return completed
    _controller_message(args, "Collecting the complete suite to bind the selection")
    for name in ("results.json", "collection.json", ".coverage"):
        (stage / name).unlink(missing_ok=True)
    _replay_complete_path(stage).unlink(missing_ok=True)
    return invocation.controller.run(pytest_args, environment, stdout="inherit")


def _run_ci_test_once(
    args: argparse.Namespace,
    config: Config,
    *,
    invocation: _TestInvocation | None = None,
) -> int:
    explicit_ci = invocation is None
    if invocation is None:
        invocation = _prepare_test_invocation(args, force_full=False)
    _select_test_engine(args, invocation.pytest_args, invocation.configuration)
    with tempfile.TemporaryDirectory(prefix="vfy-ci-") as temporary:
        stage = Path(temporary)
        request_path = stage / "request.json"
        final_path = stage / "plan.json"
        collection_path = stage / "collection.json"
        run_data = stage / ".coverage"
        result_path = stage / "results.json"
        evidence, plan, portable_fingerprint = _prepare_ci_evidence(
            args, config, invocation, request_path
        )
        baseline = evidence.baseline
        ignored = evidence.ignored_outputs
        # Bind collection before applying the subset.
        pytest_args = _selected_pytest_arguments(
            args, invocation, plan, collection_scope="complete"
        )
        environment = os.environ.copy()
        _configure_native_bridge_environment(
            environment,
            TestEngine(args._test_engine),
            invocation.controller.workers,
            invocation.native_scheduling,
        )
        # Variables from an enclosing vfy run must not reach this run's child.
        for name in _CONTROLLER_ENVIRONMENT:
            environment.pop(name, None)
        environment.update(
            {
                "COVERAGE_FILE": str(run_data),
                "VFY_TEST_RESULTS": str(result_path),
                "VFY_CI_REQUEST": str(request_path),
                "VFY_CI_FINAL": str(final_path),
                "VFY_CI_COLLECTION": str(collection_path),
                "VFY_CI_IGNORED": json.dumps(ignored),
                "VFY_FORCE_PYTEST_SUBPROCESS": "1",
            }
        )
        if args.audit_selection:
            environment["VFY_CI_AUDIT"] = "1"
        elif (
            baseline is not None
            and not plan.fallback_full
            and not args.fresh
            and not args.plan_only
        ):
            environment["VFY_REPLAY_MERGE"] = _stage_replay_merge(stage, baseline)
        if args.plan_only:
            pytest_args.extend(("--collect-only", "--no-cov"))
            environment.pop("VFY_TEST_HISTORY", None)
        else:
            environment["VFY_TEST_HISTORY"] = invocation.history_path
            evidence = _capture_green_source_identity(args, config, invocation, evidence)
        started = _profile_begin()
        completed = _run_pytest(args, invocation, pytest_args, environment, stage)
        _profile_end("pytest", started)
        if not final_path.is_file():
            _publish_ci_diagnostics(args, request_path, result_path, completed, finalized=False)
            return int(completed)
        plan = SelectionPlan.from_rust(read_ci_plan(str(final_path)))
        _controller_message(args, _selection_message(plan, collected=True))
        if completed == 5 and plan.all_tests and not plan.selected and not args.audit_selection:
            completed = 0
        if args.plan_only:
            # Plan-only must not publish coverage or history.
            if args.plan_output:
                publish_ci_plan(str(final_path), args.plan_output)
            else:
                print(final_path.read_text(encoding="utf-8"))
            return int(completed)
        if (explicit_ci or not plan.fallback_full) and not ci_plan_source_current(
            str(request_path), ignored
        ):
            raise VfyError(
                "source changed during the CI run; evidence was discarded; rerun the command"
            )
        _publish_ci_diagnostics(args, final_path, result_path, completed)
        evidence_complete = _record_execution_evidence(
            args,
            invocation,
            completed=completed,
            primary_result_path=result_path,
            temporary_path=stage,
            environment=environment,
            pytest_args=pytest_args,
            obsolete_check=None,
        )
        incremental = baseline is not None and not plan.fallback_full and not args.audit_selection
        publish_full_coverage = (
            args._test_engine == TestEngine.INDEPENDENT
            and plan.fallback_full
            and completed in {0, 1}
        )
        complete_path = _replay_complete_path(stage)
        replayed = _selected_run_reconstructed(incremental, completed, run_data, complete_path)
        fresh = (
            _read_test_coverage(complete_path if replayed else run_data)
            if evidence_complete or publish_full_coverage
            else None
        )
        if fresh is not None and not incremental:
            fresh.write(args.data_file)
        if not evidence_complete:
            # An empty selection runs no test and leaves no result to record.
            return _unsaved_selection_status(args) if replayed and plan.selected else int(completed)
        staged_graph: Path | None = None
        graph_reasons: list[str] = []
        if evidence_complete and completed == 0 and not incremental and evidence.git_available:
            staged_graph = stage / "import-graph.json"
            graph_reasons = prepare_test_import_graph(
                str(stage / "history.json"), invocation.graph_roots, evidence.source_identity
            )
        completed = _publish_test_evidence(
            args,
            invocation,
            evidence,
            plan,
            completed=completed,
            fresh=fresh,
            incremental=incremental,
            replayed=replayed,
            pytest_args=pytest_args,
            staged_import_graph=staged_graph,
            graph_reasons=graph_reasons,
            collected=plan.all_tests,
        )
        if evidence_complete and args.export_baseline and completed == 0:
            _export_ci_baseline(
                args,
                invocation,
                evidence,
                fresh=fresh,
                collection_path=collection_path,
                fingerprint=portable_fingerprint,
            )
        return int(completed)
