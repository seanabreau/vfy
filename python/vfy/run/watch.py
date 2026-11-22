"""Re-run `vfy test` when watched project files change."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vfy.command_config import apply_config
from vfy.errors import PytestConfigurationError, VfyError
from vfy.native import (
    TestHistoryLease,
    WatchSession,
    file_digest,
    snapshot_test_history,
)

from .record import (
    green_test_run_path,
)

if TYPE_CHECKING:
    from vfy.config_types import Config


from .arguments import _stage_pytest_arguments
from .engine import TestEngine, _select_test_engine
from .replay import _prepare_test_invocation
from .reports import _controller_message, _replay_cache
from .selected_run import _run_test_selection_once
from .watch_scope import _watch_baseline_ignored_paths, _watch_ignored_paths, _watch_project_roots


def _watch_history_name(history: str) -> str:
    return "history.sqlite3" if Path(history).suffix == ".sqlite3" else "history.json"


def _watch_history_digest(path: Path) -> str | None:
    return file_digest(str(path)) if path.suffix == ".sqlite3" and path.is_file() else None


def _watch_publication_artifacts(
    directory: Path,
    args: argparse.Namespace,
    staged_reports: dict[Path, Path | None],
) -> list[tuple[str, str]]:
    candidates: list[tuple[Path, Path | None]] = [
        (directory / "data.coverage", Path(args.data_file).resolve()),
        (directory / _watch_history_name(args.history), Path(args.history).resolve()),
        (directory / "replay.coverage", _replay_cache(args.history)),
        (directory / "test-run.json", green_test_run_path(args.history)),
        *staged_reports.items(),
    ]
    artifacts: list[tuple[str, str]] = []
    for source, destination in candidates:
        if not source.exists():
            continue
        if destination is None:
            from vfy.data import CoverageStore  # noqa: PLC0415 - annotations need measured files

            store = CoverageStore.read(directory / "data.coverage")
            measured = [Path(path) for path in store.measured_files()]
            for child in sorted(source.iterdir()):
                matches = [
                    path.with_name(f"{path.name},cover")
                    for path in measured
                    if f"{path.name},cover" == child.name
                ]
                if len(matches) != 1:
                    raise VfyError(f"ambiguous annotation destination: {child.name}")
                artifacts.append((str(child), str(matches[0].resolve())))
        else:
            artifacts.append((str(source), str(destination)))
    return artifacts


def _watch_iteration(args: argparse.Namespace, session: WatchSession, run_number: int) -> None:
    request = session.begin_run()
    completed: tuple[int, bool] | None = None
    phase = "configuration"
    try:
        run_args = copy.copy(args)
        run_args._watch_run_number = run_number
        for name, value in getattr(args, "_watch_seed", {}).items():
            setattr(run_args, name, copy.deepcopy(value))
        run_args, _run_report, run_config = apply_config(run_args)
        invocation = _prepare_test_invocation(run_args, force_full=False)
        if run_args.mode in {"balanced", "aggressive"} or run_config.get("vfy", {}).get(
            "test", {}
        ).get("selection") in {"balanced", "aggressive"}:
            raise VfyError("--watch supports only the safe selection policy")
        identity_roots = [str(path) for path in _watch_project_roots(list(run_args.source or []))]
        identity_ignored = [
            str(path)
            for path in _watch_ignored_paths(
                run_args, run_config, invocation.pytest_args, invocation.configuration
            )
        ]
        session.set_roots(identity_roots)
        session.set_ignored(identity_ignored)
        publish_args = copy.copy(run_args)
        phase = "preparation"
        with tempfile.TemporaryDirectory(prefix="vfy-watch-run-") as temporary:
            stage = Path(temporary)
            run_args.pytest_args, staged_reports = _stage_pytest_arguments(
                list(run_args.pytest_args), run_config, stage
            )
            run_args.data_file = str(stage / "data.coverage")
            history_name = _watch_history_name(publish_args.history)
            run_args.history = str(stage / history_name)
            run_args._watch_original_pytest_args = list(publish_args.pytest_args)
            run_args._watch_original_data_file = publish_args.data_file
            run_args._watch_original_history = publish_args.history
            run_args._watch_pycache_prefix = stage / "pycache"
            run_args._watch_input_identity = request.input_identity
            run_args._watch_identity_roots = identity_roots
            run_args._watch_identity_ignored = identity_ignored
            history_source = Path(publish_args.history).resolve()
            with TestHistoryLease(str(history_source)):
                history_digest = _watch_history_digest(history_source)
                if history_source.is_file():
                    snapshot_test_history(str(history_source), str(stage / history_name))
            for original, name in (
                (Path(publish_args.data_file), "data.coverage"),
                (_replay_cache(publish_args.history), "replay.coverage"),
            ):
                if original.is_file():
                    shutil.copy2(original, stage / name)
            phase = "execution"
            # Staging changes report destinations without changing the selected
            # config, coverage options, or plug-in requests for this iteration.
            status = _run_test_selection_once(
                run_args,
                copy.deepcopy(run_config),
                force_full=bool(args.fresh) or request.full_run,
                obsolete_check=lambda: not session.is_current(),
                configuration=invocation.configuration,
            )
            if getattr(run_args, "_test_engine", None) == TestEngine.INDEPENDENT and not getattr(
                run_args, "_watch_execution_evidence_complete", False
            ):
                raise VfyError("independent execution did not produce complete test evidence")
            phase = "publication"
            resolved_manifest = stage / "resolved-outputs.json"
            if resolved_manifest.is_file():
                resolved_outputs = json.loads(resolved_manifest.read_text(encoding="utf-8"))
                session.set_resolved_ignored([path for path in resolved_outputs.values() if path])
                staged_reports.update(
                    {
                        Path(source): Path(destination) if destination else None
                        for source, destination in resolved_outputs.items()
                    }
                )
            if session.is_current():
                artifacts = _watch_publication_artifacts(stage, publish_args, staged_reports)
                with TestHistoryLease(str(history_source)):
                    if _watch_history_digest(history_source) != history_digest:
                        raise VfyError(
                            "test history changed during watch execution; save a file to retry"
                        )
                    obsolete = session.finalize(
                        str(stage), artifacts, status, run_args._watch_full_run
                    )
            else:
                obsolete = session.finish_run(status, run_args._watch_full_run)
            completed = status, obsolete
    except (VfyError, OSError, ValueError, RuntimeError) as exc:
        print(f"Watch run {run_number}: {phase} error: {exc}", file=sys.stderr)
    if completed is None:
        session.finish_run(1, False)
    else:
        status, obsolete = completed
        message = "obsolete; queued changes" if obsolete else f"pytest exited {status}"
        _controller_message(args, f"Watch run {run_number}: {message}")


def _run_test_selection_watch(args: argparse.Namespace, config: Config) -> int:
    try:
        invocation = _prepare_test_invocation(args, force_full=False)
    except PytestConfigurationError:
        # The first iteration reports the error; the watcher must survive its repair.
        ignored = sorted(_watch_baseline_ignored_paths(args), key=str)
    else:
        _select_test_engine(args, invocation.pytest_args, invocation.configuration)
        ignored = _watch_ignored_paths(
            args, config, invocation.pytest_args, invocation.configuration
        )
    roots = _watch_project_roots(list(args.source or []))
    try:
        session = WatchSession([str(path) for path in roots], [str(path) for path in ignored], 200)
    except RuntimeError as exc:
        raise VfyError(str(exc)) from exc

    run_number = 0
    pending = True
    try:
        while True:
            if pending:
                run_number += 1
                _controller_message(args, f"Watch run {run_number}: starting")
                _watch_iteration(args, session, run_number)
                pending = False
            batch = session.wait_for_batch(250)
            watcher_failure = session.failure()
            if watcher_failure is not None:
                raise VfyError(watcher_failure)
            if batch:
                if getattr(args, "verbose", 0):
                    _controller_message(args, f"Watch changes: {batch['paths']}")
                for reason in batch["reasons"]:
                    _controller_message(args, f"Watch fallback: {reason}")
                pending = True
    except KeyboardInterrupt:
        return 2
    except (VfyError, RuntimeError) as exc:
        print(f"vfy: error: watch stopped: {exc}", file=sys.stderr)
        return 1
    finally:
        session.stop()
