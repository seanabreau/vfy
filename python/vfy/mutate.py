"""Run pytest against source mutations planned by Rust."""

from __future__ import annotations

import argparse
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, redirect_stdout, suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from ._pytest.plugin_support import disable_plugin_autoload
from ._version import __version__
from .config_types import Config
from .data import CoverageStore
from .errors import DataError, NoDataError, VfyError
from .mutation import MutationSpec, _patch_bytes
from .mutation_runner import (
    CollectedMutationRunner,
    MutationTestPlan,
    MutationTestScope,
    PortableMutationProcess,
    RunnerOutcome,
    mutation_pytest_args,
    mutation_traceback_args,
    validate_mutation_pytest_args,
)
from .mutation_workers import (
    INHERITED_LEASE_ENVIRONMENT_NAME,
    isolated_worker_pool,
    serial_worker_scope,
    terminate_file_groups,
)
from .native import (
    NativeTestInvocation,
    build_mutation_report,
    git_replay_state,
    green_test_run_valid,
    mutation_baseline_timeout,
    mutation_baseline_valid,
    mutation_contexts,
    mutation_executed_paths,
    mutation_file_digest,
    mutation_fingerprints,
    mutation_sources,
    mutation_suite_state,
    mutation_test_preferences,
    plan_mutation_tests,
    read_mutation_cache,
    render_mutation_report,
    select_mutation_catalog,
    select_mutation_sources,
    source_tree_identity,
    write_mutation_baseline,
    write_mutation_cache,
    write_report_output,
    write_test_collection,
)
from .native_types import (
    MutationBaselineCoveragePolicy,
    MutationExecutionPhase,
    MutationOwner,
    MutationRecord,
    MutationStatus,
    MutationTimeoutPolicy,
    PytestConfigRecord,
)
from .output import OutputPolicy
from .run.engine import (
    TestEngine,
    _native_runner_unavailability_reason,
    _native_xdist_plugin_args,
    _resolve_pytest_configuration,
)
from .run.record import (
    GREEN_FINGERPRINT_KEY,
    GREEN_HISTORY_DIGEST_KEY,
    GREEN_IGNORED_PATHS_KEY,
    GREEN_SOURCE_IDENTITY_KEY,
    GREEN_SOURCE_ROOTS_KEY,
    execution_fingerprint,
    green_test_run_path,
    native_execution_fingerprint,
)
from .source_roots import mutation_source_paths

if TYPE_CHECKING:
    from ._pytest_native import NativeIndependentSession, NativeMutationPublication, NativeRunResult
    from .native import CoverageData as NativeCoverageData
    from .run.engine import (
        _PytestConfiguration,
    )

type MutationCacheCollection = NativeRunResult | NativeIndependentSession


# Batch snapshots to limit full-document rewrites. A crash loses at most one
# batch; KeyboardInterrupt writes completed results immediately.
_CACHE_FLUSH_INTERVAL = 128


class MutationOwnerRecord(TypedDict):
    kind: MutationOwner
    qualname: str
    start: int
    end: int
    fast_eligible: bool
    phase: MutationExecutionPhase
    body_start: int | None


class MutationInputRecord(TypedDict):
    id: str
    path: str
    source_hash: str
    kind: str
    operator: str
    original: str
    replacement: str
    start: int
    end: int
    line: int
    column: int
    owner: MutationOwnerRecord


class MutationCacheRecord(MutationInputRecord):
    status: MutationStatus
    tests: list[str]
    killer: str | None
    duration_ms: float
    returncode: int | None
    detail: str
    phase: NotRequired[str]


class MutationScopeRecord(TypedDict):
    requested_base: str
    base_commit: str
    merge_base_commit: str
    selected_files: list[str]


class MutationReportRecord(TypedDict):
    schema: str
    fingerprint: str
    summary: dict[str, int]
    mutations: list[MutationCacheRecord]
    metrics: NotRequired[dict[str, float]]
    scope: NotRequired[MutationScopeRecord]


@dataclass(frozen=True)
class Mutation:
    key: str
    path: str
    source_hash: str
    kind: str
    operator: str
    original: str
    replacement: str
    start: int
    end: int
    line: int
    column: int
    owner_kind: MutationOwner = "module"
    owner_qualname: str = ""
    owner_start: int = 0
    owner_end: int = 0
    fast_eligible: bool = False
    execution_phase: MutationExecutionPhase = "body"
    owner_body_start: int | None = None

    @classmethod
    def from_rust(cls, value: MutationRecord) -> Mutation:
        owner = cast(MutationOwnerRecord, json.loads(value[11]))
        return cls(
            *value[:11],
            owner["kind"],
            owner["qualname"],
            owner["start"],
            owner["end"],
            owner["fast_eligible"],
            owner["phase"],
            owner["body_start"],
        )

    def spec(self) -> MutationSpec:
        return MutationSpec(
            self.path,
            self.source_hash,
            self.start,
            self.end,
            self.original,
            self.replacement,
        )

    def as_dict(self) -> MutationInputRecord:
        return {
            "id": self.key,
            "path": self.path,
            "source_hash": self.source_hash,
            "kind": self.kind,
            "operator": self.operator,
            "original": self.original,
            "replacement": self.replacement,
            "start": self.start,
            "end": self.end,
            "line": self.line,
            "column": self.column,
            "owner": {
                "kind": self.owner_kind,
                "qualname": self.owner_qualname,
                "start": self.owner_start,
                "end": self.owner_end,
                "fast_eligible": self.fast_eligible,
                "phase": self.execution_phase,
                "body_start": self.owner_body_start,
            },
        }


@dataclass(frozen=True)
class MutationResult:
    mutation: Mutation
    status: MutationStatus
    tests: tuple[str, ...] = ()
    killer: str | None = None
    duration_ms: float = 0.0
    returncode: int | None = None
    detail: str = ""
    phase: str | None = None
    _native_result: NativeRunResult | None = field(default=None, repr=False, compare=False)

    def as_dict(self) -> MutationCacheRecord:
        value: MutationCacheRecord = {
            **self.mutation.as_dict(),
            "status": self.status,
            "tests": list(self.tests),
            "killer": self.killer,
            "duration_ms": round(self.duration_ms, 3),
            "returncode": self.returncode,
            "detail": self.detail,
        }
        if self.phase is not None:
            value["phase"] = self.phase
        return value


@dataclass(frozen=True)
class MutationReport:
    fingerprint: str
    results: tuple[MutationResult, ...]
    metrics: Mapping[str, float] = field(default_factory=dict)
    scope: str | None = None

    @property
    def summary(self) -> dict[str, int]:
        return self.as_dict()["summary"]

    def as_dict(self) -> MutationReportRecord:
        return cast(MutationReportRecord, json.loads(self._as_json()))

    def _as_json(self) -> str:
        return build_mutation_report(
            self.fingerprint,
            json.dumps([result.as_dict() for result in self.results]),
            json.dumps(dict(self.metrics)),
            self.scope,
        )


def _source_files(roots: Iterable[str]) -> list[Path]:
    resolved = [root for root in mutation_source_paths(roots) if Path(root).exists()]
    return [Path(path) for path in mutation_sources(resolved)]


def _pytest_environment_state(engine: TestEngine) -> tuple[tuple[str, str], ...]:
    """Capture environment knobs that can change collection or test meaning."""
    names = (
        "PYTEST_ADDOPTS",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "PYTEST_PLUGINS",
        "VFY_MUTATION_TEST_ISOLATION",
        "VFY_MUTATION_WORKERS",
    )
    if engine is TestEngine.INDEPENDENT:
        runner_state = (
            ("<vfy-version>", __version__),
            ("<execution-engine>", "vfy-independent"),
        )
    else:
        try:
            import pytest  # noqa: PLC0415 - optional pytest metadata must not prevent importing the API

            pytest_version = str(getattr(pytest, "__version__", "unknown"))
        except Exception:  # noqa: BLE001 - a missing optional pytest import is still hashable
            pytest_version = "unavailable"
        runner_state = (
            ("<pytest-version>", pytest_version),
            ("<execution-engine>", "vfy-native"),
        )
    environment = tuple((f"<env:{name}>", os.environ.get(name, "")) for name in names)
    return (*environment, *runner_state)


def _rank_mutation_tests(
    selected: Sequence[Mutation],
    by_location: Mapping[str, set[str]],
    executed_paths: set[str],
    history_path: str,
    omitted_paths: Sequence[str],
    *,
    collection_data: NativeCoverageData | None = None,
) -> dict[str, MutationTestPlan]:
    return {
        key: MutationTestPlan(MutationTestScope(scope), tuple(nodeids))
        for key, (scope, nodeids) in plan_mutation_tests(
            [
                (
                    mutation.key,
                    mutation.path,
                    mutation.line,
                    mutation.execution_phase,
                )
                for mutation in selected
            ],
            dict(by_location),
            executed_paths,
            history_path,
            list(omitted_paths),
            collection_data=collection_data,
            mutation_inputs=json.dumps([mutation.as_dict() for mutation in selected])
            if collection_data is not None
            else None,
        ).items()
    }


def _fingerprint_request(
    mutations: Sequence[Mutation],
    tests: Sequence[str],
    pytest_args: Sequence[str],
    timeout: float,
    evidence: Mapping[str, MutationTestPlan],
    suite_state: Sequence[tuple[str, str]],
    timeout_policy: MutationTimeoutPolicy = "adaptive",
) -> str:
    return json.dumps(
        {
            "version": __version__,
            "implementation": sys.implementation.name,
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "mutations": [mutation.as_dict() for mutation in mutations],
            "tests": list(tests),
            "pytest_args": list(pytest_args),
            "timeout": str(timeout),
            "timeout_policy": timeout_policy,
            "evidence": {key: list(plan.nodeids) for key, plan in evidence.items()},
            "scopes": {key: plan.scope.value for key, plan in evidence.items()},
            "suite_state": list(suite_state),
        }
    )


def _cache_path(_path: str) -> Path:
    """Return the project-local cache, independent of the coverage basename."""
    return Path(".vfy/mutations.json").resolve()


def _baseline_manifest_path() -> Path:
    return Path(".vfy/mutation-baseline.json").resolve()


def _replay_baseline_path(history_path: str) -> Path:
    return Path(history_path).resolve().parent / "replay.coverage"


def _metadata_paths(value: str | None) -> list[str] | None:
    if value is None:
        return None
    try:
        paths = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        return None
    return [str(path) for path in paths]


def _shared_passing_test_run(
    history_path: str,
    expected_execution_fingerprint: str,
) -> CoverageStore | None:
    """Load a current successful test run without preparing mutation state."""

    if os.environ.get("VFY_MUTATION_TEST_ISOLATION") == "file":
        # Ordinary test runs do not prove the explicitly isolated fixture scope.
        return None

    replay_path = _replay_baseline_path(history_path)
    try:
        store = CoverageStore.read(replay_path)
        valid_artifact = green_test_run_valid(
            str(green_test_run_path(history_path)), str(replay_path)
        )
    except (FileNotFoundError, DataError, OSError):
        return None
    if not valid_artifact:
        return None
    metadata = store.metadata
    if metadata.get(GREEN_FINGERPRINT_KEY) != expected_execution_fingerprint:
        return None
    if metadata.get(GREEN_HISTORY_DIGEST_KEY) != mutation_file_digest(history_path):
        return None
    source_identity = metadata.get(GREEN_SOURCE_IDENTITY_KEY)
    roots = _metadata_paths(metadata.get(GREEN_SOURCE_ROOTS_KEY))
    ignored = _metadata_paths(metadata.get(GREEN_IGNORED_PATHS_KEY))
    if source_identity is None or roots is None or ignored is None:
        return None
    if source_identity.startswith("git:"):
        try:
            state = git_replay_state(ignored)
        except RuntimeError:
            return None
        if not state["clean"] or source_identity != f"git:{state['head']}":
            return None
    elif source_identity.startswith("content:"):
        try:
            current = source_tree_identity(roots, ignored)
        except RuntimeError:
            return None
        if source_identity != f"content:{current}":
            return None
    else:
        return None
    return store


def _baseline_request(
    tests: Sequence[str],
    pytest_args: Sequence[str],
    suite_state: Sequence[tuple[str, str]],
    data_file: str | None,
    data_hash: str | None = None,
) -> str:
    return json.dumps(
        {
            "tests": list(tests),
            "pytest_args": list(pytest_args),
            "suite_state": list(suite_state),
            "data_file": data_file,
            "data_hash": data_hash
            if data_hash is not None
            else mutation_file_digest(data_file)
            if data_file
            else None,
        }
    )


def _baseline_manifest_valid(
    tests: Sequence[str],
    pytest_args: Sequence[str],
    suite_state: Sequence[tuple[str, str]],
    *,
    data_file: str | None = None,
    data_hash: str | None = None,
    manifest_path: Path | None = None,
    publication: NativeMutationPublication | None = None,
) -> bool:
    request = _baseline_request(tests, pytest_args, suite_state, data_file, data_hash)
    if publication is not None:
        return publication.baseline_valid(request)
    return mutation_baseline_valid(
        str(manifest_path if manifest_path is not None else _baseline_manifest_path()),
        request,
    )


def _write_baseline_manifest(
    tests: Sequence[str],
    pytest_args: Sequence[str],
    suite_state: Sequence[tuple[str, str]],
    *,
    data_file: str | None = None,
    data_hash: str | None = None,
    manifest_path: Path | None = None,
    publication: NativeMutationPublication | None = None,
) -> None:
    request = _baseline_request(tests, pytest_args, suite_state, data_file, data_hash)
    if publication is not None:
        publication.write_baseline(request)
        return
    write_mutation_baseline(
        str(manifest_path if manifest_path is not None else _baseline_manifest_path()),
        request,
    )


def _result_from_dict(item: MutationCacheRecord) -> MutationResult:
    owner = item["owner"]
    mutation = Mutation(
        item["id"],
        item["path"],
        item["source_hash"],
        item["kind"],
        item["operator"],
        item["original"],
        item["replacement"],
        item["start"],
        item["end"],
        item["line"],
        item["column"],
        owner["kind"],
        owner["qualname"],
        owner["start"],
        owner["end"],
        owner["fast_eligible"],
        owner["phase"],
        owner["body_start"],
    )
    return MutationResult(
        mutation,
        item["status"],
        tuple(item["tests"]),
        item["killer"],
        item["duration_ms"],
        item["returncode"],
        item["detail"],
        item.get("phase"),
    )


def _read_cache(
    path: Path,
    fingerprint: str,
    mutation_fingerprints: Mapping[str, str] | None = None,
    *,
    fresh_collection: MutationCacheCollection | None = None,
    request: str = "",
) -> dict[str, MutationResult]:
    records = cast(
        dict[str, MutationCacheRecord],
        json.loads(
            fresh_collection.read_fresh_mutation_cache(str(path), request)
            if fresh_collection is not None
            else read_mutation_cache(
                str(path),
                fingerprint,
                None if mutation_fingerprints is None else dict(mutation_fingerprints),
            )
        ),
    )
    return {key: _result_from_dict(item) for key, item in records.items()}


def _write_cache(
    path: Path,
    report: MutationReport,
    mutation_fingerprints: Mapping[str, str] | None = None,
) -> None:
    write_mutation_cache(
        str(path),
        report._as_json(),
        None if mutation_fingerprints is None else dict(mutation_fingerprints),
    )


def _pytest_command(
    execution_args: Sequence[str], spec: MutationSpec | None
) -> tuple[list[str], dict[str, str]]:
    arguments = list(execution_args)
    arguments.append("-x")
    arguments[0:0] = ("-p", "vfy.mutation")
    if os.environ.get("VFY_MUTATION_TEST_ISOLATION") == "file":
        arguments[0:0] = ("-p", "vfy.mutation_parallel")
    bootstrap = str(Path(__file__).resolve().parent / "bootstrap")
    existing = os.environ.get("PYTHONPATH")
    pythonpath = f"{bootstrap}{os.pathsep}{existing}" if existing else bootstrap
    environment = os.environ.copy()
    for name in (
        "COVERAGE_FILE",
        "VFY_CLI_ACTIVE",
        "VFY_PROCESS_OPTIONS",
        "VFY_PROCESS_START",
        "VFY_REPLAY_MERGE",
        "VFY_MUTATION",
        "VFY_MUTATION_BASELINE_COVERAGE",
        "VFY_MUTATION_SELECTED_TESTS",
    ):
        environment.pop(name, None)
    environment["PYTHONPATH"] = pythonpath
    disable_plugin_autoload(environment)
    if spec is not None:
        environment["VFY_MUTATION"] = json.dumps(
            {
                "path": spec.path,
                "source_hash": spec.source_hash,
                "start": spec.start,
                "end": spec.end,
                "original": spec.original,
                "replacement": spec.replacement,
            },
            separators=(",", ":"),
        )
    return [sys.executable, "-m", "pytest", *arguments], environment


def _run_once(
    tests: Sequence[str],
    execution_args: Sequence[str],
    candidate: Mutation,
    timeout: float,
    mutation: MutationSpec | None = None,
    preferred_tests: Sequence[str] = (),
) -> RunnerOutcome:
    started = time.monotonic()
    if mutation is not None:
        try:
            source = Path(candidate.path).read_bytes()
            compile(_patch_bytes(source, mutation), candidate.path, "exec", dont_inherit=True)
        except (OSError, UnicodeError, ImportError, SyntaxError) as error:
            return RunnerOutcome(
                "invalid", duration_ms=(time.monotonic() - started) * 1000.0, detail=str(error)
            )
    command, environment = _pytest_command(execution_args, mutation)
    environment["VFY_MUTATION_PREFERRED_TESTS"] = json.dumps(list(preferred_tests))
    try:
        with TemporaryDirectory(prefix="vfy-mutation-result-") as directory:
            result_path = Path(directory) / "native.json"
            environment["PYTEST_NATIVE_RESULT"] = str(result_path)
            if tests:
                selected_path = Path(directory) / "selected-tests.json"
                write_test_collection(str(selected_path), list(tests))
                environment["VFY_MUTATION_SELECTED_TESTS"] = str(selected_path)
            completed = _run_pytest_process(command, environment, timeout)
            return _pytest_outcome(completed, environment, result_path, started)
    except subprocess.TimeoutExpired:
        return RunnerOutcome(
            "timeout",
            duration_ms=(time.monotonic() - started) * 1000.0,
            detail="native mutation execution timed out",
        )
    except (OSError, ValueError) as error:
        return RunnerOutcome(
            "error", duration_ms=(time.monotonic() - started) * 1000.0, detail=str(error)
        )


def _pytest_outcome(
    completed: subprocess.CompletedProcess[str],
    environment: Mapping[str, str],
    result_path: Path,
    started: float,
) -> RunnerOutcome:
    native_result = None
    if result_path.exists():
        from .native import _pytest  # noqa: PLC0415 - native execution is platform-gated

        native_result = _pytest.NativeRunResult.merge_json_results(
            [result_path.read_text(encoding="utf-8")]
        )
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    failure = native_result.mutation_failure() if native_result is not None else None
    if completed.returncode == 0 and native_result is not None:
        status = "survived"
    elif completed.returncode == 1 and failure is not None:
        status = "killed"
    elif completed.returncode == 5:
        status = "unreached"
    elif completed.returncode == 124 and environment.get("VFY_MUTATION_TEST_ISOLATION") == "file":
        status = "timeout"
    else:
        status = "error"
    killer = failure[0] if status == "killed" and failure is not None else None
    detail = output[-4000:] if status in {"error", "invalid", "timeout"} else ""
    # The file-group watchdog's internal exit code does not represent a
    # completed pytest run. Preserve the public incomplete-timeout contract.
    returncode = None if status == "timeout" else completed.returncode
    return RunnerOutcome(
        status,
        killer,
        (time.monotonic() - started) * 1000.0,
        returncode,
        detail,
        phase=failure[1] if status == "killed" and failure is not None else None,
    )


class _ExitStatus(Enum):
    """A fresh process exit whose status another wait call consumed."""

    UNAVAILABLE = "unavailable"


class _PortableMutationProcess:
    """Own a fresh pytest interpreter and its private result and output files."""

    def __init__(
        self,
        command: Sequence[str],
        environment: dict[str, str],
        started: float,
        lease_fd: int | None = None,
        *,
        tests: Sequence[str] = (),
    ) -> None:
        self.started = started
        self.environment = environment
        self.command = command
        self.resources = ExitStack()
        self.closed = False
        self.stopped = False
        self.exit: int | _ExitStatus | None = None
        try:
            directory = self.resources.enter_context(
                TemporaryDirectory(prefix="vfy-mutation-result-")
            )
            self.result_path = Path(directory) / "native.json"
            environment["PYTEST_NATIVE_RESULT"] = str(self.result_path)
            self.validation_error_path = Path(directory) / "invalid.txt"
            environment["VFY_MUTATION_VALIDATION_ERROR"] = str(self.validation_error_path)
            if tests:
                selected_path = Path(directory) / "selected-tests.json"
                write_test_collection(str(selected_path), list(tests))
                environment["VFY_MUTATION_SELECTED_TESTS"] = str(selected_path)
            # A collected parent's process group must never be inherited by a
            # fresh portable job; its descendants share the job's new session.
            environment.pop("VFY_MUTATION_JOB_GROUP", None)
            # The supervisor collects under this worker lease and hands it to
            # its first file group; the controller closes its own copy.
            environment.pop(INHERITED_LEASE_ENVIRONMENT_NAME, None)
            if lease_fd is not None:
                environment[INHERITED_LEASE_ENVIRONMENT_NAME] = str(lease_fd)
            # ExitStack holds these files until the asynchronous job is released.
            self.stdout = self.resources.enter_context(
                TemporaryFile(mode="w+", errors="replace")  # noqa: SIM115 - ExitStack owns the file
            )
            self.stderr = self.resources.enter_context(
                TemporaryFile(mode="w+", errors="replace")  # noqa: SIM115 - ExitStack owns the file
            )
            self.process = subprocess.Popen(
                command,
                cwd=str(Path.cwd()),
                env=environment,
                stdout=self.stdout,
                stderr=self.stderr,
                start_new_session=os.name == "posix",
                pass_fds=() if lease_fd is None else (lease_fd,),
            )
        except BaseException:
            self.resources.close()
            raise

    @property
    def pid(self) -> int:
        return self.process.pid

    def poll(self) -> RunnerOutcome | None:
        returncode = self._poll_exit()
        if returncode is None:
            return None
        # Stop inherited output writers before reading regular files. A
        # surviving descendant must not keep a completed job's watchdog busy.
        self._stop()
        if self.validation_error_path.exists():
            return RunnerOutcome(
                "invalid",
                duration_ms=(time.monotonic() - self.started) * 1000.0,
                detail=self.validation_error_path.read_text(encoding="utf-8"),
            )
        if returncode is _ExitStatus.UNAVAILABLE:
            # A result file alone cannot prove that pytest passed.
            return RunnerOutcome(
                "error",
                duration_ms=(time.monotonic() - self.started) * 1000.0,
                detail=(
                    f"the exit status of mutation pytest process {self.pid} is unavailable "
                    "because another wait call in the controller reaped it"
                ),
            )
        self.stdout.seek(0)
        self.stderr.seek(0)
        completed = subprocess.CompletedProcess(
            self.command,
            returncode,
            self.stdout.read(),
            self.stderr.read(),
        )
        try:
            return _pytest_outcome(completed, self.environment, self.result_path, self.started)
        except (OSError, ValueError) as error:
            return RunnerOutcome(
                "error", duration_ms=(time.monotonic() - self.started) * 1000.0, detail=str(error)
            )

    def _poll_exit(self) -> int | _ExitStatus | None:
        if self.exit is not None:
            return self.exit
        if os.name != "posix":
            return self.process.poll()
        try:
            waited, status = os.waitpid(self.process.pid, os.WNOHANG)
        except ChildProcessError:
            # Project code in this controller, such as a waitpid(-1) loop during
            # collection, reaped the process. subprocess reports that ECHILD
            # case as exit status 0, so vfy records the loss instead. Marking the
            # Popen finished keeps it from waiting on a PID that may be reused.
            self.process.returncode = 0
            self.exit = _ExitStatus.UNAVAILABLE
            return self.exit
        if waited == 0:
            return None
        self.process.returncode = os.waitstatus_to_exitcode(status)
        self.exit = self.process.returncode
        return self.exit

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._stop()
            self.process.wait()
        finally:
            self.resources.close()

    def _stop(self) -> None:
        if not self.stopped:
            _stop_pytest_process(self.process, self.environment, os.name == "posix")
            self.stopped = True


def _fresh_mutation_command(command: Sequence[str]) -> list[str]:
    """Run the pytest command through the validating mutation entry point."""
    executable, *arguments = command
    if arguments[:2] != ["-m", "pytest"]:
        raise RuntimeError(f"unexpected pytest command layout: {list(command)!r}")
    return [executable, "-m", "vfy.mutation_process", *arguments[2:]]


def _start_portable_mutation(
    tests: Sequence[str],
    execution_args: Sequence[str],
    candidate: Mutation,
    preferred_tests: Sequence[str] = (),
    lease_fd: int | None = None,
) -> PortableMutationProcess | RunnerOutcome:
    started = time.monotonic()
    mutation = candidate.spec()
    command, environment = _pytest_command(execution_args, mutation)
    command = _fresh_mutation_command(command)
    environment["VFY_MUTATION_PREFERRED_TESTS"] = json.dumps(list(preferred_tests))
    try:
        return _PortableMutationProcess(command, environment, started, lease_fd, tests=tests)
    except (OSError, ValueError) as error:
        return RunnerOutcome(
            "error", duration_ms=(time.monotonic() - started) * 1000.0, detail=str(error)
        )


def _run_pytest_process(
    command: Sequence[str], environment: Mapping[str, str], timeout: float | None
) -> subprocess.CompletedProcess[str]:
    owns_group = os.name == "posix" and "VFY_MUTATION_JOB_GROUP" not in environment
    with subprocess.Popen(
        command,
        cwd=str(Path.cwd()),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=owns_group,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        finally:
            _stop_pytest_process(process, environment, owns_group)


def _stop_pytest_process(
    process: subprocess.Popen[str] | subprocess.Popen[bytes],
    environment: Mapping[str, str],
    owns_group: bool,
) -> None:
    if owns_group:
        # A pytest grandchild may outlive its direct parent.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        pool_directory = environment.get("VFY_MUTATION_WORKER_POOL")
        if pool_directory is not None:
            terminate_file_groups(Path(pool_directory), process.pid)
    elif process.poll() is None:
        process.kill()


def _evaluate(
    mutation: Mutation,
    plan: MutationTestPlan,
    execution_args: Sequence[str],
    timeout: float,
    preferred_tests: Sequence[str] = (),
    *,
    engine: TestEngine = TestEngine.NATIVE,
    cache_evidence: bool = False,
) -> MutationResult:
    if plan.scope is MutationTestScope.UNREACHED:
        return MutationResult(mutation, "unreached")
    if engine is TestEngine.INDEPENDENT:
        from .independent.mutation_fresh import evaluate  # noqa: PLC0415 - selected engine resource

        return evaluate(
            mutation, plan, execution_args, timeout, preferred_tests, cache_evidence=cache_evidence
        )
    outcome = _run_once(
        plan.nodeids, execution_args, mutation, timeout, mutation.spec(), preferred_tests
    )
    return _result_from_runner(mutation, plan.nodeids, outcome)


def _select_mutation_files(
    args: argparse.Namespace, store: CoverageStore | None
) -> tuple[list[Path], str | None]:
    roots = list(args.source or []) or (store.measured_files() if store is not None else [])
    try:
        paths, fallback, scope = select_mutation_sources(
            [str(path) for path in _source_files(roots)], args.target, args.all, args.base
        )
    except ValueError as error:
        raise VfyError(str(error)) from error
    if fallback:
        OutputPolicy.from_args(args).notice(
            "Git state unavailable; mutating the supplied or measured Python files"
        )
    return [Path(path) for path in paths], scope


def _worker_count(value: str | int, pending: int) -> int:
    if pending < 2:
        return 1
    if isinstance(value, int):
        return max(1, min(value, pending))
    logical = os.cpu_count() or 1
    if value == "auto":
        with suppress(AttributeError, OSError):
            logical = len(getattr(os, "sched_getaffinity")(0))  # noqa: B009 - optional platform capability is guarded by AttributeError
    return max(1, min(logical, pending))


def _result_from_runner(
    mutation: Mutation,
    tests: tuple[str, ...],
    outcome: RunnerOutcome,
) -> MutationResult:
    status = "protected" if outcome.status == "killed" else outcome.status
    detail = outcome.detail
    if outcome.status == "killed":
        detail = detail or "native tests rejected the counterfactual"
    return MutationResult(
        mutation,
        status,
        tests,
        outcome.killer,
        outcome.duration_ms,
        outcome.returncode,
        detail,
        outcome.phase,
    )


def _baseline_result(
    status: MutationStatus,
    detail: str,
    killer: str | None,
    returncode: int | None,
) -> str:
    """Classify a clean-run result for all pending mutations."""
    if status == "killed":
        return "the selected tests do not pass without the mutation"
    if status == "survived":
        return ""
    if detail:
        return f"clean pytest baseline was not runnable: {detail[-1000:]}"
    return "clean pytest baseline was not runnable"


def _refresh_context_baseline(
    args: argparse.Namespace,
    pytest_args: Sequence[str],
    history_path: str | None = None,
    *,
    full_suite: bool,
    coverage_policy: MutationBaselineCoveragePolicy,
) -> tuple[CoverageStore, NativeRunResult]:
    """Collect a context-aware baseline when no reusable one is available."""
    command = [sys.executable, "-m", "vfy", "test", "--engine=pytest-native"]
    if full_suite:
        command.append("--all")
    if os.environ.get("VFY_MUTATION_TEST_ISOLATION") == "file":
        command.extend(("-j", "1"))
    rcfile = getattr(args, "rcfile", None)
    if rcfile:
        command.extend(("--rcfile", str(rcfile)))
    if args.data_file:
        command.extend(("--data-file", str(args.data_file)))
    if history_path:
        command.extend(("--history", str(history_path)))
    for source in args.source or []:
        command.extend(("--source", str(source)))
    values = list(pytest_args)
    if os.environ.get("VFY_MUTATION_TEST_ISOLATION") == "file":
        values[0:0] = ("-p", "vfy.mutation_parallel")
    if values:
        command.extend(values)
    environment = os.environ.copy()
    environment["VFY_MUTATION_BASELINE_COVERAGE"] = coverage_policy
    for name in (
        "VFY_CLI_ACTIVE",
        "VFY_MUTATION",
        "VFY_PROCESS_OPTIONS",
        "VFY_PROCESS_START",
        "VFY_REPLAY_MERGE",
    ):
        environment.pop(name, None)
    with TemporaryDirectory(prefix="vfy-mutation-baseline-result-") as directory:
        result_path = Path(
            environment.get("PYTEST_NATIVE_RESULT") or str(Path(directory) / "result.json")
        )
        environment["PYTEST_NATIVE_RESULT"] = str(result_path)
        baseline_timeout = mutation_baseline_timeout(args.timeout)
        try:
            completed = _run_pytest_process(command, environment, baseline_timeout)
        except subprocess.TimeoutExpired as error:
            raise NoDataError(
                f"automatic mutation baseline collection exceeded {baseline_timeout:g} seconds"
            ) from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            if detail:
                detail = f": {detail[-1000:]}"
            raise NoDataError(f"automatic mutation baseline collection failed{detail}")
        try:
            from .native import _pytest  # noqa: PLC0415 - selected native baseline result

            result = _pytest.NativeRunResult.merge_json_results(
                [result_path.read_text(encoding="utf-8")]
            )
            return CoverageStore.read(args.data_file), result
        except (DataError, OSError, ValueError) as error:
            raise NoDataError(f"automatic mutation baseline collection failed: {error}") from error


@dataclass(frozen=True)
class _MutationBaseline:
    store: CoverageStore
    locations: dict[str, set[str]]
    executed_paths: set[str]
    tests: set[str]
    suite_state: tuple[tuple[str, str], ...]
    verified: bool
    elapsed_ms: float


def _baseline_inventory(result: NativeRunResult) -> set[str]:
    if result.failed or result.errors or result.collection_errors or not result.cache_outcomes():
        raise NoDataError("automatic mutation baseline did not execute a passing test suite")
    return set(result.collection_nodeids)


def _baseline_contexts(
    store: CoverageStore, collected_tests: set[str] | None
) -> tuple[dict[str, set[str]], set[str]]:
    locations, tests = mutation_contexts(store.to_rust())
    return locations, tests or collected_tests or set()


def _mutation_baseline_configuration(
    pytest_args: Sequence[str], configuration: PytestConfigRecord, engine: TestEngine
) -> tuple[list[str], PytestConfigRecord]:
    arguments = list(pytest_args)
    if engine is TestEngine.NATIVE:
        arguments[0:0] = mutation_traceback_args(configuration["effective_args"])
        configuration = _resolve_pytest_configuration(arguments).projection
    return arguments, configuration


def _prepare_mutation_baseline(
    args: argparse.Namespace,
    config: Config,
    pytest_args: Sequence[str],
    history_path: str,
    pytest_configuration: PytestConfigRecord,
    outputs: NativeMutationPublication | None = None,
) -> _MutationBaseline:
    independent = outputs is not None
    data_path = str(outputs.data_file) if outputs is not None else args.data_file
    manifest_path = outputs.baseline_file if outputs is not None else None
    unavailable = None if independent else _native_runner_unavailability_reason()
    if unavailable is not None:
        raise VfyError(f"vfy mutate requires native execution: {unavailable}")
    baseline_pytest_args, pytest_configuration = _mutation_baseline_configuration(
        pytest_args,
        pytest_configuration,
        TestEngine.INDEPENDENT if independent else TestEngine.NATIVE,
    )
    effective_pytest_args = pytest_configuration["effective_args"]
    coverage_policy = NativeTestInvocation.mutation_baseline_coverage_policy(effective_pytest_args)
    execution_identity = execution_fingerprint(
        config,
        list(args.source or []),
        baseline_pytest_args,
        engine="native" if independent else "pytest-native",
        pytest_configuration=pytest_configuration,
    )
    if not independent:
        execution_identity = native_execution_fingerprint(
            execution_identity,
            os.environ.get("VFY_NATIVE_SCHEDULING", "file"),
            worker_collection=os.environ.get("VFY_WORKER_COLLECTION") == "1",
            worker_start=os.environ.get("VFY_WORKER_START"),
            startup_grace=os.environ.get("VFY_NATIVE_STARTUP_GRACE_MS"),
        )
    baseline_ms = 0.0
    collected_now = False
    collected_tests: set[str] | None = None

    def refresh_baseline() -> CoverageStore:
        nonlocal baseline_ms, collected_now, collected_tests
        refresh_started = time.monotonic()
        try:
            if independent:
                from .independent.mutation_fresh import (  # noqa: PLC0415 - selected engine resource
                    collect_baseline,
                )

                result = collect_baseline(
                    baseline_pytest_args,
                    args.timeout,
                    data_path,
                    args.source or [],
                    getattr(args, "rcfile", None),
                    config,
                    coverage_policy=coverage_policy,
                    coverage_arguments=effective_pytest_args,
                    history_path=history_path,
                )
                refreshed = CoverageStore.read(data_path)
            else:
                refreshed, result = _refresh_context_baseline(
                    args,
                    baseline_pytest_args,
                    history_path,
                    full_suite=not pytest_configuration["filtered_scope"],
                    coverage_policy=coverage_policy,
                )
            collected_tests = _baseline_inventory(result)
            collected_now = True
            return refreshed
        finally:
            baseline_ms += (time.monotonic() - refresh_started) * 1000.0

    store = _shared_passing_test_run(history_path, execution_identity)
    shared_passing = store is not None
    if store is None:
        try:
            store = CoverageStore.read(data_path)
        except (FileNotFoundError, DataError, OSError):
            store = refresh_baseline()

    by_location, all_tests = _baseline_contexts(store, collected_tests)
    if not all_tests and not collected_now:
        store = refresh_baseline()
        by_location, all_tests = _baseline_contexts(store, collected_tests)
    if not all_tests:
        raise VfyError("automatic mutation baseline did not collect any tests")
    suite_state = (
        *mutation_suite_state(sorted(all_tests), effective_pytest_args),
        ("<pytest-execution-identity>", execution_identity),
        *_pytest_environment_state(TestEngine.INDEPENDENT if independent else TestEngine.NATIVE),
        (history_path, mutation_file_digest(history_path)),
        (str(Path(args.data_file).resolve()), mutation_file_digest(data_path)),
    )
    baseline_verified = (
        shared_passing
        or collected_now
        or _baseline_manifest_valid(
            sorted(all_tests),
            pytest_args,
            suite_state,
            data_file=args.data_file,
            data_hash=mutation_file_digest(data_path),
            manifest_path=manifest_path,
            publication=outputs,
        )
    )
    if shared_passing or collected_now:
        _write_baseline_manifest(
            sorted(all_tests),
            pytest_args,
            suite_state,
            data_file=args.data_file,
            data_hash=mutation_file_digest(data_path),
            manifest_path=manifest_path,
            publication=outputs,
        )
    if not baseline_verified:
        previous_store = store
        try:
            store = refresh_baseline()
        except NoDataError:
            if independent:
                raise
            # Keep a failing suite's context file so clean verification reports
            # "unstable", not a mutation kill. Missing or corrupt files still raise.
            store = previous_store
            baseline_verified = False
        else:
            by_location, all_tests = _baseline_contexts(store, collected_tests)
            if not all_tests:
                raise VfyError("automatic mutation baseline did not collect any tests")
            suite_state = (
                *mutation_suite_state(sorted(all_tests), effective_pytest_args),
                ("<pytest-execution-identity>", execution_identity),
                *_pytest_environment_state(
                    TestEngine.INDEPENDENT if independent else TestEngine.NATIVE
                ),
                (history_path, mutation_file_digest(history_path)),
                (str(Path(args.data_file).resolve()), mutation_file_digest(data_path)),
            )
            _write_baseline_manifest(
                sorted(all_tests),
                pytest_args,
                suite_state,
                data_file=args.data_file,
                data_hash=mutation_file_digest(data_path),
                manifest_path=manifest_path,
                publication=outputs,
            )
            baseline_verified = True
    return _MutationBaseline(
        store,
        by_location,
        mutation_executed_paths(store.to_rust()),
        all_tests,
        suite_state,
        baseline_verified,
        baseline_ms,
    )


def _cached_mutations(
    selected: Sequence[Mutation],
    ranked_tests: Mapping[str, MutationTestPlan],
    cache: Mapping[str, MutationResult],
    *,
    preserve_result_tests: bool = False,
) -> tuple[dict[str, MutationResult], list[tuple[Mutation, MutationTestPlan]]]:
    results: dict[str, MutationResult] = {}
    pending: list[tuple[Mutation, MutationTestPlan]] = []
    for mutation in selected:
        plan = ranked_tests[mutation.key]
        cached = cache.get(mutation.key)
        if cached is not None:
            # Reuse cached status and timing, but take the mutation and its
            # owner eligibility from the current plan.
            results[mutation.key] = MutationResult(
                mutation,
                cached.status,
                cached.tests if preserve_result_tests else plan.nodeids,
                cached.killer,
                cached.duration_ms,
                cached.returncode,
                cached.detail,
                cached.phase,
            )
        else:
            pending.append((mutation, plan))
    return results, pending


def _verify_clean_baseline(
    baseline: RunnerOutcome | None,
    verified: bool,
    candidate: Mutation | None,
    execution_args: Sequence[str],
    timeout: float,
) -> tuple[RunnerOutcome, float]:
    elapsed_ms = 0.0
    if baseline is None:
        if verified:
            baseline = RunnerOutcome(
                "survived", returncode=0, detail="reused verified context baseline"
            )
        else:
            baseline_started = time.monotonic()
            if candidate is None:
                raise VfyError("unverified mutation baseline has no execution candidate")
            baseline = _run_once((), execution_args, candidate, mutation_baseline_timeout(timeout))
            elapsed_ms += (time.monotonic() - baseline_started) * 1000.0

    return baseline, elapsed_ms


def _evaluate_fallback(
    pending: Sequence[tuple[Mutation, MutationTestPlan]],
    execution_args: Sequence[str],
    timeout: float,
    workers: int,
    record: Callable[[str, MutationResult], None],
    preferences: Mapping[str, Sequence[str]],
    *,
    engine: TestEngine = TestEngine.NATIVE,
    cache_evidence: bool = False,
    context_data: str | None = None,
    collection: MutationCacheCollection | None = None,
) -> int:
    if engine is TestEngine.INDEPENDENT and pending and context_data is not None:
        from .independent.mutation_fresh import (  # noqa: PLC0415 - isolated mutation resource
            evaluate_mutations,
        )

        return evaluate_mutations(
            pending,
            execution_args,
            timeout,
            context_data,
            record,
            cache_evidence=cache_evidence,
            collection=collection,
        )
    if workers == 1 or len(pending) < 2:
        for mutation, plan in pending:
            record(
                mutation.key,
                _evaluate(
                    mutation,
                    plan,
                    execution_args,
                    timeout,
                    preferences.get(mutation.key, ()),
                    engine=engine,
                    cache_evidence=cache_evidence,
                ),
            )
        return 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _evaluate,
                mutation,
                plan,
                execution_args,
                timeout,
                preferences.get(mutation.key, ()),
                engine=engine,
                cache_evidence=cache_evidence,
            ): mutation.key
            for mutation, plan in pending
        }
        for future in as_completed(futures):
            record(futures[future], future.result())
    return 0


@dataclass
class _MutationProgress:
    cache_path: Path
    enabled: bool
    fingerprint: str
    mutation_fingerprints: Mapping[str, str]
    mutation_by_key: Mapping[str, Mutation]
    results: dict[str, MutationResult]
    uncacheable_keys: set[str] = field(default_factory=set)
    dirty_results: int = 0
    persistence_ms: float = 0.0
    fresh_collection: MutationCacheCollection | None = None
    fresh_request: str = ""

    def flush_cache(self, *, force: bool = False) -> None:
        if not self.enabled:
            return
        if self.dirty_results == 0:
            return
        if not force and self.dirty_results < _CACHE_FLUSH_INTERVAL:
            return
        persistence_started = time.monotonic()
        snapshot = MutationReport(
            self.fingerprint,
            tuple(
                self.results[mutation.key]
                for mutation in self.mutation_by_key.values()
                if mutation.key in self.results and mutation.key not in self.uncacheable_keys
            ),
        )
        if self.fresh_collection is not None:
            self.fresh_collection.write_fresh_mutation_cache(
                str(self.cache_path),
                self.fresh_request,
                [
                    result._native_result
                    for result in self.results.values()
                    if result._native_result is not None
                ],
            )
        else:
            _write_cache(self.cache_path, snapshot, self.mutation_fingerprints)
        self.persistence_ms += (time.monotonic() - persistence_started) * 1000.0
        self.dirty_results = 0

    def record_runner_result(
        self, key: str, outcome: RunnerOutcome, plan: MutationTestPlan, *, cacheable: bool
    ) -> None:
        mutation = self.mutation_by_key.get(key)
        if mutation is None:
            return
        if not cacheable:
            self.uncacheable_keys.add(key)
        self.results[key] = _result_from_runner(mutation, plan.nodeids, outcome)
        self.dirty_results += 1
        self.flush_cache()

    def record_fallback_result(self, key: str, result: MutationResult, *, cacheable: bool) -> None:
        if not cacheable:
            self.uncacheable_keys.add(key)
        self.results[key] = result
        self.dirty_results += 1
        self.flush_cache()


def run_mutation(args: argparse.Namespace, config: Config) -> int:
    """Run independent serial mutations or the explicit pytest-backed adapter."""
    if getattr(args, "engine", "native") == "native":
        if sys.platform != "linux":
            raise VfyError(
                "independent serial mutations require Linux descendant reaping; "
                "run on Linux, or on macOS use `vfy mutate --engine=pytest-native` "
                "with vfy[pytest] installed"
            )
        if str(args.workers) != "1":
            raise VfyError(
                "independent mutations require --workers=1; "
                "use `vfy mutate --engine=pytest-native` "
                "with vfy[pytest] installed for parallel mutations"
            )
        if getattr(args, "test_isolation", None) == "file":
            raise VfyError(
                "independent mutations require --test-isolation=serial; "
                "use `vfy mutate --engine=pytest-native "
                "--test-isolation=file` with vfy[pytest] installed"
            )
        from .native import _pytest  # noqa: PLC0415 - native execution is platform-gated

        outputs = _pytest.NativeMutationPublication(
            Path(args.data_file).resolve(),
            _cache_path(args.data_file).resolve(),
            _baseline_manifest_path().resolve(),
            Path(args.output).resolve() if args.output and args.output != "-" else None,
        )
        stdout = io.StringIO()
        with serial_worker_scope(), redirect_stdout(stdout), ExitStack() as resources:
            status = _run_mutation(args, config, outputs, resources)
        outputs.commit()
        sys.stdout.write(stdout.getvalue())
        return status
    isolation = getattr(args, "test_isolation", None)
    if (
        isolation == "serial"
        or os.name != "posix"
        or not hasattr(os, "fork")
        or threading.active_count() != 1
    ):
        if isolation == "file":
            raise VfyError("--test-isolation=file requires a single-threaded POSIX process")
        with serial_worker_scope():
            return _run_mutation(args, config)
    capacity = _worker_count(args.workers, sys.maxsize)
    with isolated_worker_pool(capacity, args.timeout):
        return _run_mutation(args, config)


def _mutation_arguments(args: argparse.Namespace) -> list[str]:
    pytest_args: list[str] = list(args.pytest_args)
    rcfile = getattr(args, "rcfile", None)
    if rcfile and not any(
        argument == "-c"
        or argument == "--config-file"
        or argument.startswith(("--config-file=", "-c"))
        for argument in pytest_args
    ):
        pytest_args[0:0] = ["-c", str(rcfile)]
    return pytest_args


def _note_uncollected_tests(nodeids: Sequence[str]) -> None:
    if not nodeids:
        return
    print(
        f"Note: pytest did not collect {len(nodeids)} test IDs recorded in "
        f"the coverage contexts, such as {nodeids[0]!r}. Mutations they "
        "covered also ran the tests with new IDs or the full suite. Give "
        "parametrized tests stable IDs to keep mutation runs selective.",
        file=sys.stderr,
    )


def _mutation_configuration(
    pytest_args: list[str], engine: TestEngine
) -> tuple[_PytestConfiguration, tuple[str, ...]]:
    configuration = _resolve_pytest_configuration(pytest_args, engine=engine)
    try:
        worker_args = NativeTestInvocation.mutation_worker_args(
            configuration.effective_args, configuration.projection["cli_args"]
        )
    except ValueError as error:
        raise VfyError(str(error)) from error
    if worker_args:
        pytest_args[0:0] = worker_args
        configuration = _resolve_pytest_configuration(pytest_args, engine=engine)
    if engine is TestEngine.INDEPENDENT:
        plugin_args = _native_xdist_plugin_args(configuration, configuration.plugins)
        if plugin_args:
            # Match autoload's registration name in the independent plugin manager.
            plugin_args = ("-p", "xdist")
        if worker_args or plugin_args:
            pytest_args[0:0] = [*plugin_args, "--workers=1"]
            configuration = _resolve_pytest_configuration(pytest_args, engine=engine)
        validate_mutation_pytest_args(configuration)
        return configuration, tuple(pytest_args)
    return configuration, tuple(mutation_pytest_args(pytest_args, configuration=configuration))


def _mutation_cache_partition(
    request: str,
    execution_args: Sequence[str],
    timeout: float,
    *,
    independent: bool,
    enabled: bool,
    pending: Sequence[tuple[Mutation, MutationTestPlan]] = (),
    context_data: str | None = None,
    resources: ExitStack | None = None,
) -> tuple[str, dict[str, str], MutationCacheCollection | None, bool]:
    fingerprint, fingerprints = mutation_fingerprints(request)
    if not independent or not enabled:
        return fingerprint, fingerprints, None, enabled
    from .independent.mutation_fresh import (  # noqa: PLC0415 - actual clean cache resource
        collect_cache_scope,
    )

    if resources is not None and context_data is not None:
        from .independent.mutation_fresh import (  # noqa: PLC0415 - native collection lifetime
            collect_reusable_scope,
        )

        retained = collect_reusable_scope(
            request, pending, execution_args, timeout, context_data, resources
        )
        if retained is not None:
            owner, partition = retained
            return *partition, owner, True
    collected = collect_cache_scope(execution_args, timeout)
    partition = collected.fresh_mutation_fingerprints(request)
    if partition is None:
        return fingerprint, fingerprints, collected, False
    return *partition, collected, True


def _run_mutation(
    args: argparse.Namespace,
    config: Config,
    outputs: NativeMutationPublication | None = None,
    resources: ExitStack | None = None,
) -> int:
    started = time.monotonic()
    baseline_ms = 0.0
    catalog_ms = 0.0
    execution_ms = 0.0
    collected_results = 0
    pytest_args = _mutation_arguments(args)
    independent = outputs is not None
    configuration, execution_args = _mutation_configuration(
        pytest_args, TestEngine.INDEPENDENT if independent else TestEngine.NATIVE
    )
    cache_path = outputs.cache_file if outputs is not None else _cache_path(args.data_file)
    history_path = str(Path(getattr(args, "history", ".vfy/test-history.sqlite3")).resolve())

    # Explicit Git scope and catalog errors must precede baseline publication.
    baseline = (
        None
        if args.base is not None
        else _prepare_mutation_baseline(
            args, config, pytest_args, history_path, configuration.projection, outputs
        )
    )
    files, scope = _select_mutation_files(args, baseline.store if baseline is not None else None)
    catalog_started = time.monotonic()
    try:
        records = select_mutation_catalog(
            [str(path) for path in files],
            args.target,
            args.operator or [],
            args.max_mutations,
            os.environ.get("VFY_MUTATION_KEYS"),
        )
    except OSError as error:
        raise VfyError(f"failed to analyze mutation sources: {error}") from error
    except ValueError as error:
        raise VfyError(str(error)) from error
    catalog_ms = (time.monotonic() - catalog_started) * 1000.0
    if baseline is None:
        baseline = _prepare_mutation_baseline(
            args, config, pytest_args, history_path, configuration.projection, outputs
        )
    by_location, all_tests = baseline.locations, baseline.tests
    suite_state, baseline_verified = baseline.suite_state, baseline.verified
    baseline_ms = baseline.elapsed_ms
    selected = [Mutation.from_rust(record) for record in records]
    ranked_tests = _rank_mutation_tests(
        selected,
        by_location,
        baseline.executed_paths,
        history_path,
        config.get("run", {}).get("omit", []),
        collection_data=baseline.store.to_rust(),
    )
    preferences = (
        mutation_test_preferences(
            [
                (mutation.key, mutation.path, mutation.owner_start, mutation.owner_end)
                for mutation in selected
                if mutation.execution_phase == "definition"
            ],
            by_location,
            history_path,
        )
        if os.environ.get("VFY_MUTATION_TEST_ISOLATION") == "file"
        else {}
    )
    fingerprint_request = _fingerprint_request(
        selected,
        sorted(all_tests),
        pytest_args,
        args.timeout,
        ranked_tests,
        suite_state,
        args.timeout_policy,
    )
    fingerprint, per_mutation_fingerprints, fresh_collection, cache_enabled = (
        _mutation_cache_partition(
            fingerprint_request,
            execution_args,
            args.timeout,
            independent=independent,
            enabled=not args.no_cache,
            pending=[(mutation, ranked_tests[mutation.key]) for mutation in selected],
            context_data=str(outputs.data_file) if outputs is not None else None,
            resources=resources,
        )
    )
    cache = (
        _read_cache(
            cache_path,
            fingerprint,
            per_mutation_fingerprints,
            fresh_collection=fresh_collection,
            request=fingerprint_request,
        )
        if cache_enabled
        else {}
    )
    results, pending = _cached_mutations(
        selected, ranked_tests, cache, preserve_result_tests=independent
    )
    cache_hits = len(results)
    worker_count = _worker_count(args.workers, len(pending))

    progress = _MutationProgress(
        cache_path,
        cache_enabled,
        fingerprint,
        per_mutation_fingerprints,
        {mutation.key: mutation for mutation in selected},
        results,
        fresh_collection=fresh_collection,
        fresh_request=fingerprint_request,
    )

    # Run known portable mutations in the collected runner's worker budget so
    # full-suite cases can overlap short body mutations.
    def start_portable(
        mutation: Mutation, plan: MutationTestPlan, lease_fd: int | None
    ) -> PortableMutationProcess | RunnerOutcome:
        return _start_portable_mutation(
            plan.nodeids,
            execution_args,
            mutation,
            preferences.get(mutation.key, ()),
            lease_fd,
        )

    runner_type = CollectedMutationRunner
    if independent:
        from .independent.mutation_fresh import (  # noqa: PLC0415 - selected engine resource
            FreshMutationFallbackRunner,
        )

        runner_type = FreshMutationFallbackRunner
    runner = runner_type(
        tuple(sorted(all_tests)),
        execution_args,
        args.timeout,
        worker_count,
        baseline_verified=baseline_verified,
        timeout_policy=args.timeout_policy,
        history_path=history_path,
        portable_starter=None if independent else start_portable,
    )

    def record_runner_result(key: str, outcome: RunnerOutcome) -> None:
        plan = runner.report_plans.get(key, runner.plans.get(key, ranked_tests[key]))
        progress.record_runner_result(key, outcome, plan, cacheable=key not in runner.plans)

    runner.on_result = record_runner_result
    if pending:
        execution_started = time.monotonic()
        try:
            runner.run(pending)
        except KeyboardInterrupt:
            # Commit completed callbacks before propagating the interrupt.
            progress.flush_cache(force=True)
            raise
        baseline = runner.baseline
        execution_ms += (time.monotonic() - execution_started) * 1000.0
        _note_uncollected_tests(runner.uncollected)
    else:
        # Cached mutations still need a valid clean-baseline manifest.
        baseline = (
            RunnerOutcome("survived", returncode=0, detail="all mutations were cached")
            if baseline_verified
            else None
        )
    baseline, verification_ms = _verify_clean_baseline(
        baseline, baseline_verified, selected[0] if selected else None, execution_args, args.timeout
    )
    baseline_ms += verification_ms

    baseline_detail = _baseline_result(
        baseline.status, baseline.detail, baseline.killer, baseline.returncode
    )
    if baseline.status != "survived":
        for mutation in selected:
            tests = ranked_tests[mutation.key].nodeids
            status = "unstable" if baseline.status in {"killed", "unreached"} else baseline.status
            results[mutation.key] = MutationResult(
                mutation,
                status,
                tests,
                baseline.killer,
                baseline.duration_ms,
                baseline.returncode,
                baseline_detail,
            )
            progress.dirty_results += 1
        progress.flush_cache(force=True)
    else:
        execution_started = time.monotonic()
        pending_fallback = [
            (mutation, plan if independent else runner.plans.get(mutation.key, plan))
            for mutation, plan in pending
            if mutation.key in runner.fallback and mutation.key not in runner.portable_outcomes
        ]

        def record_fallback_result(key: str, result: MutationResult) -> None:
            progress.record_fallback_result(key, result, cacheable=key not in runner.plans)

        try:
            collected_results = _evaluate_fallback(
                pending_fallback,
                execution_args,
                args.timeout,
                worker_count,
                record_fallback_result,
                preferences,
                engine=TestEngine.INDEPENDENT if independent else TestEngine.NATIVE,
                cache_evidence=cache_enabled and fresh_collection is not None,
                context_data=str(outputs.data_file) if outputs is not None else None,
                collection=fresh_collection,
            )
        except KeyboardInterrupt:
            # Commit completed fallback results before propagating the interrupt.
            progress.flush_cache(force=True)
            raise
        progress.flush_cache(force=True)
        execution_ms += (time.monotonic() - execution_started) * 1000.0
    report = MutationReport(
        fingerprint,
        tuple(results[mutation.key] for mutation in selected),
        {
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
            "mutations": len(selected),
            "executed": len(pending),
            "cache_hits": cache_hits,
            "portable_fallbacks": len(runner.fallback) - collected_results,
            "fast_results": len(runner.outcomes) + collected_results,
            "catalog_ms": round(catalog_ms, 3),
            "baseline_ms": round(baseline_ms, 3),
            "execution_ms": round(execution_ms if pending else 0.0, 3),
            "persistence_ms": round(progress.persistence_ms, 3),
        },
        scope=scope,
    )
    if isinstance(report.metrics, dict):
        report.metrics["persistence_ms"] = round(progress.persistence_ms, 3)
        report.metrics["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 3)
    content, status = render_mutation_report(
        report._as_json(),
        args.format,
        args.pretty_print,
        args.fail_on_survivor,
    )
    if not args.output or args.output == "-":
        sys.stdout.write(content)
    else:
        write_report_output(
            content, str(outputs.report_file if outputs is not None else args.output)
        )
    return status


__all__ = ["Mutation", "MutationReport", "MutationResult", "run_mutation"]
