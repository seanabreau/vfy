"""CPython bootstrap for the independent driver's Rust worker supervisor."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from uuid import uuid4

from vfy import native
from vfy._pytest.plugin_support import (
    worker_seed_environment,
    worker_seeds,
)

from .bootstrap import StartupOptions, prepare_bootstrap

if TYPE_CHECKING:
    from _pytest.config import Config as PytestConfig

    from .protocol import Config as IndependentConfig
    from .terminal import TerminalReporter

_USAGE_ERRORS = (native._pytest.IndependentUsageError, native._pytest.UnsupportedFeatureError)


class WorkerCollection:
    """Retain the worker handle across collection, execution, and shutdown."""

    def __init__(
        self, directory: str, index: int, owner: native._pytest.NativeIndependentInvocation
    ) -> None:
        self.owner = owner
        self.directory = directory
        self.index = index
        self.worker: native._pytest.NativeParallelWorker | None = None
        self.aborted = False
        self.options: dict[str, object] | None = None

    def __call__(
        self, nodeids: list[str], collection: native._pytest.NativeRunResult
    ) -> list[str] | None:
        native._pytest.NativeParallelWorker.publish_independent_collection(
            self.directory, self.index, nodeids, collection, invocation=self.owner
        )
        if collection.collection_errors:
            session = self.owner.pytest_session
            if session is None:
                raise RuntimeError("independent collector has no configured session")
            if not session.config.getoption("continue_on_collection_errors", False):
                return None
        self.worker = native._pytest.NativeParallelWorker.independent(
            self.directory, self.index, nodeids
        )
        if self.worker is None:
            self.aborted = True
            if self.options is not None:
                # A released surplus worker collected successfully and has no assignment.
                released = native._pytest.NativeParallelWorker.surplus(self.directory, self.index)
                if released:
                    self.owner.mark_worker_surplus()
                else:
                    self.options["_collection_aborted"] = True
            return []
        self.owner.bind_worker_assignment(self.worker)
        return self.worker.prepare_independent(nodeids)


def result_status(result: native._pytest.NativeRunResult) -> int:
    """Return the independent driver's public session status."""
    if result.exitstatus is not None:
        return result.exitstatus
    if result.collection_errors:
        return 2
    if result.failed or result.errors:
        return 1
    return 0 if result.collected else 5


def _publish_xdist_worker(index: int) -> dict[str, object] | None:
    """Publish pytest-xdist's worker environment and return its ``workerinput``.

    Only parallel controllers set the worker count; fresh serial workers, such as
    mutation runs, are not xdist workers and publish nothing.
    """
    count = os.environ.get("PYTEST_XDIST_WORKER_COUNT")
    if count is None:
        return None
    worker_id = f"gw{index}"
    os.environ["PYTEST_XDIST_WORKER"] = worker_id
    workerinput: dict[str, object] = {
        "workerid": worker_id,
        "workercount": int(count),
        "testrunuid": os.environ["PYTEST_XDIST_TESTRUNUID"],
        "mainargv": json.loads(os.environ["VFY_NATIVE_MAINARGV"]),
    }
    workerinput.update(worker_seeds())
    return workerinput


def run_worker(
    paths: list[str],
    root: str,
    keyword: str | None,
    basetemp: str | None,
    options: dict[str, object],
    *,
    invocation: native._pytest.NativeIndependentInvocation | None = None,
) -> int:
    """Finish native lifecycle cleanup before publishing worker completion."""
    owner = invocation or native._pytest.NativeIndependentInvocation()
    worker = WorkerCollection(
        os.environ["VFY_NATIVE_PARALLEL_DIR"], int(os.environ["VFY_NATIVE_PARALLEL_INDEX"]), owner
    )
    worker_options = options.copy()
    worker.options = worker_options
    worker_options["_collection_callback"] = worker
    worker_options["_worker_basetemp_index"] = worker.index
    worker_options["_workerinput"] = _publish_xdist_worker(worker.index)
    worker_basetemp = str(Path(basetemp) / f"worker-{worker.index}") if basetemp else None
    if invocation is None:
        owner.bind_worker(worker.directory, worker.index)
    try:
        try:
            result = (
                owner.execute(paths, root, options=worker_options)
                if "_arguments" in worker_options
                else owner.run(
                    paths,
                    root,
                    keyword,
                    collect_only=bool(worker_options.get("collect_only", False)),
                    basetemp=worker_basetemp,
                    options=worker_options,
                )
            )
            if result is None:
                if owner.exitstatus is None:
                    raise RuntimeError("independent worker command has no exit status")
                return owner.exitstatus
        except BaseException as error:
            if worker.index == 0 and isinstance(error, _USAGE_ERRORS):
                # Every worker collects the same selection and raises the same usage error.
                # Worker 0 writes it once, before publishing, so the controller's replay sees it.
                sys.stderr.write(f"vfy independent usage error: {error}\n")
            if owner.disposition != "return":
                # A peer interruption outside any session handler has no owned status to
                # publish; the original error ends the process as CPython reports it.
                raise
            if worker.index == 0 and not isinstance(error, _USAGE_ERRORS):
                from .entry import (  # noqa: PLC0415 - handled startup error only
                    _report_startup_error,
                )

                _report_startup_error(error)
            return owner.publish_worker_failure(error)
        return owner.publish_worker_result(result, worker.worker)
    finally:
        if invocation is None:
            owner.close()


def _write_worker_status(reporter: TerminalReporter | None, workers: int, items: int) -> None:
    """Write the bring-up lines that pytest-xdist prints before progress.

    Without a terminal, xdist writes its setup line and its collection line in
    order, followed by a blank line.
    """
    if reporter is None:
        return
    if reporter.config.option.verbose < 0:
        setup = collection = "bringing up nodes..."
    else:
        noun = "worker" if workers == 1 else "workers"
        item = "item" if items == 1 else "items"
        setup = f"created: {workers}/{workers} {noun}"
        collection = f"{workers} {noun} [{items} {item}]"
    reporter.write_line(setup)
    reporter.write_line(collection)
    reporter.write_line("")


def run_parallel(
    arguments: list[str],
    root: str,
    workers: int,
    *,
    config: IndependentConfig | PytestConfig,
    maxfail: int = 0,
    collect_only: bool = False,
    continue_on_collection_errors: bool = False,
    coverage_options: Mapping[str, object] | None = None,
    reporter: TerminalReporter | None = None,
    startup: StartupOptions | None = None,
) -> native._pytest.NativeRunResult:
    """Launch collection workers through the authoritative Rust supervisor.

    Native owned completions preserve upstream controls and final session status.
    Missing, modified, or unexpected worker publications are infrastructure
    errors (status 3); an OS status on NativeParallelError is diagnostic only.
    The controller alone writes terminal output for the workers.
    """
    collection_snapshot: str | None = None

    def collected(
        count: int,
        _reports: list[str],
        _warnings: list[tuple[str, str, int, str]],
        errors: int,
        snapshot: str | None,
        _finish_warnings: list[tuple[str, str, int, str]],
        _benchmark_fixture: bool,
    ) -> None:
        nonlocal collection_snapshot
        collection_snapshot = snapshot
        if errors == 0 or continue_on_collection_errors:
            _write_worker_status(reporter, workers, count)

    environment = {
        **os.environ,
        "PYTEST_XDIST_WORKER_COUNT": str(workers),
        "VFY_NATIVE_MAINARGV": os.environ.get("VFY_NATIVE_MAINARGV") or json.dumps(sys.argv),
        "PYTEST_XDIST_TESTRUNUID": os.environ.get("PYTEST_XDIST_TESTRUNUID") or uuid4().hex,
    }
    worker_seed_environment(config, environment)
    command, environment = prepare_bootstrap(
        ["-m", "vfy.independent.entry", *arguments],
        environment,
        startup=startup,
        coverage_options=coverage_options,
    )
    with TemporaryDirectory(prefix="vfy-independent-") as directory:
        session = native._pytest.NativeParallelSession.bootstrap(
            workers,
            directory,
            maxfail,
            0,
            collect_only=collect_only,
            continue_on_collection_errors=continue_on_collection_errors,
            engine="vfy-independent-v1",
        )
        if reporter is not None:
            session.report_progress(reporter.config)
        result = session.run_bootstrap(
            sys.executable, command, environment, str(Path(root)), collected
        )
    if result is not None:
        return result
    if collection_snapshot is None:
        raise RuntimeError("independent workers did not publish a collection snapshot")
    return native._pytest.NativeRunResult.from_independent_json(collection_snapshot)
