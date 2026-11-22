"""Run mutations from one collected pytest session on POSIX.

A clean parent forks short-lived children. Each child replaces one imported
function's code, runs the relevant items, and returns a result through a pipe.
Module and class bodies, decorated callables, incompatible closures, active
threads, and non-POSIX interpreters use the subprocess backend.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import select
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import FunctionType
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    import pytest
    from _pytest._code.code import ExceptionRepr

    from ._pytest_native import MutationProcessWatchdog
    from .mutate import Mutation

from . import native
from .errors import VfyError
from .invocation_preparation import pytest_option
from .mutation_workers import WorkerPool, terminate_file_groups
from .native import (
    mutation_baseline_timeout,
    mutation_portable_worker_limit,
    mutation_test_durations,
    mutation_timeout,
    next_mutation_job,
    order_mutation_jobs,
    plan_mutation_test_groups,
)
from .native_types import MutationStatus, MutationTimeoutPolicy
from .run.arguments import (
    _asyncio_plugin_args,
)
from .run.engine import (
    _configured_pytest_cov_override,
    _native_xdist_plugin_args,
    _pytest_plugin_names,
    _PytestConfiguration,
    _resolve_pytest_configuration,
    _without_pytest_cov_loads,
    native_adapter_plugin_args,
)


def _result_status(value: object) -> MutationStatus:
    if isinstance(value, str) and value in {
        "protected",
        "survived",
        "unreached",
        "invalid",
        "error",
        "timeout",
        "unstable",
        "killed",
    }:
        return value
    return "error"


class MutationTestScope(Enum):
    SELECTED = "selected"
    FULL_SUITE = "full_suite"
    UNREACHED = "unreached"


@dataclass(frozen=True)
class MutationTestPlan:
    scope: MutationTestScope
    nodeids: tuple[str, ...] = ()


def _fork_pipe() -> tuple[int, int, int]:
    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(read_fd, False)
        if threading.active_count() != 1:
            raise _FastMutationError("collected mutation fork requires a single-threaded process")
        pid = os.fork()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    return read_fd, write_fd, pid


def _read_payload(job: _Job) -> None:
    # Drain without waiting for child exit: its result may exceed pipe capacity.
    if job.read_fd is None:
        return
    while True:
        try:
            chunk = os.read(job.read_fd, 1 << 20)
        except InterruptedError:
            continue
        except BlockingIOError:
            return
        except OSError:
            _close_payload_pipe(job)
            return
        if not chunk:
            _close_payload_pipe(job)
            return
        job.payload.extend(chunk)


def _close_payload_pipe(job: _Job) -> None:
    fd = job.read_fd
    job.read_fd = None
    if fd is not None:
        with contextlib.suppress(OSError):
            os.close(fd)


def _poll_child(job: _Job) -> int | None:
    try:
        waited, status = os.waitpid(job.pid, os.WNOHANG)
    except ChildProcessError:
        waited, status = job.pid, 1
    if waited == 0:
        return None
    _read_payload(job)
    return status


def _ready_descriptors(descriptors: Sequence[int], timeout: float) -> list[int]:
    ready: list[int]
    select_readable = select.select
    deadline = time.monotonic() + timeout
    try:
        if hasattr(select, "poll"):
            poller = select.poll()
            for fd in descriptors:
                poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
            milliseconds = max(1, int(timeout * 1000)) if timeout > 0 else 0
            events = poller.poll(milliseconds)
            ready = [fd for fd, event in events if not event & select.POLLNVAL]
            if events and not ready:
                time.sleep(min(0.001, max(0.0, deadline - time.monotonic())))
            return ready
        ready, _, _ = select_readable(list(descriptors), [], [], timeout)
    except InterruptedError:
        return []
    except (OSError, ValueError):
        # Unsupported or high descriptors must not turn the watchdog loop into
        # a busy poll. The caller still owns reaping and result classification.
        time.sleep(max(0.0, deadline - time.monotonic()))
        return []
    return ready


def _wait_jobs(jobs: Sequence[_Job], *, deadline: float | None = None) -> None:
    now = time.monotonic()
    timeout = 0.05
    if deadline is not None:
        timeout = min(timeout, max(0.0, deadline - now))
    for job in jobs:
        if deadline is None:
            timeout = min(timeout, max(0.0, job.started + job.timeout - now))
        if job.exit_fd is None and job.read_fd is None:
            # EOF precedes process exit. Poll promptly on platforms without
            # pidfds, after removing the permanently readable payload pipe.
            timeout = min(timeout, 0.001 if job.portable is None else 0.01)
    by_payload_fd = {job.read_fd: job for job in jobs if job.read_fd is not None}
    descriptors = [*by_payload_fd, *(job.exit_fd for job in jobs if job.exit_fd is not None)]
    for fd in _ready_descriptors(descriptors, timeout):
        job = by_payload_fd.get(fd)
        if job is not None:
            _read_payload(job)


def validate_mutation_pytest_args(configuration: _PytestConfiguration) -> None:
    """Reject options that invalidate native mutation failure classification."""
    configured = configuration.effective_args
    if "--continue-on-collection-errors" in configured:
        raise VfyError(
            "vfy mutate requires successful collection; remove --continue-on-collection-errors"
        )
    if "no:vfy._pytest.native_pytest" in configuration.plugins:
        raise VfyError(
            "vfy mutate requires its native executor; remove -p no:vfy._pytest.native_pytest"
        )
    try:
        native.NativeTestInvocation.mutation_worker_args(
            configured, configuration.projection["cli_args"]
        )
    except ValueError as error:
        raise VfyError(str(error)) from error


def mutation_traceback_args(configured: Sequence[str]) -> tuple[str, ...]:
    """Use Python tracebacks unless the caller requests richer diagnostics."""
    detailed_traceback = any(
        argument in {"--showlocals", "--full-trace", "--xfail-tb"}
        # A short-option value can contain `l`; retaining rich diagnostics in
        # that case also preserves grouped requests such as `-ql`.
        or (argument.startswith("-") and not argument.startswith("--") and "l" in argument[1:])
        for argument in configured
    )
    return (
        ("--tb=native",)
        if pytest_option(configured, "--tb") is None and not detailed_traceback
        else ()
    )


def mutation_pytest_args(
    pytest_args: Sequence[str], *, configuration: _PytestConfiguration | None = None
) -> list[str]:
    """Bootstrap collection with the same native executor for every mutation.

    Mutation runs load the same installed plug-ins as native ``vfy test``, so a
    suite that passes there also passes as the mutation baseline.
    """
    configuration = configuration or _resolve_pytest_configuration(list(pytest_args))
    validate_mutation_pytest_args(configuration)
    pytest_args, _ = _without_pytest_cov_loads(pytest_args)
    return [
        "-p",
        "vfy._pytest.native_pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        "no:pytest_cov",
        "-p",
        "no:slipcover",
        *_asyncio_plugin_args(configuration, _pytest_plugin_names(pytest_args)),
        "-p",
        "vfy.pytest_options",
        *mutation_traceback_args(configuration.effective_args),
        *_native_xdist_plugin_args(configuration, _pytest_plugin_names(pytest_args)),
        *native_adapter_plugin_args(configuration, _pytest_plugin_names(pytest_args)),
        *pytest_args,
        *_configured_pytest_cov_override(configuration),
    ]


@dataclass(frozen=True)
class RunnerOutcome:
    status: MutationStatus
    killer: str | None = None
    duration_ms: float = 0.0
    returncode: int | None = None
    detail: str = ""
    fallback: bool = False
    phase: str | None = None


class PortableMutationProcess(Protocol):
    """A fresh pytest process supervised by the mutation controller."""

    @property
    def pid(self) -> int: ...

    def poll(self) -> RunnerOutcome | None: ...

    def close(self) -> None: ...


class _PortableStarter(Protocol):
    def __call__(
        self, mutation: Mutation, plan: MutationTestPlan, lease_fd: int | None
    ) -> PortableMutationProcess | RunnerOutcome: ...


_JobKindName = Literal["collected", "portable"]


class _MutationJobKind(Enum):
    COLLECTED = "collected"
    PORTABLE = "portable"


@dataclass(frozen=True)
class _MutationJobSpec:
    mutation: Mutation
    kind: _MutationJobKind
    items: tuple[pytest.Item, ...] = ()
    plan: MutationTestPlan | None = None


@dataclass
class _Job:
    pid: int
    read_fd: int | None
    mutation: Mutation | None
    started: float
    timeout: float
    kind: _MutationJobKind = _MutationJobKind.COLLECTED
    payload: bytearray = field(default_factory=bytearray)
    lease_fd: int | None = None
    portable: PortableMutationProcess | None = None
    exit_fd: int | None = None


class _FastMutationError(Exception):
    """The collected fork or exact code-object replacement cannot be proven safe."""


class NativeMutationController:
    """Coordinate mutation children without executing tests in their parent."""

    def __init__(
        self,
        runner: CollectedMutationRunner,
        mutations: Sequence[tuple[Mutation, MutationTestPlan]],
    ) -> None:
        self.runner = runner
        self.mutations = mutations

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed and self.runner._collection_error is None:
            self.runner._collection_error = str(report.longrepr)

    def pytest_internalerror(self, excrepr: ExceptionRepr) -> None:
        self.runner._collection_error = str(excrepr)

    def run_native_mutations(self, session: pytest.Session) -> None:
        self.runner._session = session
        self.runner.items = {item.nodeid: item for item in session.items}
        self.runner._execute(session, self.mutations)


class CollectedMutationRunner:
    """Run one collected pytest session and many isolated fork children."""

    def __init__(
        self,
        tests: Sequence[str],
        execution_args: Sequence[str],
        timeout: float,
        workers: int,
        *,
        baseline_verified: bool = False,
        timeout_policy: MutationTimeoutPolicy = "adaptive",
        history_path: str | None = None,
        on_result: Callable[[str, RunnerOutcome], None] | None = None,
        portable_starter: _PortableStarter | None = None,
    ) -> None:
        self.tests = tuple(dict.fromkeys(str(test) for test in tests))
        self.execution_args = tuple(execution_args)
        self.timeout = timeout
        self.timeout_policy = timeout_policy
        self.workers = max(1, workers)
        self.baseline_verified = baseline_verified
        self._durations = self._read_durations(history_path)
        self.on_result = on_result
        self.portable_starter = portable_starter
        self.items: dict[str, pytest.Item] = {}
        self._collection_error: str | None = None
        self.baseline: RunnerOutcome | None = None
        self.outcomes: dict[str, RunnerOutcome] = {}
        self.portable_outcomes: dict[str, RunnerOutcome] = {}
        self.fallback: set[str] = set()
        # Recorded context IDs that collection no longer produces, such as
        # parametrize IDs built from the current time.
        self.uncollected: tuple[str, ...] = ()
        # Plans widened because their recorded tests were not collected again.
        self.plans: dict[str, MutationTestPlan] = {}
        # The collected path reports the IDs it actually ran while any later
        # fresh-process fallback still needs the full requested scope.
        self.report_plans: dict[str, MutationTestPlan] = {}
        self._session: pytest.Session | None = None
        self._prestarted: dict[int, _Job] = {}
        self._prestart_keys: set[str] = set()
        self._prestart_completed: list[tuple[_Job, RunnerOutcome]] = []
        self._prestart_watchdog: MutationProcessWatchdog | None = None
        self._prestart_expired: set[int] = set()
        self._pidfds: set[int] = set()
        self._blocked_job_counts: tuple[int, int] | None = None
        pool_directory = os.environ.get("VFY_MUTATION_WORKER_POOL")
        self.worker_pool = (
            WorkerPool(Path(pool_directory), int(os.environ["VFY_MUTATION_WORKERS"]))
            if pool_directory is not None
            else None
        )
        self.portable_workers = mutation_portable_worker_limit(
            self.workers, "file" if self.worker_pool is not None else "serial"
        )

    @staticmethod
    def _read_durations(path: str | None) -> dict[str, float]:
        return {} if path is None else mutation_test_durations(path)

    def _timeout_for(self, items: Sequence[pytest.Item]) -> float:
        """Apply the selected watchdog policy to the passing-test durations."""
        return mutation_timeout(
            [item.nodeid for item in items], self._durations, self.timeout, self.timeout_policy
        )

    @staticmethod
    def available() -> bool:
        """Whether it is safe to fork a collected pytest interpreter."""
        if os.environ.get("VFY_MUTATION_ENGINE", "auto").lower() == "portable":
            return False
        return os.name == "posix" and hasattr(os, "fork") and threading.active_count() == 1

    def _arguments(self) -> list[str]:
        arguments = list(self.execution_args)
        if "-q" not in arguments and "-v" not in arguments:
            arguments.append("-q")
        # Collect the full user-selected suite; contexts narrow mutation runs only.
        return arguments

    def run(self, mutations: Sequence[tuple[Mutation, MutationTestPlan]]) -> None:
        """Collect once and populate ``baseline``, ``outcomes`` and fallback."""
        # File isolation needs its matching verified baseline before this
        # controller can collect; portable and threaded routes also skip it.
        if (self.worker_pool is not None and not self.baseline_verified) or not self.available():
            # Without a new collection, recorded IDs may be stale. A fresh
            # process can safely run the requested scope without those IDs.
            self.plans = {
                mutation.key: MutationTestPlan(MutationTestScope.FULL_SUITE)
                for mutation, plan in mutations
                if plan.scope is MutationTestScope.SELECTED
            }
            if self.baseline_verified:
                self.baseline = RunnerOutcome(
                    "survived", returncode=0, detail="reused verified context baseline"
                )
            self.fallback.update(mutation.key for mutation, _ in mutations)
            return
        plugin = NativeMutationController(self, mutations)
        # pytest's terminal reporter otherwise emits dots and a summary from
        # the parent session, which would corrupt ``--format json`` output.
        previous_autoload = os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD")
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        try:
            self._prestart_portable(mutations)
            import pytest  # noqa: PLC0415 - start fresh jobs before loading the collected backend

            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                try:
                    completed = pytest.main(self._arguments(), plugins=[plugin])
                except Exception as error:  # noqa: BLE001 - pytest invokes project collection code
                    self._collection_error = f"pytest collection failed: {error}"
                    completed = pytest.ExitCode.INTERNAL_ERROR
        finally:
            try:
                self._discard_prestarted()
            finally:
                if previous_autoload is None:
                    os.environ.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
                else:
                    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = previous_autoload
        if completed == pytest.ExitCode.INTERRUPTED:
            raise KeyboardInterrupt
        self._finish_run(mutations, int(completed))

    def _finish_run(
        self, mutations: Sequence[tuple[Mutation, MutationTestPlan]], completed: int
    ) -> None:
        if self._session is None:
            detail = self._collection_error or "pytest did not create a runnable session"
            self.baseline = RunnerOutcome("error", detail=detail)
            self.fallback.update(mutation.key for mutation, _ in mutations)
            return
        detail = self._collection_error or (
            f"pytest stopped before returning a mutation result (exit code {completed})"
        )
        for mutation, _ in mutations:
            if mutation.key not in self.outcomes and mutation.key not in self.fallback:
                outcome = RunnerOutcome("error", returncode=completed, detail=detail)
                self.outcomes[mutation.key] = outcome
                self._emit(mutation.key, outcome)

    def _execute(
        self, session: pytest.Session, mutations: Sequence[tuple[Mutation, MutationTestPlan]]
    ) -> None:
        self._stop_prestart()
        if self._collection_error is not None:
            self.baseline = RunnerOutcome(
                "error", detail=f"pytest collection failed: {self._collection_error}"
            )
            self.fallback.update(mutation.key for mutation, _ in mutations)
            return
        self.uncollected = tuple(nodeid for nodeid in self.tests if nodeid not in self.items)
        recorded = frozenset(self.tests)
        # A test whose ID changed since the context baseline is among these.
        renamed = tuple(item for nodeid, item in self.items.items() if nodeid not in recorded)
        # A fresh pytest process can name those tests differently again, so
        # only the full suite is certain to include them.
        self.plans = {
            mutation.key: MutationTestPlan(MutationTestScope.FULL_SUITE)
            for mutation, plan in mutations
            if plan.scope is MutationTestScope.SELECTED
            and any(nodeid not in self.items for nodeid in plan.nodeids)
        }
        # Run the full collection for the clean baseline, including tests
        # without coverage contexts.
        clean_items = list(self.items.values())
        self._preload_fork_runtime(session)
        if threading.active_count() != 1:
            # Collection plug-ins can start threads after the initial fork-safety check.
            self.fallback.update(mutation.key for mutation, _ in mutations)
            if self.baseline_verified:
                self.baseline = RunnerOutcome(
                    "survived", returncode=0, detail="reused verified context baseline"
                )
                self._run_jobs(session, ())
            return
        if self.baseline_verified:
            self.baseline = RunnerOutcome(
                "survived", returncode=0, detail="reused verified context baseline"
            )
        else:
            baseline = self._fork_job(
                session,
                None,
                clean_items,
                timeout=mutation_baseline_timeout(self.timeout),
            )
            # A short mutation timeout must not make a healthy baseline fail.
            self.baseline = (
                baseline
                if isinstance(baseline, RunnerOutcome)
                else self._finish_single(baseline, timeout=mutation_baseline_timeout(self.timeout))
            )
        if self.baseline.status != "survived":
            # Without a passing baseline, the caller marks pending mutations unstable.
            self.fallback.update(mutation.key for mutation, _ in mutations)
            return

        jobs = [
            job
            for mutation, plan in mutations
            if (job := self._plan_job(mutation, plan, renamed)) is not None
        ]
        # Fork from the unexecuted parent so children cannot share mutated
        # pytest or module state.
        self._run_jobs(session, jobs)

    def _plan_job(
        self, mutation: Mutation, plan: MutationTestPlan, renamed: Sequence[pytest.Item]
    ) -> _MutationJobSpec | None:
        if plan.scope is MutationTestScope.UNREACHED:
            outcome = RunnerOutcome("unreached")
            self.outcomes[mutation.key] = outcome
            self._emit(mutation.key, outcome)
            return None
        if plan.scope is MutationTestScope.SELECTED and getattr(mutation, "fast_eligible", False):
            selected = [self.items.get(nodeid) for nodeid in plan.nodeids]
            if mutation.key in self.plans:
                # The collected session can run every test whose ID is new.
                selected.extend(renamed)
            items = tuple(dict.fromkeys(item for item in selected if item is not None))
            if items:
                if mutation.key in self.plans:
                    self.report_plans[mutation.key] = MutationTestPlan(
                        MutationTestScope.SELECTED, tuple(item.nodeid for item in items)
                    )
                return _MutationJobSpec(mutation, _MutationJobKind.COLLECTED, items)
            outcome = RunnerOutcome(
                "error", detail="no collected test matches the recorded coverage contexts"
            )
            self.outcomes[mutation.key] = outcome
            self._emit(mutation.key, outcome)
            return None
        # Defaults, decorators, module assignments, and class bodies run before
        # test contexts; they and mutations without an exact code-object
        # replacement run in a fresh pytest process.
        self.fallback.add(mutation.key)
        if self.portable_starter is None:
            return None
        plan = self.plans.get(mutation.key, plan)
        return _MutationJobSpec(mutation, _MutationJobKind.PORTABLE, plan=plan)

    @staticmethod
    def _preload_fork_runtime(session: pytest.Session) -> None:
        if threading.active_count() != 1:
            return
        asyncio_plugin = sys.modules.get("vfy.asyncio_plugin")
        if asyncio_plugin is not None and session.config.pluginmanager.is_registered(
            asyncio_plugin
        ):
            from .asyncio_plugin import (  # noqa: PLC0415 - share runtime modules after collection
                _preload_fork_runtime,
            )

            _preload_fork_runtime()

    def _fork_job(
        self,
        session: pytest.Session,
        mutation: Mutation | None,
        items: Sequence[pytest.Item],
        *,
        timeout: float | None = None,
        lease_fd: int | None = None,
        isolate_files: bool = False,
    ) -> _Job | RunnerOutcome:
        started = time.monotonic()
        try:
            read_fd, write_fd, pid = _fork_pipe()
        except _FastMutationError as error:
            return RunnerOutcome("error", detail=str(error), fallback=mutation is not None)
        if pid == 0:  # pragma: no cover - exercised by integration tests in a child
            os.close(read_fd)
            for fd in self._pidfds:
                with contextlib.suppress(OSError):
                    os.close(fd)
            self._pidfds.clear()
            if self.worker_pool is not None:
                self.worker_pool.after_fork()
            try:
                with contextlib.suppress(OSError):
                    os.setsid()
                os.environ["VFY_MUTATION_JOB_GROUP"] = str(os.getpid())
                # Keep structured protocol output private.  pytest's capture
                # machinery still records assertion details for its hooks.
                devnull = os.open(os.devnull, os.O_RDWR)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                if mutation is not None:
                    self._apply_fast_mutation(mutation)
                    outcome = self._run_items(session, items, isolate_files=isolate_files)
                else:
                    outcome = self._run_items(session, items)
                self._write_child(write_fd, outcome)
            except _FastMutationError as error:
                self._write_child(
                    write_fd,
                    RunnerOutcome(
                        "error",
                        detail=str(error),
                        fallback=True,
                    ),
                )
            except Exception as error:  # noqa: BLE001 - preserve a structured child result
                self._write_child(
                    write_fd,
                    RunnerOutcome(
                        "error",
                        detail=f"native mutation child crashed: {type(error).__name__}: {error}",
                    ),
                )
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        job = _Job(
            pid,
            read_fd,
            mutation,
            started,
            self.timeout if timeout is None else timeout,
            lease_fd=lease_fd,
        )
        try:
            job.exit_fd = self._open_exit_fd(pid)
        except BaseException:
            try:
                self._kill(pid)
                if self.worker_pool is not None:
                    terminate_file_groups(self.worker_pool.directory, pid)
            finally:
                self._close_job_descriptors(job)
            raise
        return job

    def _open_exit_fd(self, pid: int) -> int | None:
        if not hasattr(os, "pidfd_open"):
            return None
        try:
            fd = os.pidfd_open(pid)
        except OSError:
            return None
        try:
            self._pidfds.add(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _write_child(fd: int, outcome: RunnerOutcome) -> None:
        payload = json.dumps(
            {
                "status": outcome.status,
                "killer": outcome.killer,
                "duration_ms": outcome.duration_ms,
                "returncode": outcome.returncode,
                "detail": outcome.detail,
                "fallback": outcome.fallback,
                "phase": outcome.phase,
            },
            separators=(",", ":"),
        ).encode()
        remaining = memoryview(payload)
        while remaining:
            remaining = remaining[os.write(fd, remaining) :]

    def _run_items(
        self,
        session: pytest.Session,
        items: Sequence[pytest.Item],
        *,
        isolate_files: bool = False,
    ) -> RunnerOutcome:
        started = time.monotonic()
        session.testsfailed = 0
        session.shouldstop = False
        try:
            if isolate_files:
                from .mutation_parallel import (  # noqa: PLC0415 - opt-in pytest execution hook
                    run_file_groups,
                )

                group_result = run_file_groups(session, items)
                if group_result.status != "completed":
                    return RunnerOutcome(
                        group_result.status,
                        duration_ms=group_result.duration_seconds * 1000.0,
                        detail=group_result.detail,
                    )
                result = group_result.native_result
                if result is None:
                    return RunnerOutcome("error", detail="native file groups returned no result")
            else:
                from .native import _pytest  # noqa: PLC0415 - native execution is platform-gated

                result = _pytest.run_native_suite(
                    list(items),
                    capture=session.config.getoption("capture", "fd") != "no",
                    maxfail=1,
                )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:  # noqa: BLE001 - pytest hooks are user code
            return RunnerOutcome(
                "error",
                duration_ms=(time.monotonic() - started) * 1000.0,
                detail=f"native execution failed: {type(error).__name__}: {error}",
            )
        duration = (time.monotonic() - started) * 1000.0
        failure = result.mutation_failure()
        if failure is not None:
            nodeid, phase = failure
            return RunnerOutcome("killed", nodeid, duration, 1, phase=phase)
        return RunnerOutcome("survived", duration_ms=duration, returncode=0)

    def _start_job(
        self, session: pytest.Session, spec: _MutationJobSpec, portable_lease: int | None
    ) -> _Job | RunnerOutcome | None:
        if spec.kind is _MutationJobKind.PORTABLE:
            return self._start_portable_job(spec, portable_lease)
        lease_fd = None
        isolate_files = False
        if self.worker_pool is not None:
            isolate_files = (
                len(plan_mutation_test_groups([item.nodeid for item in spec.items], [])) > 1
            )
            if not isolate_files:
                lease_fd = self.worker_pool.acquire()
                if lease_fd is None:
                    return None
        try:
            job = self._fork_job(
                session,
                spec.mutation,
                spec.items,
                timeout=self._timeout_for(spec.items),
                lease_fd=lease_fd,
                isolate_files=isolate_files,
            )
            if (
                isinstance(job, RunnerOutcome)
                and lease_fd is not None
                and self.worker_pool is not None
            ):
                self.worker_pool.release(lease_fd)
            return job
        except BaseException:
            if lease_fd is not None and self.worker_pool is not None:
                self.worker_pool.release(lease_fd)
            raise

    def _start_portable_job(
        self, spec: _MutationJobSpec, lease_fd: int | None
    ) -> _Job | RunnerOutcome:
        try:
            starter = self.portable_starter
            if starter is None or spec.plan is None:
                raise RuntimeError("portable mutation starter is unavailable")
            started = time.monotonic()
            process = starter(spec.mutation, spec.plan, lease_fd)
        finally:
            # A started supervisor keeps its inherited copy of the lease while
            # it collects. Closing the controller's copy leaves the lock with
            # that process, or frees it when no process started.
            if lease_fd is not None and self.worker_pool is not None:
                self.worker_pool.release(lease_fd)
        if isinstance(process, RunnerOutcome):
            return process
        job = _Job(
            process.pid,
            None,
            spec.mutation,
            started,
            self.timeout,
            _MutationJobKind.PORTABLE,
            portable=process,
        )
        try:
            job.exit_fd = self._open_exit_fd(process.pid)
        except BaseException:
            process.close()
            raise
        return job

    def _prestart_portable(self, mutations: Sequence[tuple[Mutation, MutationTestPlan]]) -> None:
        if not self.baseline_verified or self.portable_starter is None:
            return
        from .native import _pytest  # noqa: PLC0415 - optional native process watchdog

        if not _pytest.MutationProcessWatchdog.available():
            return
        # The optional watchdog bounds one acquisition batch; later jobs still
        # use the requested public worker capacity in the ordinary scheduler.
        prestart_limit = min(self.portable_workers, 256)
        for mutation, plan in mutations:
            if len(self._prestart_keys) >= prestart_limit:
                break
            if plan.scope is not MutationTestScope.FULL_SUITE:
                continue
            lease_fd = None
            if self.worker_pool is not None:
                # Supervisor collection counts against the file-worker capacity.
                lease_fd = self.worker_pool.acquire()
                if lease_fd is None:
                    break
            spec = _MutationJobSpec(mutation, _MutationJobKind.PORTABLE, plan=plan)
            started = time.monotonic()
            job = self._start_portable_job(spec, lease_fd)
            self._prestart_keys.add(mutation.key)
            if isinstance(job, RunnerOutcome):
                self._prestart_completed.append(
                    (
                        _Job(0, None, mutation, started, self.timeout, _MutationJobKind.PORTABLE),
                        job,
                    )
                )
            else:
                self._prestarted[job.pid] = job
        if self._prestarted:
            now = time.monotonic()
            self._prestart_watchdog = _pytest.MutationProcessWatchdog(
                [
                    (job.pid, max(0.0, job.timeout - (now - job.started)))
                    for job in self._prestarted.values()
                ],
                str(self.worker_pool.directory) if self.worker_pool is not None else None,
            )

    def _stop_prestart(self) -> None:
        if self._prestart_watchdog is not None:
            self._prestart_expired.update(self._prestart_watchdog.finish())
            self._prestart_watchdog = None

    def _discard_prestarted(self) -> None:
        try:
            self._stop_prestart()
        finally:
            for job in self._prestarted.values():
                self._release_job(job)
            self._prestarted.clear()
            self._prestart_completed.clear()
            self._prestart_keys.clear()
            self._prestart_expired.clear()

    def _release_job(self, job: _Job) -> None:
        try:
            if job.portable is not None:
                job.portable.close()
            if self.worker_pool is not None:
                terminate_file_groups(self.worker_pool.directory, job.pid)
            if job.lease_fd is not None and self.worker_pool is not None:
                self.worker_pool.release(job.lease_fd)
                job.lease_fd = None
        finally:
            self._close_job_descriptors(job)

    def _close_job_descriptors(self, job: _Job) -> None:
        _close_payload_pipe(job)
        fd = job.exit_fd
        job.exit_fd = None
        if fd is not None:
            self._pidfds.discard(fd)
            with contextlib.suppress(OSError):
                os.close(fd)

    def _launch_jobs(
        self,
        session: pytest.Session,
        pending: list[_MutationJobSpec],
        active: dict[int, _Job],
    ) -> None:
        if self._blocked_job_counts == (len(pending), len(active)):
            return
        isolation: Literal["serial", "file"] = "file" if self.worker_pool is not None else "serial"
        while pending:
            pending_kinds: list[_JobKindName] = [spec.kind.value for spec in pending]
            active_kinds: list[_JobKindName] = [job.kind.value for job in active.values()]
            index = next_mutation_job(
                pending_kinds, active_kinds, self.workers, isolation, "available"
            )
            portable_lease = None
            if (
                index is not None
                and self.worker_pool is not None
                and pending[index].kind is _MutationJobKind.PORTABLE
            ):
                # A fresh supervisor collects under one file-worker lease.
                portable_lease = self.worker_pool.acquire()
                if portable_lease is None:
                    index = next_mutation_job(
                        pending_kinds, active_kinds, self.workers, isolation, "exhausted"
                    )
                    if index is None:
                        # Leases free up without changing the job counts, so
                        # retry on the next poll instead of caching this state.
                        break
            if index is None:
                # A launch consumes a pending job, so unchanged counts imply
                # unchanged inputs to the Rust scheduling decision.
                self._blocked_job_counts = len(pending), len(active)
                break
            job = self._start_job(session, pending[index], portable_lease)
            if job is None:
                break
            spec = pending.pop(index)
            if isinstance(job, RunnerOutcome):
                self._record(
                    _Job(0, None, spec.mutation, time.monotonic(), self.timeout, spec.kind), job
                )
                continue
            active[job.pid] = job

    def _poll_job(self, job: _Job) -> RunnerOutcome | None:
        if job.portable is not None:
            return job.portable.poll()
        status = _poll_child(job)
        if status is None:
            return None
        with contextlib.suppress(OSError):
            os.killpg(job.pid, signal.SIGKILL)
        return self._completed_outcome(job, status)

    def _overdue_outcome(self, job: _Job) -> RunnerOutcome:
        """Classify a job whose deadline passed before the controller checked it.

        A delayed controller, for example after a slow fork, can reach the
        deadline check after a child has already exited. That child keeps its
        result; a collected child's own measured duration must still fit the
        deadline. Only a child that is still running is killed as a timeout.
        """
        outcome = self._poll_job(job)
        if outcome is None:
            if job.portable is None:
                self._kill(job.pid)
        elif job.portable is not None or outcome.duration_ms <= job.timeout * 1000.0:
            return outcome
        return RunnerOutcome(
            "timeout",
            duration_ms=job.timeout * 1000.0,
            detail="native mutation execution timed out",
        )

    def _run_jobs(self, session: pytest.Session, jobs: Sequence[_MutationJobSpec]) -> None:
        self._blocked_job_counts = None
        order = order_mutation_jobs([job.kind.value for job in jobs])
        pending = [
            jobs[index] for index in order if jobs[index].mutation.key not in self._prestart_keys
        ]
        active = self._prestarted
        self._prestarted = {}
        self._prestart_keys.clear()
        try:
            self._record_prestarted(active)
            self._launch_jobs(session, pending, active)
            while active or pending:
                self._launch_jobs(session, pending, active)
                now = time.monotonic()
                for job in list(active.values()):
                    if now - job.started <= job.timeout:
                        continue
                    outcome = self._overdue_outcome(job)
                    self._release_job(job)
                    self._record(job, outcome)
                    active.pop(job.pid, None)
                    self._launch_jobs(session, pending, active)
                if not active:
                    select.select([], [], [], 0.01)
                    continue
                _wait_jobs(list(active.values()))
                # A child can crash before writing its protocol payload, leaving
                # no readable descriptor.  Reap it and expose a hard error.
                for job in list(active.values()):
                    outcome = self._poll_job(job)
                    if outcome is None:
                        continue
                    self._record(job, outcome)
                    active.pop(job.pid, None)
                    self._release_job(job)
                    self._launch_jobs(session, pending, active)
        finally:
            for job in active.values():
                if job.portable is None:
                    self._kill(job.pid)
                self._release_job(job)
            if self.worker_pool is not None:
                self.worker_pool.close()

    def _record_prestarted(self, active: dict[int, _Job]) -> None:
        for job, outcome in self._prestart_completed:
            self._record(job, outcome)
        self._prestart_completed.clear()
        # Watched children remain unreaped until the native watchdog joins.
        # Read completed results before applying the ordinary scheduler's
        # elapsed-time check, which also includes parent collection time.
        for job in list(active.values()):
            if job.pid in self._prestart_expired:
                outcome = RunnerOutcome(
                    "timeout",
                    duration_ms=job.timeout * 1000.0,
                    detail="native mutation execution timed out",
                )
            else:
                outcome = self._poll_job(job)
                if outcome is None:
                    continue
            self._release_job(job)
            self._record(job, outcome)
            active.pop(job.pid)
        self._prestart_expired.clear()

    def _completed_outcome(self, job: _Job, status: int) -> RunnerOutcome:
        if job.payload:
            return self._decode(job.payload)
        detail = "pytest child exited without a structured result"
        if os.WIFSIGNALED(status):
            detail = f"pytest child terminated by signal {os.WTERMSIG(status)}"
        return RunnerOutcome("error", detail=detail)

    @staticmethod
    def _kill(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(pid, 0)

    @staticmethod
    def _decode(payload: bytes | bytearray) -> RunnerOutcome:
        try:
            value = json.loads(payload.decode())
            return RunnerOutcome(
                _result_status(value.get("status")),
                None if value.get("killer") is None else str(value["killer"]),
                float(value.get("duration_ms", 0.0)),
                None if value.get("returncode") is None else int(value["returncode"]),
                str(value.get("detail", "")),
                bool(value.get("fallback", False)),
                None if value.get("phase") is None else str(value["phase"]),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return RunnerOutcome("error", detail="pytest child returned malformed protocol data")

    def _finish_single(self, job: _Job, *, timeout: float | None = None) -> RunnerOutcome:
        timeout = self.timeout if timeout is None else timeout
        deadline = job.started + timeout
        reaped = False
        try:
            while time.monotonic() < deadline:
                _wait_jobs([job], deadline=deadline)
                status = _poll_child(job)
                if status is None:
                    continue
                reaped = True
                if job.payload:
                    return self._decode(job.payload)
                detail = "pytest baseline child exited without a structured result"
                if os.WIFSIGNALED(status):
                    detail = f"pytest baseline terminated by signal {os.WTERMSIG(status)}"
                return RunnerOutcome("error", detail=detail)
            return RunnerOutcome(
                "timeout",
                duration_ms=timeout * 1000.0,
                detail="native mutation execution timed out",
            )
        finally:
            if not reaped:
                self._kill(job.pid)
            self._close_job_descriptors(job)

    def _record(self, job: _Job, outcome: RunnerOutcome) -> None:
        mutation = job.mutation
        if mutation is None:
            self.baseline = outcome
            return
        elapsed = outcome.duration_ms or (time.monotonic() - job.started) * 1000.0
        outcome = RunnerOutcome(
            outcome.status,
            outcome.killer,
            elapsed,
            outcome.returncode,
            outcome.detail,
            outcome.fallback,
            outcome.phase,
        )
        if job.kind is _MutationJobKind.PORTABLE:
            self.fallback.add(mutation.key)
            self.portable_outcomes[mutation.key] = outcome
            self._emit(mutation.key, outcome)
        elif outcome.fallback:
            self.fallback.add(mutation.key)
        else:
            self.outcomes[mutation.key] = outcome
            self._emit(mutation.key, outcome)

    def _emit(self, key: str, outcome: RunnerOutcome) -> None:
        if self.on_result is not None:
            self.on_result(key, outcome)

    @staticmethod
    def _apply_fast_mutation(mutation: Mutation) -> FunctionType:
        try:
            function = native._pytest.install_mutation_code(json.dumps(mutation.as_dict()))
        except (AttributeError, OSError, TypeError, ValueError, SyntaxError, UnicodeError) as error:
            raise _FastMutationError(f"invalid mutation: {error}") from error
        if function is None:
            raise _FastMutationError(
                "mutation callable identity is not eligible for code replacement"
            )
        return function


__all__ = ["CollectedMutationRunner", "RunnerOutcome"]
