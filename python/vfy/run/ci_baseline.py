"""Publish and export CI baselines and import-graph evidence."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from vfy.errors import VfyError
from vfy.native import (
    audit_ci_plan,
    export_ci_baseline,
    fallback_ci_plan,
    file_digest,
    green_git_head,
    import_ci_baseline,
    prepare_test_import_graph,
    publish_ci_plan,
    publish_test_import_graph,
    read_ci_plan,
    source_tree_identity,
    validate_ci_artifact_paths,
    write_green_test_run,
)

from .record import (
    execution_fingerprint,
    green_test_run_path,
    native_execution_fingerprint,
)

if TYPE_CHECKING:
    from vfy.config_types import Config
    from vfy.data import CoverageStore


from .engine import TestEngine
from .plan import SelectionPlan
from .profiling import _profile_begin, _profile_end
from .replay import (
    _REPLAY_COMMIT_KEY,
    _replay_evidence,
    _ReplayEvidence,
    _TestInvocation,
    _write_green_baseline,
)
from .reports import _controller_message, _pytest_option
from .selection import _select_test_plan
from .watch_scope import _WATCH_INPUT_IDENTITY


def _publish_import_graph(
    invocation: _TestInvocation,
    staged: Path | None,
    reasons: list[str],
    source_identity: str,
) -> list[str]:
    if staged is None:
        return prepare_test_import_graph(
            invocation.history_path, invocation.graph_roots, source_identity
        )
    try:
        publish_test_import_graph(str(staged), invocation.history_path)
    except (OSError, RuntimeError, ValueError) as exc:
        return [*reasons, str(exc)]
    return reasons


def _read_test_coverage(path: Path) -> CoverageStore | None:
    if not path.is_file():
        return None
    from vfy.data import CoverageStore  # noqa: PLC0415 - absent coverage needs no data adapter

    started = _profile_begin()
    fresh = CoverageStore.read(path)
    _profile_end("persistence", started)
    return fresh


def _publish_test_evidence(
    args: argparse.Namespace,
    invocation: _TestInvocation,
    evidence: _ReplayEvidence,
    plan: SelectionPlan,
    *,
    completed: int,
    fresh: CoverageStore | None,
    incremental: bool,
    replayed: bool,
    pytest_args: list[str],
    staged_import_graph: Path | None,
    graph_reasons: list[str],
    collected: Sequence[str] | None = None,
) -> int:
    baseline = evidence.baseline
    cache_path = invocation.cache_path
    selected = list(plan.selected)
    if fresh is None:
        return int(completed)
    if incremental:
        if replayed:
            assert baseline is not None
            fresh.write(args.data_file)
            reused = max(0, len(plan.all_tests) - len(selected))
            baseline_commit = baseline.metadata.get(_REPLAY_COMMIT_KEY, evidence.state.head)
            # In-process pytest can leave its final progress and summary buffered.
            sys.stdout.flush()
            _controller_message(
                args,
                f"Reused {reused} tests; complete coverage reconstructed from "
                f"passing baseline {baseline_commit[:8]}",
            )
        elif completed == 0:
            raise VfyError("the selected run did not reconstruct complete coverage")
        else:
            fresh.write(args.data_file)
            _controller_message(args, "Passing baseline preserved because the selected run failed")
    elif (
        completed == 0
        and evidence.state.submodule_change is None
        and _can_publish_green_baseline(args, invocation)
    ):
        identity_validation_started = _profile_begin()
        try:
            current_head = (
                green_git_head(evidence.ignored_outputs) if evidence.git_available else None
            )
        except RuntimeError:
            current_head = None
        if evidence.git_available and current_head is None:
            _profile_end("source_identity_validation", identity_validation_started)
            return int(completed)
        if evidence.source_identity.startswith("git:"):
            identity_current = bool(
                current_head and evidence.source_identity == f"git:{current_head}"
            )
        else:
            try:
                current_identity = "content:" + source_tree_identity(
                    list(evidence.identity_roots), list(evidence.identity_ignored)
                )
            except RuntimeError:
                current_identity = ""
            identity_current = bool(
                evidence.source_identity and evidence.source_identity == current_identity
            )
        _profile_end("source_identity_validation", identity_validation_started)
        if identity_current:
            if evidence.git_available:
                import_graph_started = _profile_begin()
                graph_reasons = _publish_import_graph(
                    invocation,
                    staged_import_graph,
                    graph_reasons,
                    evidence.source_identity,
                )
                _profile_end("import_graph", import_graph_started)
                if graph_reasons:
                    _controller_message(
                        args,
                        "Failed to prepare the complete import graph; selection falls back safely",
                    )
            watch_identity = getattr(args, "_watch_input_identity", None)
            if watch_identity is not None:
                fresh.metadata[_WATCH_INPUT_IDENTITY] = watch_identity
            replay_head = current_head
            green_persistence_started = _profile_begin()
            _write_green_baseline(
                fresh,
                cache_path,
                head=replay_head,
                fingerprint=evidence.fingerprint if replay_head is not None else None,
                green_fingerprint=evidence.green_fingerprint,
                history_digest=file_digest(invocation.history_path),
                source_identity=evidence.source_identity,
                source_roots=list(evidence.identity_roots),
                ignored_paths=list(evidence.identity_ignored),
                collected=collected,
            )
            write_green_test_run(str(green_test_run_path(invocation.history_path)), str(cache_path))
            _profile_end("green_persistence", green_persistence_started)
            if replay_head is not None:
                message = (
                    f"Prepared passing baseline {replay_head[:8]} for publication"
                    if getattr(args, "watch", False)
                    else f"Saved passing baseline {replay_head[:8]} at {cache_path}"
                )
                _controller_message(args, message)
    return int(completed)


def _can_publish_green_baseline(args: argparse.Namespace, invocation: _TestInvocation) -> bool:
    return bool(
        invocation.configuration.coverage_enabled
        and (
            invocation.replay_enabled
            or (
                (
                    args.mode == "full"
                    or getattr(args, "_test_engine", None) == TestEngine.INDEPENDENT
                )
                and not invocation.configuration.projection["filtered_scope"]
                and "--cov-append" not in invocation.configuration.effective_args
            )
        )
    )


def _prepare_ci_evidence(
    args: argparse.Namespace, config: Config, invocation: _TestInvocation, request_path: Path
) -> tuple[_ReplayEvidence, SelectionPlan, str]:
    try:
        validate_ci_artifact_paths(
            args.data_file, args.history, args.baseline, args.export_baseline, args.plan_output
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise VfyError(f"invalid CI artifact destinations: {exc}") from exc
    configured_arguments = list(invocation.configuration.effective_args)
    if (
        args._test_engine == TestEngine.INDEPENDENT
        and invocation.configuration.coverage_enabled
        and "--cov-append" in configured_arguments
    ):
        raise VfyError("CI selection does not support --cov-append; remove --cov-append")
    if args.export_baseline and (
        not invocation.replay_enabled
        or "--no-cov" in configured_arguments
        or "--cov-append" in configured_arguments
    ):
        raise VfyError(
            "--export-baseline requires fresh coverage with replay enabled; "
            "remove --no-cov, --cov-append, and --no-replay"
        )
    if _pytest_option(configured_arguments, "--tx") is not None:
        raise VfyError("CI selection supports local pytest-xdist workers; remove --tx")
    evidence = _replay_evidence(args, config, invocation)
    ignored = evidence.ignored_outputs
    portable_fingerprint = ""
    if args.baseline or args.export_baseline:
        try:
            portable_fingerprint = execution_fingerprint(
                config,
                invocation.source_roots,
                invocation.controller.resolve_pytest_args(
                    invocation.pytest_args,
                    route=TestEngine(args._test_engine),
                    configuration=invocation.configuration,
                ),
                identity="portable",
                dependency_fingerprint=evidence.dependency_fingerprint,
                engine=args._test_engine,
                effective_pytest_args=invocation.configuration.effective_args,
                pytest_configuration=invocation.configuration.projection,
            )
            portable_fingerprint = native_execution_fingerprint(
                portable_fingerprint,
                invocation.native_scheduling,
                worker_collection=invocation.worker_collection,
                worker_start=invocation.worker_start,
                startup_grace=invocation.startup_grace,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise VfyError(f"failed to identify CI baseline inputs: {exc}") from exc
    if args.baseline:
        from vfy.data import CoverageStore  # noqa: PLC0415 - only CI baseline import needs coverage

        started = _profile_begin()
        try:
            imported = import_ci_baseline(
                args.baseline, portable_fingerprint, ignored, str(request_path)
            )
            baseline = CoverageStore(rust_data=imported) if imported is not None else None
            plan = SelectionPlan.from_rust(read_ci_plan(str(request_path)))
        except (OSError, RuntimeError, ValueError) as exc:
            raise VfyError(f"failed to load CI baseline {args.baseline!r}: {exc}") from exc
        _profile_end("baseline_load", started)
        evidence = replace(evidence, baseline=baseline)
    else:
        baseline = evidence.baseline
        plan = _select_test_plan(
            args, invocation, evidence, force_full=False, request_path=request_path
        )
        if baseline is None or plan.fallback_full or args.fresh:
            fallback_ci_plan(str(request_path), evidence.state.head, "; ".join(plan.reasons))
    return evidence, plan, portable_fingerprint


def _publish_ci_diagnostics(
    args: argparse.Namespace,
    final_path: Path,
    result_path: Path,
    completed: int,
    *,
    finalized: bool = True,
) -> None:
    if args.audit_selection:
        output_path = args.plan_output or str(final_path)
        audit = audit_ci_plan(str(final_path), str(result_path), completed, output_path)
        _controller_message(
            args, "Selection audit " + ("completed" if audit["complete"] else "is incomplete")
        )
        for test in audit["omitted_failures"]:
            _controller_message(args, f"Selection would have omitted failing test: {test}")
    elif args.plan_output and finalized:
        publish_ci_plan(str(final_path), args.plan_output)


def _export_ci_baseline(
    args: argparse.Namespace,
    invocation: _TestInvocation,
    evidence: _ReplayEvidence,
    *,
    fresh: CoverageStore | None,
    collection_path: Path,
    fingerprint: str,
) -> None:
    if fresh is None:
        raise VfyError(
            "--export-baseline requires fresh coverage; pytest produced no coverage data"
        )
    try:
        export_ci_baseline(
            args.export_baseline,
            str(invocation.cache_path),
            invocation.history_path,
            str(collection_path),
            fingerprint,
            invocation.source_roots,
            evidence.ignored_outputs,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise VfyError(f"failed to export CI baseline: {exc}") from exc
