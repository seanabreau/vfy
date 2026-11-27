"""Prepare test invocations and record or replay passing-run evidence."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from vfy.dependency_fingerprint import PendingDependencyFingerprint
from vfy.errors import DataError, VfyError
from vfy.independent.configuration import PytestConfiguration as _PytestConfiguration
from vfy.invocation_preparation import finish, start
from vfy.native import (
    git_replay_state,
    replay_fingerprint,
    source_tree_identity,
)
from vfy.source_roots import source_graph_roots
from vfy.workers import requested_worker_collection

from .record import (
    GREEN_FINGERPRINT_KEY,
    GREEN_HISTORY_DIGEST_KEY,
    GREEN_IGNORED_PATHS_KEY,
    GREEN_SOURCE_IDENTITY_KEY,
    GREEN_SOURCE_ROOTS_KEY,
    execution_fingerprint,
    native_execution_fingerprint,
)

if TYPE_CHECKING:
    from vfy.config_types import Config
    from vfy.data import CoverageStore
    from vfy.dependency_fingerprint import PendingDependencyFingerprint
    from vfy.invocation_preparation import PreparedInvocation
    from vfy.native_types import (
        PytestConfigRecord,
    )


from .engine import (
    TestEngine,
    _conflicting_asyncio_plugin,
    _resolve_pytest_configuration,
    _xdist_requested,
)
from .plan import GitReplayState, SelectionPlan
from .profiling import _profile_begin, _profile_end
from .pytest_controller import TestController, _controller_for_engine
from .reports import _controller_message, _pytest_report_outputs, _replay_cache
from .watch_scope import _WATCH_INPUT_IDENTITY, _watch_ignored_paths, _watch_project_roots

_REPLAY_COMMIT_KEY = "vfy_replay_commit"


_REPLAY_FINGERPRINT_KEY = "vfy_replay_fingerprint"


def _requested_startup_grace(native_engine: bool) -> str | None:
    value = os.environ.get("VFY_NATIVE_STARTUP_GRACE_MS") if native_engine else None
    return None if value == "0" else value


def _test_invocation_fingerprint(
    args: argparse.Namespace,
    config: Config,
    pytest_args: list[str],
    *,
    workers: str | int | None = None,
    distribution: str | None = None,
    native_scheduling: str | None = None,
    worker_collection: bool = False,
    worker_start: str | None = None,
    startup_grace: str | None = None,
    dependency_fingerprint: str,
    effective_pytest_args: Sequence[str],
    pytest_configuration: PytestConfigRecord,
) -> str:
    fingerprint = replay_fingerprint(
        [
            execution_fingerprint(
                config,
                list(args.source or []),
                pytest_args,
                dependency_fingerprint=dependency_fingerprint,
                engine=args._test_engine,
                effective_pytest_args=effective_pytest_args,
                pytest_configuration=pytest_configuration,
            ),
            str(workers) if workers is not None else "",
            distribution or "pytest-config",
            "stock-xdist-scheduler",
        ]
    )
    return native_execution_fingerprint(
        fingerprint,
        native_scheduling,
        worker_collection=worker_collection,
        worker_start=worker_start,
        startup_grace=startup_grace,
    )


def _installed_dependency_fingerprint(pending: PendingDependencyFingerprint) -> str:
    """Fingerprint all installed distributions before reusing a passing run.

    Plug-ins and transitive dependencies can change test behavior without a
    change to the project's configuration.
    """

    started = _profile_begin()
    value = pending.take()
    _profile_end("dependency_fingerprinting", started)
    return value


def _green_baseline(
    path: Path,
    *,
    head: str,
    fingerprint: str,
) -> tuple[CoverageStore | None, str | None]:
    from vfy.data import CoverageStore  # noqa: PLC0415 - only replay reads saved coverage

    try:
        store = CoverageStore.read(path)
    except FileNotFoundError:
        return None, "no passing baseline exists yet"
    except (DataError, OSError) as exc:
        return None, f"the passing baseline is unreadable: {exc}"
    metadata = store.metadata
    if metadata.get(_REPLAY_COMMIT_KEY) != head:
        return None, "Git HEAD changed since the passing baseline"
    if metadata.get(_REPLAY_FINGERPRINT_KEY) != fingerprint:
        return None, "the Python, configuration, environment, or pytest invocation changed"
    return store, None


def _write_green_baseline(
    store: CoverageStore,
    path: Path,
    *,
    head: str | None,
    fingerprint: str | None,
    green_fingerprint: str,
    history_digest: str,
    source_identity: str,
    source_roots: list[str],
    ignored_paths: list[str],
    collected: Sequence[str] | None = None,
) -> None:
    if collected is not None:
        store.metadata["vfy_green_test_collection"] = json.dumps(collected, separators=(",", ":"))
    store.metadata.update(
        {
            GREEN_FINGERPRINT_KEY: green_fingerprint,
            GREEN_HISTORY_DIGEST_KEY: history_digest,
            GREEN_SOURCE_IDENTITY_KEY: source_identity,
            GREEN_SOURCE_ROOTS_KEY: json.dumps(source_roots, separators=(",", ":")),
            GREEN_IGNORED_PATHS_KEY: json.dumps(ignored_paths, separators=(",", ":")),
        }
    )
    if head is None or fingerprint is None:
        store.metadata.pop(_REPLAY_COMMIT_KEY, None)
        store.metadata.pop(_REPLAY_FINGERPRINT_KEY, None)
    else:
        store.metadata.update(
            {
                _REPLAY_COMMIT_KEY: head,
                _REPLAY_FINGERPRINT_KEY: fingerprint,
            }
        )
    store.metadata.pop("vfy_replay_version", None)
    store.write(path)


@dataclass(frozen=True)
class _TestInvocation:
    controller: TestController
    pytest_args: list[str]
    configuration: _PytestConfiguration
    baseline_pytest_args: tuple[str, ...]
    location: str | None
    source_roots: list[str]
    distribution: str
    native_scheduling: str | None
    replay_enabled: bool
    history_path: str
    cache_path: Path
    targets: list[str]
    worker_collection: bool = False
    worker_start: str | None = None
    startup_grace: str | None = None
    # Directories and files behind source roots, for the selection import graph.
    graph_roots: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _ReplayEvidence:
    state: GitReplayState
    git_available: bool
    fingerprint: str
    baseline: CoverageStore | None
    baseline_reason: str | None
    ignored_outputs: list[str]
    green_fingerprint: str
    source_identity: str = ""
    identity_roots: tuple[str, ...] = ()
    identity_ignored: tuple[str, ...] = ()
    dependency_fingerprint: str = ""


def _prepare_test_invocation(
    args: argparse.Namespace,
    *,
    force_full: bool,
    configuration: _PytestConfiguration | None = None,
    prepared: PreparedInvocation | None = None,
) -> _TestInvocation:
    if force_full:
        args.mode = "full"
        args.fresh = False
    owner = start(args) if prepared is None else prepared.owner
    targets = owner.targets
    location = owner.location
    pytest_args = owner.pytest_args
    baseline_pytest_args = tuple(owner.baseline_pytest_args)
    engine = TestEngine(getattr(args, "engine", TestEngine.INDEPENDENT.value))
    independent = engine is TestEngine.INDEPENDENT
    if prepared is not None:
        configuration = prepared.configuration
    if configuration is None:
        configuration = _resolve_pytest_configuration(pytest_args, engine=engine)
    conflicting_asyncio_plugin = None if independent else _conflicting_asyncio_plugin(pytest_args)
    if conflicting_asyncio_plugin is not None:
        raise VfyError(
            "vfy test includes built-in asyncio support; remove the explicit "
            f"pytest-asyncio plug-in load ({conflicting_asyncio_plugin!r}) and remove "
            "pytest-asyncio from the project's test dependencies"
        )
    native_engine = engine is TestEngine.NATIVE
    source_roots = list(args.source or [])
    plan = (
        finish(
            args,
            owner,
            configuration,
            force_full=force_full,
            native_xdist_requested=native_engine and _xdist_requested(configuration),
        )
        if prepared is None
        else prepared.plan
    )
    controller = _controller_for_engine(plan["controller_workers"], engine)
    native_scheduling = str(plan["scheduling"]) if plan["scheduling"] is not None else None
    distribution = plan["distribution"]
    replay_enabled = plan["replay_enabled"]
    history_path = str(Path(args.history).resolve())
    cache_path = _replay_cache(args.history)
    worker_start = os.environ.get("VFY_WORKER_START") if native_engine else None
    if worker_start == "exec":
        worker_start = None
    startup_grace = _requested_startup_grace(native_engine)
    return _TestInvocation(
        controller,
        pytest_args,
        configuration,
        baseline_pytest_args,
        location,
        source_roots,
        distribution,
        native_scheduling,
        replay_enabled,
        history_path,
        cache_path,
        targets,
        # Fingerprint the requested worker policy even if configure hooks reject it.
        worker_collection=(
            native_engine
            and requested_worker_collection(
                args.workers,
                policy=os.environ.get("VFY_WORKER_COLLECTION"),
                worker_start=worker_start,
                mutation_isolation=os.environ.get("VFY_MUTATION_TEST_ISOLATION"),
            )
        ),
        worker_start=worker_start,
        startup_grace=startup_grace,
        graph_roots=source_graph_roots(source_roots),
    )


def _replay_evidence(
    args: argparse.Namespace,
    config: Config,
    invocation: _TestInvocation,
    *,
    purpose: Literal["selection", "replay"] = "replay",
) -> _ReplayEvidence:
    controller = invocation.controller
    pytest_args = invocation.pytest_args
    cache_path = invocation.cache_path
    ignored_outputs = [
        str(cache_path.parent),
        str(Path(args.data_file).resolve()),
        f"{Path(args.data_file).resolve()}.",
        *(
            str(Path(directory).resolve())
            for directory in (
                ".vfy",
                ".pytest_cache",
                ".ruff_cache",
                ".mypy_cache",
                ".tox",
                ".nox",
                ".venv",
                "venv",
                "__pycache__",
            )
        ),
        *_pytest_report_outputs(pytest_args, config, invocation.configuration),
    ]
    ignored_outputs.extend(
        str(Path(path).resolve())
        for name in ("baseline", "export_baseline", "plan_output")
        if (path := getattr(args, name, None))
    )
    for attribute in ("_watch_original_data_file", "_watch_original_history"):
        original = getattr(args, attribute, None)
        if original:
            original_path = Path(original).resolve()
            ignored_outputs.extend((str(original_path), f"{original_path}."))
    if getattr(args, "watch", False):
        ignored_outputs.extend(
            str(path)
            for path in _watch_ignored_paths(
                args, config, list(args._watch_original_pytest_args), invocation.configuration
            )
        )
    pending = None
    if purpose == "replay":
        pending = PendingDependencyFingerprint()
    try:
        git_available = True
        git_started = _profile_begin()
        try:
            state = GitReplayState.from_rust(git_replay_state(ignored_outputs))
        except RuntimeError as exc:
            # Without Git (for example, in a source archive), run the full suite
            # and preserve the replay baseline.
            git_available = False
            state = GitReplayState("", False, (), True, True, "")
            if args.fresh:
                _controller_message(
                    args, f"Git replay unavailable ({exc}); running without a saved baseline"
                )
        _profile_end("git", git_started)
        if purpose == "selection":
            return _ReplayEvidence(state, git_available, "", None, None, ignored_outputs, "")
        fingerprint_args = controller.resolve_pytest_args(
            pytest_args,
            route=TestEngine(args._test_engine),
            configuration=invocation.configuration,
        )
        if getattr(args, "watch", False):
            fingerprint_args = controller.resolve_pytest_args(
                [*invocation.targets, *args._watch_original_pytest_args],
                route=TestEngine(args._test_engine),
                configuration=invocation.configuration,
            )
        if pending is None:
            raise RuntimeError("replay dependency evidence has no owner")
        dependency_fingerprint = _installed_dependency_fingerprint(pending)
    finally:
        if pending is not None:
            pending.close()
    fingerprint = _test_invocation_fingerprint(
        args,
        config,
        fingerprint_args,
        workers=controller.workers,
        distribution=invocation.distribution,
        native_scheduling=invocation.native_scheduling,
        worker_collection=invocation.worker_collection,
        worker_start=invocation.worker_start,
        startup_grace=invocation.startup_grace,
        dependency_fingerprint=dependency_fingerprint,
        effective_pytest_args=invocation.configuration.effective_args,
        pytest_configuration=invocation.configuration.projection,
    )
    green_fingerprint = execution_fingerprint(
        config,
        list(args.source or []),
        invocation.baseline_pytest_args,
        dependency_fingerprint=dependency_fingerprint,
        engine=args._test_engine,
        effective_pytest_args=invocation.configuration.effective_args,
        pytest_configuration=invocation.configuration.projection,
    )
    green_fingerprint = native_execution_fingerprint(
        green_fingerprint,
        invocation.native_scheduling,
        worker_collection=invocation.worker_collection,
        worker_start=invocation.worker_start,
        startup_grace=invocation.startup_grace,
    )
    expected_identity = getattr(args, "_watch_input_identity", None)
    baseline: CoverageStore | None = None
    baseline_reason = state.submodule_change
    if (
        invocation.replay_enabled
        and git_available
        and state.submodule_change is None
        and not args.fresh
        and not getattr(args, "baseline", None)
    ):
        baseline_started = _profile_begin()
        baseline, baseline_reason = _green_baseline(
            cache_path,
            head=state.head,
            fingerprint=fingerprint,
        )
        if (
            baseline is not None
            and expected_identity is not None
            and state.clean
            and baseline.metadata.get(_WATCH_INPUT_IDENTITY) != expected_identity
        ):
            baseline = None
            baseline_reason = "the saved evidence belongs to an older source identity"
        _profile_end("baseline_load", baseline_started)
    return _ReplayEvidence(
        state,
        git_available,
        fingerprint,
        baseline,
        baseline_reason,
        ignored_outputs,
        green_fingerprint,
        dependency_fingerprint=dependency_fingerprint,
    )


def _capture_green_source_identity(
    args: argparse.Namespace,
    config: Config,
    invocation: _TestInvocation,
    evidence: _ReplayEvidence,
) -> _ReplayEvidence:
    identity_started = _profile_begin()
    expected_identity = getattr(args, "_watch_input_identity", None)
    if expected_identity is not None:
        identity_roots = tuple(args._watch_identity_roots)
        identity_ignored = tuple(args._watch_identity_ignored)
        source_identity = f"content:{expected_identity}"
    elif evidence.git_available and evidence.state.clean:
        identity_roots = ()
        identity_ignored = tuple(
            str(path)
            for path in _watch_ignored_paths(
                args, config, invocation.pytest_args, invocation.configuration
            )
        )
        source_identity = f"git:{evidence.state.head}"
    else:
        identity_roots = tuple(
            str(path) for path in _watch_project_roots(list(invocation.source_roots))
        )
        identity_ignored = tuple(
            str(path)
            for path in _watch_ignored_paths(
                args, config, invocation.pytest_args, invocation.configuration
            )
        )
        try:
            source_identity = "content:" + source_tree_identity(
                list(identity_roots), list(identity_ignored)
            )
        except RuntimeError:
            source_identity = ""
    _profile_end("source_identity", identity_started)
    return replace(
        evidence,
        source_identity=source_identity,
        identity_roots=identity_roots,
        identity_ignored=identity_ignored,
    )


def _unsaved_selection_status(args: argparse.Namespace) -> int:
    _controller_message(
        args,
        "Complete coverage was not saved: the selected run's evidence could not be recorded",
    )
    return 1


def _replay_complete_path(stage: Path) -> Path:
    return stage / "replay-complete.coverage"


def _selected_run_reconstructed(
    incremental: bool, completed: int, run_data: Path, complete_path: Path
) -> bool:
    """Return whether a selected run that published its data also rebuilt complete coverage."""
    return incremental and completed in {0, 1} and run_data.is_file() and complete_path.is_file()


def _stage_replay_merge(
    stage: Path, baseline: CoverageStore, plan: SelectionPlan | None = None
) -> str:
    """Give the selected run what it needs to reconstruct complete coverage itself.

    Without ``plan`` the run binds the plan after collection from ``VFY_CI_FINAL``.
    """
    baseline_path = stage / "replay-baseline.coverage"
    baseline.write(baseline_path)
    specification: dict[str, object] = {
        "baseline": str(baseline_path),
        "complete": str(_replay_complete_path(stage)),
    }
    if plan is not None:
        specification["plan"] = {
            "fallback_full": plan.fallback_full,
            "selected": list(plan.selected),
            "affected_files": list(plan.affected_files),
            "scoped_files": {path: list(shifts) for path, shifts in plan.scoped_files.items()},
        }
    specification_path = stage / "replay-merge.json"
    specification_path.write_text(json.dumps(specification), encoding="utf-8")
    return str(specification_path)
