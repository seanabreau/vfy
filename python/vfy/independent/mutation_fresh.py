"""Fresh interpreter resources for the independent serial mutation command."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast, override

from vfy import native
from vfy.errors import NoDataError
from vfy.mutation_runner import CollectedMutationRunner
from vfy.pytest_options import coverage_config_argument, register_coverage_options

from .bootstrap import prepare_bootstrap
from .worker import result_status

if TYPE_CHECKING:
    from vfy.config_types import Config
    from vfy.mutate import Mutation, MutationCacheRecord, MutationResult
    from vfy.mutation_runner import MutationTestPlan
    from vfy.native_types import MutationBaselineCoveragePolicy

    from .coverage_support import CoverageOptions


class FreshMutationFallbackRunner(CollectedMutationRunner):
    """Use the existing portable scheduling path without importing pytest."""

    @staticmethod
    @override
    def available() -> bool:
        return False


def run_fresh(
    arguments: Sequence[str],
    timeout: float,
    mutation: Mutation | None = None,
    *,
    coverage_environment: Mapping[str, str] | None = None,
    requested_tests: Sequence[str] = (),
    collect_only: bool = False,
    cache_arguments: Sequence[str] | None = None,
) -> tuple[native._pytest.NativeRunResult | None, str | None, str, str]:
    """Pass one actual command to the existing authenticated worker supervisor."""
    environment = (
        dict(coverage_environment) if coverage_environment is not None else os.environ.copy()
    )
    # A serial mutation session owns a new invocation, including inside an xdist worker.
    for name in (
        "VFY_CLI_ACTIVE",
        "VFY_MUTATION",
        "VFY_REPLAY_MERGE",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        environment.pop(name, None)
    environment.pop("VFY_MUTATION_REPLAY", None)
    if coverage_environment is None:
        environment["VFY_MUTATION_REPLAY"] = "1"
        for name in (
            "VFY_PROCESS_OPTIONS",
            "VFY_PROCESS_START",
            "COVERAGE_FILE",
            "VFY_MUTATION_BASELINE_COVERAGE",
        ):
            environment.pop(name, None)
    snapshot: str | None = None

    def collected(
        _count: int,
        _reports: list[str],
        _warnings: list[tuple[str, str, int, str]],
        _errors: int,
        value: str | None,
        _finish_warnings: list[tuple[str, str, int, str]],
        _benchmark_fixture: bool,
    ) -> None:
        nonlocal snapshot
        snapshot = value

    with TemporaryDirectory(prefix="vfy-independent-mutation-") as directory:
        if collect_only:
            if mutation is not None or requested_tests:
                raise ValueError("fresh collection cannot request a mutation or selected tests")
            session = native._pytest.NativeParallelSession.fresh_collection(
                directory, timeout, arguments=list(arguments)
            )
        else:
            session = native._pytest.NativeParallelSession.fresh_mutation(
                directory,
                timeout,
                None if mutation is None else json.dumps(mutation.as_dict()),
                tests=list(requested_tests),
                cache_arguments=None if cache_arguments is None else list(cache_arguments),
            )
        command = [
            "-m",
            "vfy.independent.mutation_child",
            "-p",
            "vfy.independent.coverage_support",
            *arguments,
            *(["--collect-only"] if collect_only else []),
        ]
        command, environment = prepare_bootstrap(command, environment)

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result, outcome = session.run_mutation(
                sys.executable,
                command,
                environment,
                str(Path.cwd()),
                collected,
            )
    if result is None and outcome is None and snapshot is not None:
        result = native._pytest.NativeRunResult.from_independent_json(snapshot)
    return result, outcome, stdout.getvalue(), stderr.getvalue()


def collect_cache_scope(arguments: Sequence[str], timeout: float) -> native._pytest.NativeRunResult:
    """Retain the actual authenticated clean collection before any cache lookup."""
    result, _outcome, stdout, stderr = run_fresh(
        arguments, native.mutation_baseline_timeout(timeout), collect_only=True
    )
    if result is None or result_status(result) != 0:
        raise NoDataError("independent mutation cache collection did not pass\n" + stdout + stderr)
    return result


def collect_baseline(
    arguments: Sequence[str],
    timeout: float,
    data_file: str,
    sources: Sequence[str],
    rcfile: str | None,
    config: Config,
    *,
    coverage_policy: MutationBaselineCoveragePolicy,
    coverage_arguments: Sequence[str],
    history_path: str,
) -> native._pytest.NativeRunResult:
    """Combine child shards only after its native invocation was authenticated."""
    from vfy.api import Coverage  # noqa: PLC0415 - only the measured baseline needs coverage
    from vfy.config import (  # noqa: PLC0415 - preserve the actual report configuration
        load as load_config,
    )
    from vfy.process_startup import subprocess_environment  # noqa: PLC0415 - baseline resource
    from vfy.run.reports import (  # noqa: PLC0415 - baseline reporting shares the controller adapter
        _pytest_option,
        _write_coverage_reports,
    )

    from .protocol import (  # noqa: PLC0415 - baseline provisions the parsed pytest coverage mode
        Parser,
    )

    override = _pytest_option(coverage_arguments, "--cov-config")
    config_file = coverage_config_argument(override) if override is not None else rcfile or True
    coverage_config = (
        load_config(config_file if isinstance(config_file, str) else None)
        if override is not None
        else config
    )
    parser = Parser()
    register_coverage_options(parser)
    options = cast("CoverageOptions", argparse.Namespace(**parser.parse(coverage_arguments, False)))
    coverage = Coverage(
        data_file=data_file,
        source=(
            native.NativeTestInvocation.coverage_report_sources(coverage_arguments, sources)
            if coverage_policy == "requested"
            else list(sources) or None
        ),
        config_file=config_file,
        branch=options.cov_branch,
        decisions=options.cov_decisions,
    )
    with TemporaryDirectory(prefix="vfy-mutation-baseline-shards-") as directory:
        child_data = str(Path(directory) / Path(data_file).name)
        with subprocess_environment(coverage, data_file=child_data):
            environment = os.environ.copy()
            environment["VFY_MUTATION_BASELINE_COVERAGE"] = coverage_policy
            baseline_arguments = list(arguments)
            if coverage_policy != "disabled":
                baseline_arguments.extend(("--cov", "--cov-context=test", "--cov-report="))
            result, _outcome, stdout, stderr = run_fresh(
                [*baseline_arguments, "-x"],
                native.mutation_baseline_timeout(timeout),
                coverage_environment=environment,
            )
        if (
            result is None
            or result_status(result) != 0
            or result.failed
            or result.errors
            or result.collection_errors
            or not result.cache_outcomes()
        ):
            raise NoDataError(
                "automatic independent mutation baseline did not pass\n" + stdout + stderr
            )
        coverage.combine([directory], strict=True)
        coverage.save()
        if coverage_policy == "requested":
            report = io.StringIO()
            report_args = argparse.Namespace(
                data_file=data_file, rcfile=rcfile, history=history_path, isolated=False
            )
            with contextlib.redirect_stdout(report):
                passed = _write_coverage_reports(
                    report_args,
                    coverage_config,
                    list(coverage_arguments),
                    list(sources),
                    coverage.data._store,
                )
            if not passed:
                raise NoDataError(
                    "automatic independent mutation baseline did not pass\n" + report.getvalue()
                )
    destination = os.environ.get("PYTEST_NATIVE_RESULT")
    if destination:
        result.write_json(destination)
    return result


def evaluate(
    mutation: Mutation,
    plan: MutationTestPlan,
    execution_args: Sequence[str],
    timeout: float,
    preferred_tests: Sequence[str] = (),
    *,
    cache_evidence: bool = False,
) -> MutationResult:
    """Adapt the canonical Rust mutation result without reclassifying the child."""
    del preferred_tests
    from vfy.mutate import _result_from_dict  # noqa: PLC0415 - avoid the command adapter cycle

    result, outcome, _stdout, _stderr = run_fresh(
        [*execution_args, "-x"],
        timeout,
        mutation,
        requested_tests=plan.nodeids,
        cache_arguments=execution_args if cache_evidence else None,
    )
    if outcome is None:
        raise RuntimeError("fresh mutation did not return its authenticated mutation result")
    return replace(
        _result_from_dict(cast("MutationCacheRecord", json.loads(outcome))),
        _native_result=result if cache_evidence else None,
    )


def evaluate_mutations(
    pending: Sequence[tuple[Mutation, MutationTestPlan]],
    execution_args: Sequence[str],
    timeout: float,
    context_data: str,
    record: Callable[[str, MutationResult], None],
    *,
    cache_evidence: bool,
    collection: native._pytest.NativeRunResult
    | native._pytest.NativeIndependentSession
    | None = None,
) -> int:
    """Adapt one native mutation batch and its fresh-process lifecycle boundary."""
    from vfy.mutate import (  # noqa: PLC0415 - avoid command adapter cycle
        _evaluate,
        _result_from_dict,
    )
    from vfy.mutation_runner import (  # noqa: PLC0415 - fresh plan adapter
        MutationTestPlan,
        MutationTestScope,
    )
    from vfy.run.engine import (  # noqa: PLC0415 - selected engine lifecycle
        TestEngine,
    )

    def fresh(index: int, tests: list[str]) -> None:
        mutation, plan = pending[index]
        actual = (
            plan
            if plan.scope is MutationTestScope.UNREACHED
            else MutationTestPlan(
                MutationTestScope.SELECTED if tests else MutationTestScope.FULL_SUITE, tuple(tests)
            )
        )
        record(
            mutation.key,
            _evaluate(
                mutation,
                actual,
                execution_args,
                timeout,
                engine=TestEngine.INDEPENDENT,
                cache_evidence=cache_evidence,
            ),
        )

    def collected(index: int, outcome: str, result: native._pytest.NativeRunResult | None) -> None:
        record(
            pending[index][0].key,
            replace(
                _result_from_dict(cast("MutationCacheRecord", json.loads(outcome))),
                _native_result=result if cache_evidence else None,
            ),
        )

    requests = _mutation_requests(pending)
    if isinstance(collection, native._pytest.NativeRunResult):
        for index, tests in enumerate(
            native._pytest.fresh_mutation_test_plans(requests, context_data)
        ):
            fresh(index, tests)
        return 0
    if isinstance(collection, native._pytest.NativeIndependentSession):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return collection.run_mutations(
                requests, list(execution_args), timeout, context_data, fresh, collected
            )
    if not native._pytest.has_collectable_mutations(requests, context_data):
        for index in range(len(pending)):
            fresh(index, [])
        return 0
    with (
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
        _mutation_session(execution_args) as session,
    ):
        return session.run_mutations(
            requests, list(execution_args), timeout, context_data, fresh, collected
        )


def _mutation_requests(pending: Sequence[tuple[Mutation, MutationTestPlan]]) -> str:
    return json.dumps(
        [
            {"mutation": mutation.as_dict(), "tests": list(plan.nodeids), "scope": plan.scope.value}
            for mutation, plan in pending
        ]
    )


@contextlib.contextmanager
def _replay_coverage_scope() -> Iterator[None]:
    """Bind execution-only coverage while retaining the authenticated plugin argv."""
    previous = os.environ.get("VFY_MUTATION_REPLAY")
    os.environ["VFY_MUTATION_REPLAY"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("VFY_MUTATION_REPLAY", None)
        else:
            os.environ["VFY_MUTATION_REPLAY"] = previous


@contextlib.contextmanager
def _mutation_session(
    execution_args: Sequence[str],
    *,
    cache: bool = False,
    timeout: float = 30.0,
) -> Iterator[native._pytest.NativeIndependentSession]:
    from .session import (  # noqa: PLC0415 - native collection lifetime
        IndependentSession,
    )

    with (
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
        _replay_coverage_scope(),
    ):
        owner = IndependentSession(
            ["-p", "vfy.independent.coverage_support", *execution_args, "-x"],
            mutation_arguments=execution_args if cache else None,
            mutation_timeout=timeout,
            mutation_guard=True,
        )
    try:
        if owner.native is None:
            raise RuntimeError("independent mutation collection has no native session")
        yield owner.native
    finally:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            owner.close()


def collect_reusable_scope(
    request: str,
    pending: Sequence[tuple[Mutation, MutationTestPlan]],
    execution_args: Sequence[str],
    timeout: float,
    context_data: str,
    resources: contextlib.ExitStack,
) -> tuple[native._pytest.NativeIndependentSession, tuple[str, dict[str, str]]] | None:
    """Retain the collection owner through cache reuse and execution."""
    plans = _mutation_requests(pending)
    if not native._pytest.has_collectable_mutations(plans, context_data):
        return None
    with contextlib.ExitStack() as owned:
        session = owned.enter_context(
            _mutation_session(execution_args, cache=True, timeout=timeout)
        )
        partition = session.prepare_mutation_cache(request, plans, context_data)
        if partition is None:
            return None
        resources.enter_context(owned.pop_all())
    return session, partition
