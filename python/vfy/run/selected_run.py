"""Run `vfy test` once against a saved selection."""

from __future__ import annotations

import argparse
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from vfy.independent.configuration import PytestConfiguration as _PytestConfiguration
from vfy.native import (
    plan_test_selection,
    prepare_test_import_graph,
)

from .execution import (
    prepare_execution,
)

if TYPE_CHECKING:
    from vfy.config_types import Config
    from vfy.invocation_preparation import PreparedInvocation


from .arguments import _selected_pytest_arguments
from .ci_baseline import _can_publish_green_baseline, _publish_test_evidence, _read_test_coverage
from .ci_run import _run_ci_test_once
from .engine import TestEngine, _select_test_engine
from .plan import _SKIPPED_TESTS_NOTICE, SelectionPlan
from .profiling import _profile_begin, _profile_end
from .replay import (
    _capture_green_source_identity,
    _prepare_test_invocation,
    _replay_complete_path,
    _replay_evidence,
    _selected_run_reconstructed,
    _stage_replay_merge,
)
from .reports import _controller_message, _pytest_option, _write_coverage_reports
from .selection import _select_test_plan


def _run_test_selection_once(
    args: argparse.Namespace,
    config: Config,
    *,
    force_full: bool = False,
    obsolete_check: Callable[[], bool] | None = None,
    configuration: _PytestConfiguration | None = None,
    prepared: PreparedInvocation | None = None,
) -> int:
    invocation = _prepare_test_invocation(
        args, force_full=force_full, configuration=configuration, prepared=prepared
    )
    _select_test_engine(args, invocation.pytest_args, invocation.configuration)
    if (
        args._test_engine == TestEngine.INDEPENDENT
        and args.mode == "safe"
        and invocation.configuration.coverage_enabled
        and "--cov-append" not in invocation.configuration.effective_args
        and not invocation.configuration.projection["filtered_scope"]
        and invocation.location is None
        and not getattr(args, "watch", False)
    ):
        return _run_ci_test_once(args, config, invocation=invocation)
    can_publish_baseline = _can_publish_green_baseline(args, invocation)
    requested_scope = (
        args.mode == "safe"
        and invocation.location is None
        and not getattr(args, "watch", False)
        and invocation.configuration.projection["filtered_scope"]
    )
    needs_evidence = (
        invocation.replay_enabled
        or can_publish_baseline
        or (
            invocation.configuration.coverage_enabled
            and args.mode != "full"
            and not requested_scope
        )
        or invocation.location is not None
        or args.mode in {"balanced", "aggressive"}
        or getattr(args, "watch", False)
    )
    evidence = (
        _replay_evidence(
            args,
            config,
            invocation,
            purpose=(
                "selection"
                if args.mode in {"full", "safe"}
                and invocation.location is None
                and not getattr(args, "watch", False)
                and not invocation.replay_enabled
                and not can_publish_baseline
                else "replay"
            ),
        )
        if needs_evidence
        else None
    )
    baseline = evidence.baseline if evidence is not None else None
    replay_enabled = invocation.replay_enabled
    source_roots = invocation.source_roots
    history_path = invocation.history_path
    pytest_args = invocation.pytest_args
    if (
        replay_enabled
        and evidence is not None
        and evidence.git_available
        and baseline is not None
        and evidence.state.clean
        and not getattr(args, "watch", False)
    ):
        baseline.write(args.data_file)
        known = SelectionPlan.from_rust(
            plan_test_selection(
                baseline.to_rust(), history_path, invocation.graph_roots, "", "safe", None
            )
        )
        count = len(known.all_tests)
        _controller_message(
            args, f"Reused {count} tests from passing baseline {evidence.state.head[:8]}"
        )
        _controller_message(
            args, "No code changed; complete coverage restored without running pytest"
        )
        _controller_message(args, _SKIPPED_TESTS_NOTICE)
        if _pytest_option(list(invocation.configuration.effective_args), "--html"):
            _controller_message(
                args, "Existing HTML report preserved; no new test report was generated"
            )
        reporting_started = _profile_begin()
        reports_passed = _write_coverage_reports(
            args, config, list(invocation.configuration.effective_args), source_roots, baseline
        )
        _profile_end("reporting", reporting_started)
        return 0 if reports_passed else 1

    plan = _select_test_plan(args, invocation, evidence, force_full=force_full)
    pytest_args = _selected_pytest_arguments(args, invocation, plan)
    incremental = replay_enabled and baseline is not None and not plan.fallback_full
    if evidence is not None and not incremental and _can_publish_green_baseline(args, invocation):
        evidence = _capture_green_source_identity(args, config, invocation, evidence)
    with prepare_execution(
        args,
        invocation,
        pytest_args,
        incremental=incremental,
        selected=None if plan.fallback_full else plan.selected,
    ) as execution:
        temporary_path = execution.temporary_path
        run_data = execution.run_data
        graph_result: list[list[str] | Exception] = []
        graph_thread: threading.Thread | None = None
        staged_import_graph: Path | None = None
        if (
            not incremental
            and evidence is not None
            and evidence.git_available
            and _can_publish_green_baseline(args, invocation)
        ):
            staged_import_graph = temporary_path / "import-graph.json"

            def prepare_import_graph() -> None:
                try:
                    graph_result.append(
                        prepare_test_import_graph(
                            str(temporary_path / "history.json"),
                            invocation.graph_roots,
                            evidence.source_identity,
                        )
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    graph_result.append(exc)

            graph_thread = threading.Thread(target=prepare_import_graph, daemon=True)
            graph_thread.start()
        if incremental:
            assert baseline is not None
            execution.environment["VFY_REPLAY_MERGE"] = _stage_replay_merge(
                temporary_path, baseline, plan
            )
        pytest_started = _profile_begin()
        completed = execution.run()
        _profile_end("pytest", pytest_started)
        if graph_thread is not None:
            graph_thread.join()
        graph_reasons: list[str] = []
        if graph_result:
            graph_result_value = graph_result[0]
            if isinstance(graph_result_value, Exception):
                graph_reasons.append(str(graph_result_value))
                staged_import_graph = None
            else:
                graph_reasons.extend(graph_result_value)
        evidence_complete = execution.record(completed, obsolete_check=obsolete_check)
        complete_path = _replay_complete_path(temporary_path)
        replayed = _selected_run_reconstructed(incremental, completed, run_data, complete_path)
        if (
            not evidence_complete
            or not invocation.configuration.coverage_enabled
            or (not incremental and not can_publish_baseline)
        ):
            return int(completed)
        assert evidence is not None
        return _publish_test_evidence(
            args,
            invocation,
            evidence,
            plan,
            completed=completed,
            fresh=_read_test_coverage(complete_path if replayed else run_data),
            incremental=incremental,
            replayed=replayed,
            pytest_args=pytest_args,
            staged_import_graph=staged_import_graph,
            graph_reasons=graph_reasons,
        )
