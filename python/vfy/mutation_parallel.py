"""Run pytest test files in separate forks of a collected session."""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import select
import signal
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pytest

from .mutation_workers import WorkerPool
from .native import plan_mutation_test_groups

if TYPE_CHECKING:
    from ._pytest_native import NativeRunResult
    from .data import CoverageData


@dataclass(frozen=True)
class FileGroupRun:
    status: Literal["completed", "timeout", "error"]
    detail: str = ""
    duration_seconds: float = 0.0
    failed_nodeid: str | None = None
    native_result: NativeRunResult | None = None


class _ForkSafetyError(RuntimeError):
    """The supervisor acquired a thread after planning isolated file groups."""


@dataclass
class _GroupJob:
    index: int
    pid: int
    result_path: Path
    lease_fd: int
    deadline: float
    registration: Path
    exit_fd: int | None = None


@dataclass(frozen=True)
class _GroupResult:
    status: Literal["completed", "timeout", "error", "cancelled"]
    native_result_json: str | None = None
    mutation_failure: tuple[str, str] | None = None
    detail: str = ""


@dataclass(frozen=True)
class _CoverageForkTarget:
    data: CoverageData
    parent_basename: str
    child_basename: str


def _coverage_finish() -> None:
    from .api import Coverage  # noqa: PLC0415 - child-only coverage lifecycle
    from .process_startup import active_coverage, finish  # noqa: PLC0415 - child-only lifecycle

    coverage = Coverage.current()
    if coverage is None:
        return
    if active_coverage() is coverage:
        finish()
    else:
        coverage.stop()
        coverage.save()


def _coverage_fork_target(session: pytest.Session) -> _CoverageForkTarget | None:
    """Stage fork-created coverage shards only when the vfy coverage adapter is active."""
    from ._pytest.native_pytest import (  # noqa: PLC0415 - native adapter cycle
        _vfy_bridge_plugin_enabled,
    )

    if not _vfy_bridge_plugin_enabled(session.config):
        return None

    coverage_plugin = session.config.pluginmanager.getplugin("_cov")
    if coverage_plugin is None:
        return None
    from .pytest_plugin import VfyCovPlugin  # noqa: PLC0415 - coverage adapter cycle

    if not isinstance(coverage_plugin, VfyCovPlugin) or not coverage_plugin._native_bridge:
        raise RuntimeError("native file groups require vfy's active coverage adapter")
    coverage = coverage_plugin.coverage
    if coverage is None:
        return None
    child_data = coverage_plugin._native_child_data
    if child_data is None:
        raise RuntimeError("native file groups have no staged child-data directory")
    data = coverage.data
    parent_basename = data.base_filename()
    child_basename = str(Path(child_data.name) / Path(parent_basename).name)
    return _CoverageForkTarget(data, parent_basename, child_basename)


def _run_child_group(
    session: pytest.Session,
    items: Sequence[pytest.Item],
    pool: WorkerPool,
    result_path: Path,
    lease_fd: int,
) -> int:
    """Run one native suite in a fork child and serialize its Rust result."""
    pool.after_fork(keep=lease_fd)
    native_result_json: str | None = None
    mutation_failure: tuple[str, str] | None = None
    status = "completed"
    detail = ""
    try:
        from ._pytest.native_pytest import (  # noqa: PLC0415 - native adapter cycle
            _run_native_session,
            _vfy_bridge_plugin_enabled,
        )

        capture_mode = session.config.getoption("capture", "fd")
        native_result = _run_native_session(
            session,
            capture=capture_mode != "no",
            maxfail=1,
            initial_failures=0,
            vfy_bridge=_vfy_bridge_plugin_enabled(session.config),
            items=items,
        )
        native_result_json = native_result.to_json()
        mutation_failure = native_result.mutation_failure()
    except Exception as error:  # noqa: BLE001 - preserve a structured child result
        status = "error"
        detail = f"native file-group child failed: {type(error).__name__}: {error}"
    finally:
        try:
            _coverage_finish()
        except Exception as error:  # noqa: BLE001 - report save is part of child completion
            status = "error"
            detail = f"failed to save child coverage data: {type(error).__name__}: {error}"
    try:
        result_path.write_text(
            json.dumps(
                {
                    "status": status,
                    "detail": detail,
                    "native_result": native_result_json,
                    "mutation_failure": mutation_failure,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    except (OSError, UnicodeError, TypeError, ValueError):
        return 1
    return 0


def _start_group(
    index: int,
    session: pytest.Session,
    groups: Sequence[Sequence[pytest.Item]],
    pool: WorkerPool,
    lease_fd: int,
    result_path: Path,
    timeout: float,
    coverage_target: _CoverageForkTarget | None,
) -> _GroupJob:
    read_fd, write_fd = os.pipe()
    try:
        if coverage_target is not None:
            # Coverage's at-fork handler derives its suffixed child filename
            # from this basename. Restore the parent immediately after fork.
            coverage_target.data._basename = coverage_target.child_basename
        try:
            if threading.active_count() != 1:
                raise _ForkSafetyError(
                    "file-isolated mutation execution cannot fork with active threads"
                )
            pid = os.fork()
        finally:
            if coverage_target is not None:
                coverage_target.data._basename = coverage_target.parent_basename
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    if pid == 0:  # pragma: no cover - covered through the public pytest boundary
        os.close(write_fd)
        try:
            # The outer watchdog must know our private group before any test
            # can spawn descendants. EOF means the supervisor died at startup.
            try:
                ready = os.read(read_fd, 1)
            finally:
                os.close(read_fd)
            if ready != b"1":
                os._exit(1)
            devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            os.close(devnull)
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = _run_child_group(session, groups[index], pool, result_path, lease_fd)
        except (OSError, ValueError, TypeError, RuntimeError):
            code = 1
        os._exit(code)
    os.close(read_fd)
    registration = pool.directory / f"group-{os.getpgrp()}-{pid}"
    job = _GroupJob(index, pid, result_path, lease_fd, time.monotonic() + timeout, registration)
    try:
        os.setpgid(pid, pid)
        registration.touch(exist_ok=False)
        os.write(write_fd, b"1")
        if hasattr(os, "pidfd_open"):
            # Kernels and process sandboxes can expose the API without
            # allowing it. waitpid still owns reaping on that route.
            with contextlib.suppress(OSError):
                job.exit_fd = os.pidfd_open(pid)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        _terminate(job)
        raise
    finally:
        os.close(write_fd)
    return job


def _read_group_result(job: _GroupJob, wait_status: int) -> _GroupResult:
    if not os.WIFEXITED(wait_status) or os.WEXITSTATUS(wait_status) != 0:
        return _GroupResult("error", detail=f"native file-group child exited with {wait_status}")
    try:
        payload = json.loads(job.result_path.read_text(encoding="utf-8"))
        status = payload["status"]
        if status not in {"completed", "error"}:
            raise ValueError("invalid child status")
        native_result_json = payload.get("native_result")
        if status == "completed" and not isinstance(native_result_json, str):
            raise ValueError("completed child has no native result")
        raw_failure = payload.get("mutation_failure")
        if raw_failure is None:
            mutation_failure = None
        elif (
            isinstance(raw_failure, list)
            and len(raw_failure) == 2
            and all(isinstance(value, str) for value in raw_failure)
        ):
            mutation_failure = cast("tuple[str, str]", tuple(raw_failure))
        else:
            raise ValueError("invalid mutation failure")
        return _GroupResult(
            status,
            native_result_json if isinstance(native_result_json, str) else None,
            mutation_failure,
            str(payload.get("detail", "")),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        return _GroupResult("error", detail=f"invalid native file-group result: {error}")


def _terminate(job: _GroupJob) -> None:
    try:
        _terminate_group(job)
    finally:
        if job.exit_fd is not None:
            os.close(job.exit_fd)
            job.exit_fd = None


def _terminate_group(job: _GroupJob) -> None:
    reaped = False
    try:
        os.killpg(job.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError as group_error:
        # Darwin can transiently return EPERM while our group leader is exiting.
        # Signal it directly only while waitpid confirms it is still our
        # unreaped child; its PID cannot be reused until we reap it.
        try:
            waited, _ = os.waitpid(job.pid, os.WNOHANG)
        except ChildProcessError:
            waited = job.pid
        if waited == 0:
            try:
                os.kill(job.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as child_error:
                raise child_error from group_error
            with contextlib.suppress(ChildProcessError):
                os.waitpid(job.pid, 0)
            reaped = True
        elif waited == job.pid:
            reaped = True
        else:
            raise RuntimeError(
                f"waitpid returned unexpected child {waited} for group {job.pid}"
            ) from group_error
        # The direct child may have descendants in the group. Retry once after
        # reaping it; only ESRCH means the group has already disappeared.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(job.pid, signal.SIGKILL)
    if not reaped:
        with contextlib.suppress(ChildProcessError):
            os.waitpid(job.pid, 0)
    job.registration.unlink(missing_ok=True)


def _group_timeout() -> float:
    raw_timeout = os.environ.get("VFY_MUTATION_TEST_TIMEOUT", "30")
    timeout = float(raw_timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("VFY_MUTATION_TEST_TIMEOUT must be positive")
    return timeout


def _settings() -> tuple[Path, int, float, list[str]] | FileGroupRun:
    pool_path = os.environ.get("VFY_MUTATION_WORKER_POOL")
    try:
        worker_count = int(os.environ["VFY_MUTATION_WORKERS"])
        timeout = _group_timeout()
        preferred_raw = json.loads(os.environ.get("VFY_MUTATION_PREFERRED_TESTS", "[]"))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        return FileGroupRun("error", f"invalid file-group settings: {error}")
    if pool_path is None or worker_count < 1:
        return FileGroupRun("error", "file-group worker pool is not configured")
    if not isinstance(preferred_raw, list) or not all(
        isinstance(nodeid, str) for nodeid in preferred_raw
    ):
        return FileGroupRun("error", "preferred mutation tests must be a JSON list of node IDs")
    preferred = cast("list[str]", preferred_raw)
    return Path(pool_path), worker_count, timeout, preferred


def _planned_groups(
    selected: Sequence[pytest.Item], preferred: Sequence[str]
) -> list[list[pytest.Item]] | FileGroupRun:
    try:
        indices = plan_mutation_test_groups([item.nodeid for item in selected], list(preferred))
        return [[selected[index] for index in group] for group in indices]
    except (IndexError, TypeError, ValueError, RuntimeError) as error:
        return FileGroupRun("error", f"failed to plan pytest file groups: {error}")


def _poll_groups(
    active: dict[int, _GroupJob],
    pool: WorkerPool,
    results: dict[int, _GroupResult],
    earliest_terminal: int | None,
) -> tuple[bool, int | None]:
    changed = False
    now = time.monotonic()
    for index, job in tuple(active.items()):
        try:
            waited, wait_status = os.waitpid(job.pid, os.WNOHANG)
        except ChildProcessError:
            waited, wait_status = job.pid, 1
        if waited:
            _terminate(job)
            pool.release(job.lease_fd)
            result = _read_group_result(job, wait_status)
            results[index] = result
            del active[index]
            changed = True
            if (result.status != "completed" or result.mutation_failure is not None) and (
                earliest_terminal is None or index < earliest_terminal
            ):
                earliest_terminal = index
        elif now >= job.deadline:
            _terminate(job)
            pool.release(job.lease_fd)
            results[index] = _GroupResult("timeout", detail="pytest file group timed out")
            del active[index]
            changed = True
            if earliest_terminal is None or index < earliest_terminal:
                earliest_terminal = index
    return changed, earliest_terminal


def _cancel_groups_after(
    active: dict[int, _GroupJob],
    next_index: int,
    earliest_terminal: int,
    pool: WorkerPool,
    results: dict[int, _GroupResult],
) -> None:
    for index in range(earliest_terminal + 1, next_index):
        job = active.get(index)
        if job is not None:
            _terminate(job)
            pool.release(job.lease_fd)
            del active[index]
            results[index] = _GroupResult("cancelled")


def _replay_groups(results: dict[int, _GroupResult], limit: int, started: float) -> FileGroupRun:
    result_json: list[str] = []
    failed_nodeid = None
    for index in range(limit + 1):
        result = results.get(index)
        if result is None:
            return FileGroupRun(
                "error",
                f"native file group {index} has no result",
                time.monotonic() - started,
            )
        if result.status in {"error", "timeout"}:
            return FileGroupRun(
                result.status,
                result.detail,
                time.monotonic() - started,
            )
        if result.status != "completed" or result.native_result_json is None:
            return FileGroupRun(
                "error",
                f"native file group {index} did not complete with a suite result",
                time.monotonic() - started,
            )
        result_json.append(result.native_result_json)
        if failed_nodeid is None and result.mutation_failure is not None:
            failed_nodeid = result.mutation_failure[0]
    from .native import _pytest  # noqa: PLC0415 - native execution is platform-gated

    try:
        native_result = _pytest.NativeRunResult.merge_json_results(result_json)
    except (RuntimeError, TypeError, ValueError) as error:
        return FileGroupRun(
            "error",
            f"failed to merge native file-group results: {error}",
            time.monotonic() - started,
        )
    return FileGroupRun(
        "completed",
        duration_seconds=time.monotonic() - started,
        failed_nodeid=failed_nodeid,
        native_result=native_result,
    )


def _execute_groups(
    session: pytest.Session,
    groups: list[list[pytest.Item]],
    pool_path: Path,
    worker_count: int,
    timeout: float,
    started: float,
) -> FileGroupRun:
    coverage_target = _coverage_fork_target(session)
    pool = WorkerPool(pool_path, worker_count)
    pool.adopt_inherited_lease()
    results: dict[int, _GroupResult] = {}
    active: dict[int, _GroupJob] = {}
    earliest_terminal: int | None = None
    next_index = 0
    schedule_limit = len(groups)
    queue_deadline = started + timeout
    with tempfile.TemporaryDirectory(prefix="vfy-mutation-groups-") as temporary:
        root = Path(temporary)
        try:
            while active or (earliest_terminal is None and next_index < schedule_limit):
                changed = False
                # Fill available leases before sleeping. Poll each registered
                # child so an observed failure stops the remaining launches.
                while earliest_terminal is None and next_index < schedule_limit:
                    lease = pool.acquire()
                    if lease is None:
                        break
                    result_path = root / f"{next_index}.json"
                    active[next_index] = _start_group(
                        next_index,
                        session,
                        groups,
                        pool,
                        lease,
                        result_path,
                        timeout,
                        coverage_target,
                    )
                    next_index += 1
                    polled, earliest_terminal = _poll_groups(
                        active, pool, results, earliest_terminal
                    )
                    changed = changed or polled
                polled, earliest_terminal = _poll_groups(active, pool, results, earliest_terminal)
                changed = changed or polled
                if earliest_terminal is not None:
                    _cancel_groups_after(active, next_index, earliest_terminal, pool, results)
                    next_index = max(next_index, earliest_terminal + 1)
                if not active and next_index < schedule_limit and earliest_terminal is None:
                    if time.monotonic() >= queue_deadline:
                        results[next_index] = _GroupResult(
                            "timeout", detail="timed out waiting for a file-group worker lease"
                        )
                        earliest_terminal = next_index
                        next_index += 1
                    elif not changed:
                        _wait_groups(active, waiting_for_lease=True)
                elif active and not changed:
                    _wait_groups(active, waiting_for_lease=next_index < schedule_limit)
            replay_limit = earliest_terminal if earliest_terminal is not None else len(groups) - 1
            return _replay_groups(results, replay_limit, started)
        except _ForkSafetyError as error:
            return FileGroupRun("error", str(error), time.monotonic() - started)
        finally:
            for job in active.values():
                _terminate(job)
                pool.release(job.lease_fd)
            pool.close()


def _wait_groups(active: dict[int, _GroupJob], *, waiting_for_lease: bool) -> None:
    # A completed child should immediately release its shared execution slot.
    # Lease release by another supervisor has no descriptor, so bound that wait.
    timeout = 0.001 if waiting_for_lease else 0.05
    now = time.monotonic()
    poller = select.poll()
    for job in active.values():
        timeout = min(timeout, max(0.0, job.deadline - now))
        if job.exit_fd is None:
            timeout = min(timeout, 0.001)
        else:
            poller.register(job.exit_fd, select.POLLIN)
    with contextlib.suppress(InterruptedError):
        poller.poll(max(1, math.ceil(timeout * 1000)) if timeout else 0)


def run_file_groups(
    session: pytest.Session, items: Sequence[pytest.Item] | None = None
) -> FileGroupRun:
    """Run collected items in native source-file groups and merge their Rust results."""
    started = time.monotonic()
    if os.name != "posix" or not hasattr(os, "fork"):
        return FileGroupRun("error", "file-isolated mutation execution requires POSIX fork")
    if threading.active_count() != 1:
        return FileGroupRun(
            "error", "file-isolated mutation execution cannot fork with active threads"
        )
    settings = _settings()
    if isinstance(settings, FileGroupRun):
        return settings
    pool_path, worker_count, timeout, preferred = settings
    selected = list(session.items if items is None else items)
    if not selected:
        from ._pytest.native_pytest import (  # noqa: PLC0415 - native adapter cycle
            _run_native_session,
            _vfy_bridge_plugin_enabled,
        )

        capture_mode = session.config.getoption("capture", "fd")
        result = _run_native_session(
            session,
            capture=capture_mode != "no",
            maxfail=1,
            initial_failures=session.testsfailed,
            vfy_bridge=_vfy_bridge_plugin_enabled(session.config),
            items=selected,
        )
        return FileGroupRun(
            "completed",
            duration_seconds=time.monotonic() - started,
            failed_nodeid=(result.mutation_failure() or (None, None))[0],
            native_result=result,
        )
    groups = _planned_groups(selected, preferred)
    if isinstance(groups, FileGroupRun):
        return groups
    return _execute_groups(session, groups, pool_path, worker_count, timeout, started)
