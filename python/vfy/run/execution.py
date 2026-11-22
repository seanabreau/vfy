"""Run pytest for a controller and record its test results."""

from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol

from vfy._pytest.plugin_support import disable_plugin_autoload
from vfy.errors import VfyError
from vfy.native import record_test_execution

from .confirmation import _confirmation_message, _confirmation_pytest_args
from .engine import TestEngine
from .reports import _controller_message

if TYPE_CHECKING:
    from vfy.native_types import PytestConfigRecord, TestRunRecord


class ExecutionConfiguration(Protocol):
    """The configured collection facts needed to record a run."""

    @property
    def projection(self) -> PytestConfigRecord: ...


class ExecutionController(Protocol):
    """The actual child launcher and its requested worker policy."""

    @property
    def workers(self) -> str | int | None: ...

    def run(
        self,
        pytest_args: list[str],
        environment: dict[str, str],
        *,
        stdout: Literal["inherit", "stderr"] = "inherit",
    ) -> int: ...


class ExecutionInvocation(Protocol):
    """Read-only controller inputs used by the shared execution owner."""

    @property
    def controller(self) -> ExecutionController: ...

    @property
    def configuration(self) -> ExecutionConfiguration: ...

    @property
    def history_path(self) -> str: ...

    @property
    def native_scheduling(self) -> str | None: ...


class TestRunSummary(NamedTuple):
    """Result of one controller-owned pytest invocation."""

    failed: tuple[str, ...]
    outcomes: Mapping[str, str]
    infrastructure_failed: bool

    @classmethod
    def from_rust(cls, value: TestRunRecord) -> TestRunSummary:
        return cls(
            tuple(value["failed"]),
            MappingProxyType(dict(value["outcomes"])),
            value["infrastructure_failed"],
        )


# Variables a controller sets for one child run. An outer vfy run's values must not
# reach a nested run that does not set them itself.
_CONTROLLER_ENVIRONMENT = (
    "VFY_SELECTED_TESTS",
    "VFY_CONFIRM_FAILURES",
    "VFY_CONFIRMATION_STAGE",
    "VFY_REPLAY_MERGE",
    "VFY_WATCH_STAGE",
    "VFY_CI_REQUEST",
    "VFY_CI_FINAL",
    "VFY_CI_COLLECTION",
    "VFY_CI_AUDIT",
    "VFY_CI_IGNORED",
)


def _configure_native_bridge_environment(
    environment: dict[str, str],
    route: str,
    workers: str | int | None = 1,
    native_scheduling: str | None = None,
) -> None:
    for name in (
        "VFY_NATIVE_PARALLEL_DIR",
        "VFY_NATIVE_PARALLEL_INDEX",
        "VFY_NATIVE_PARALLEL_RETAIN_OUTPUT",
        "VFY_NATIVE_PARALLEL_COVERAGE_BASE",
    ):
        environment.pop(name, None)
    if native_scheduling is not None and route in {"pytest-native", "native"}:
        environment["VFY_NATIVE_SCHEDULING"] = native_scheduling
    if route == "pytest-native":
        environment["VFY_NATIVE_COVERAGE_BRIDGE"] = "1"
        environment["VFY_NATIVE_WORKERS"] = str(workers or 1)
        # Native runs load their explicit adapter set; arbitrary installed
        # pytest11 entry points are not part of the native compatibility surface.
        disable_plugin_autoload(environment)
    else:
        environment.pop("VFY_NATIVE_COVERAGE_BRIDGE", None)
        environment.pop("VFY_NATIVE_WORKERS", None)


def _record_test_result(
    history_path: str,
    result_path: Path,
    run_id: str,
    attempt: str,
) -> TestRunSummary:
    return TestRunSummary.from_rust(
        record_test_execution(history_path, str(result_path), run_id, attempt)
    )


def _version_query(pytest_args: Sequence[str]) -> bool:
    """Match pytest's single ``--version`` or ``-V`` request, which records no evidence."""
    return pytest_args.count("--version") + pytest_args.count("-V") == 1


def _record_execution_evidence(
    args: argparse.Namespace,
    invocation: ExecutionInvocation,
    *,
    completed: int,
    primary_result_path: Path,
    temporary_path: Path,
    environment: dict[str, str],
    pytest_args: list[str],
    obsolete_check: Callable[[], bool] | None,
) -> bool:
    if invocation.configuration.projection["collect_only"]:
        return False
    # Every frontend's successful single-version response omits test evidence.
    if completed == 0 and _version_query(pytest_args) and not primary_result_path.exists():
        return False
    if (
        getattr(args, "_test_engine", "pytest-native") in {"pytest-native", "native"}
        and completed in {2, 3, 4, 5, -signal.SIGINT}
        and not primary_result_path.exists()
    ):
        # Pytest already reported why collection, interruption, an internal
        # error, usage validation, or an empty run stopped before results exist.
        # A failed test without results still needs the persistence diagnostic.
        return False
    history_path = invocation.history_path
    primary: TestRunSummary | None = None
    run_id = uuid.uuid4().hex
    try:
        primary = _record_test_result(history_path, primary_result_path, run_id, "primary")
    except (VfyError, OSError, RuntimeError, ValueError) as exc:
        _controller_message(args, f"Test result evidence could not be recorded: {exc}")
    if (
        args.confirm_failures
        and completed == 1
        and primary is not None
        and primary.failed
        and not primary.infrastructure_failed
        and not (obsolete_check is not None and obsolete_check())
    ):
        _controller_message(
            args,
            f"Confirming {len(primary.failed)} failed "
            f"test{'s' if len(primary.failed) != 1 else ''} in a fresh serial process",
        )
        candidates_path = temporary_path / "confirmation-tests.json"
        candidates_path.write_text(json.dumps(primary.failed), encoding="utf-8")
        confirmation_result_path = temporary_path / "confirmation-results.json"
        confirmation_environment = environment.copy()
        confirmation_environment["VFY_TEST_RESULTS"] = str(confirmation_result_path)
        confirmation_environment["VFY_CONFIRM_FAILURES"] = str(candidates_path)
        confirmation_environment["VFY_FORCE_PYTEST_SUBPROCESS"] = "1"
        confirmation_environment["VFY_CONFIRMATION_STAGE"] = str(temporary_path)
        route = TestEngine(args._test_engine)
        _configure_native_bridge_environment(confirmation_environment, route, workers=1)
        confirmation_environment.pop("VFY_REPLAY_MERGE", None)
        for name in (
            "VFY_CI_REQUEST",
            "VFY_CI_FINAL",
            "VFY_CI_COLLECTION",
            "VFY_CI_AUDIT",
        ):
            confirmation_environment.pop(name, None)
        confirmation_arguments = _confirmation_pytest_args(pytest_args, temporary_path, route=route)
        invocation.controller.run(confirmation_arguments, confirmation_environment)
        try:
            confirmation = _record_test_result(
                history_path,
                confirmation_result_path,
                run_id,
                "confirmation",
            )
        except (VfyError, OSError, RuntimeError, ValueError) as exc:
            _controller_message(
                args,
                "Failure confirmation could not be recorded; "
                f"the original run remains failed: {exc}",
            )
        else:
            _controller_message(args, _confirmation_message(primary, confirmation))
    return primary is not None and not primary.infrastructure_failed


def _execution_environment(
    args: argparse.Namespace, invocation: ExecutionInvocation
) -> dict[str, str]:
    environment = os.environ.copy()
    for name in _CONTROLLER_ENVIRONMENT:
        environment.pop(name, None)
    environment["VFY_TEST_HISTORY"] = invocation.history_path
    _configure_native_bridge_environment(
        environment,
        args._test_engine,
        invocation.controller.workers,
        invocation.native_scheduling,
    )
    if getattr(args, "watch", False):
        environment["VFY_FORCE_PYTEST_SUBPROCESS"] = "1"
    watch_pycache = getattr(args, "_watch_pycache_prefix", None)
    if watch_pycache is not None:
        environment["PYTHONPYCACHEPREFIX"] = str(watch_pycache)
        environment["VFY_WATCH_STAGE"] = str(Path(watch_pycache).parent)
    return environment


class Execution:
    """Own one launched test command and its temporary evidence."""

    def __init__(
        self,
        args: argparse.Namespace,
        invocation: ExecutionInvocation,
        pytest_args: list[str],
        temporary_path: Path,
        environment: dict[str, str],
        *,
        incremental: bool,
        selected: Sequence[str] | None,
    ) -> None:
        self.args = args
        self.invocation = invocation
        self.pytest_args = pytest_args
        self.temporary_path = temporary_path
        self.environment = environment
        self.run_data = temporary_path / ".coverage" if incremental else Path(args.data_file)
        self.environment["COVERAGE_FILE"] = str(self.run_data.resolve())
        self.primary_result_path = temporary_path / "primary-results.json"
        self.environment["VFY_TEST_RESULTS"] = str(self.primary_result_path)
        if selected is not None:
            selected_path = temporary_path / "selected-tests.json"
            selected_path.write_text(json.dumps(list(selected)), encoding="utf-8")
            self.environment["VFY_SELECTED_TESTS"] = str(selected_path)

    def run(self) -> int:
        return self.invocation.controller.run(self.pytest_args, self.environment)

    def record(self, completed: int, *, obsolete_check: Callable[[], bool] | None = None) -> bool:
        complete = _record_execution_evidence(
            self.args,
            self.invocation,
            completed=completed,
            primary_result_path=self.primary_result_path,
            temporary_path=self.temporary_path,
            environment=self.environment,
            pytest_args=self.pytest_args,
            obsolete_check=obsolete_check,
        )
        self.args._watch_execution_evidence_complete = complete
        return complete


@contextmanager
def prepare_execution(
    args: argparse.Namespace,
    invocation: ExecutionInvocation,
    pytest_args: list[str],
    *,
    incremental: bool = False,
    selected: Sequence[str] | None = None,
) -> Iterator[Execution]:
    """Keep process evidence and staged files alive until their caller finishes."""
    environment = _execution_environment(args, invocation)
    with tempfile.TemporaryDirectory(prefix="vfy-replay-") as temporary:
        yield Execution(
            args,
            invocation,
            pytest_args,
            Path(temporary),
            environment,
            incremental=incremental,
            selected=selected,
        )
