"""Start native workers in fresh interpreters or forked collection snapshots."""

from __future__ import annotations

import argparse
import contextlib
import inspect
import os
import signal
import stat
import sys
import tempfile
import threading
from collections import deque
from collections.abc import Callable
from enum import Enum
from functools import partial
from io import TextIOWrapper
from pathlib import Path
from types import BuiltinFunctionType, CodeType, FunctionType, GeneratorType, MethodType, ModuleType
from typing import TYPE_CHECKING, NoReturn, TextIO, cast

import pytest
from _pytest import (
    assertion as pytest_assertion,
    debugging as pytest_debugging,
    doctest as pytest_doctest,
    faulthandler as pytest_faulthandler,
    junitxml as pytest_junitxml,
    legacypath as pytest_legacypath,
    logging as pytest_logging,
    monkeypatch as pytest_monkeypatch,
    threadexception as pytest_threadexception,
    unittest as pytest_unittest,
    unraisableexception as pytest_unraisableexception,
    warnings as pytest_warnings,
)
from _pytest._code import ExceptionInfo
from _pytest.capture import CaptureManager, FDCapture, MultiCapture, SysCapture
from _pytest.compat import NOTSET
from _pytest.config import Config
from _pytest.logging import get_option_ini
from _pytest.monkeypatch import MonkeyPatch
from _pytest.python import Metafunc

from vfy.native import _pytest
from vfy.pytest_metadata import metadata_key
from vfy.workers import requested_worker_collection, worker_count

from . import compact_collection, lazy_function, parameter_family
from .plugin_support import worker_seed_environment, worker_seeds

if TYPE_CHECKING:
    from vfy._pytest_native import (
        NativeExecutionSession,
        NativeParallelSession,
        NativeParallelWorker,
        NativeRunResult,
    )


class ForkStart(Enum):
    """How a collected-fork run was selected, which sets its leftover-thread policy."""

    EXPLICIT = "explicit"
    AUTOMATIC = "automatic"


_worker_terminal = pytest.StashKey[TextIO]()
_worker_html = pytest.StashKey[bool]()
_automatic_fork_candidate = pytest.StashKey[bool]()
_collected_fork_child = pytest.StashKey[ForkStart]()
_collected_fork_controller = pytest.StashKey["NativeRunResult"]()
_MONKEYPATCH_NOTSET: object = getattr(pytest_monkeypatch, "notset", NOTSET)

_fork_os_functions = tuple(
    (name, getattr(os, name, None))
    for name in (
        "fork",
        "pipe",
        "read",
        "write",
        "waitpid",
        "setpgid",
        "_exit",
        "close",
        "dup2",
        "open",
    )
)


def is_collected_fork_child(config: pytest.Config) -> bool:
    return config.stash.get(_collected_fork_child, None) is not None


def is_collected_fork_controller(config: pytest.Config, result: NativeRunResult) -> bool:
    return config.stash.get(_collected_fork_controller, None) is result


def is_worker() -> bool:
    return "VFY_NATIVE_PARALLEL_DIR" in os.environ


def is_parallel() -> bool:
    return is_worker() or os.environ.get("VFY_NATIVE_WORKERS", "1") != "1"


def worker_collection_enabled(config: pytest.Config) -> bool:
    """Let explicit fresh workers collect without repeating collection in the parent."""
    if (
        config.option.collectonly
        or config.option.help
        or config.option.continue_on_collection_errors
    ):
        return False
    policy = os.environ.get("VFY_WORKER_COLLECTION")
    if policy is not None:
        return policy == "1"
    return (
        requested_worker_collection(
            os.environ.get("VFY_NATIVE_WORKERS", "1"),
            policy=policy,
            worker_start=os.environ.get("VFY_WORKER_START"),
            mutation_isolation=os.environ.get("VFY_MUTATION_TEST_ISOLATION"),
        )
        and compact_collection.native_mutation_controller(config) is None
        and not automatic_fork_candidate(config)
    )


def automatic_fork_candidate(config: pytest.Config) -> bool:
    """Decide before collection whether explicit workers should fork a collected suite.

    Only the default start mode and an unset collection policy qualify; either
    variable selects its route explicitly. The decision reads invocation options
    alone, so a run that cannot fork never pays for a controller collection. Checks
    that need collected items, live threads or descriptors run in
    ``_admit_collected_fork`` and fall back to fresh workers.
    """
    if (
        os.environ.get("VFY_WORKER_START") is not None
        or os.environ.get("VFY_WORKER_COLLECTION") is not None
    ):
        return False
    cached = config.stash.get(_automatic_fork_candidate, None)
    if cached is None:
        cached = _options_allow_automatic_fork(config)
        config.stash[_automatic_fork_candidate] = cached
    return cached


def _options_allow_automatic_fork(config: pytest.Config) -> bool:
    workers = os.environ.get("VFY_NATIVE_WORKERS", "1")
    return (
        workers.isdecimal()
        and int(workers) >= 2
        and not os.environ.get("VFY_MUTATION_TEST_ISOLATION")
        and not os.environ.get("VFY_WATCH_STAGE")
        and sys.platform == "linux"
        and sys.implementation.name == "cpython"
        and (not hasattr(sys, "_is_gil_enabled") or sys._is_gil_enabled())
        and config.getoption("capture") in {"fd", "sys", "no"}
        and not getattr(config.option, "htmlpath", None)
        and not getattr(config.option, "numprocesses", None)
        and _serial_logging_reason(config) is None
    )


def is_worker_collection_controller(config: pytest.Config) -> bool:
    return worker_collection_enabled(config) and not is_worker()


def validate_worker_collection(config: pytest.Config) -> None:
    if not worker_collection_enabled(config):
        return
    workers = os.environ.get("VFY_NATIVE_WORKERS", "1")
    if not is_worker() and (not workers.isdecimal() or int(workers) < 2):
        raise pytest.UsageError(
            "experimental worker collection requires at least two explicit workers"
        )
    if getattr(config.option, "stepwise", False):
        raise pytest.UsageError("native stepwise execution requires --workers=1")
    if os.environ.get("VFY_MUTATION_TEST_ISOLATION"):
        raise pytest.UsageError(
            "experimental worker collection does not support mutation isolation"
        )


def _validate_startup_grace() -> None:
    if "VFY_NATIVE_STARTUP_GRACE_MS" not in os.environ:
        return
    try:
        grace = _pytest.NativeParallelSession.startup_grace_ms()
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    if grace and os.environ.get("VFY_NATIVE_SCHEDULING") != "demand":
        raise pytest.UsageError("native startup grace requires demand scheduling")


def prepare_worker(config: pytest.Config) -> None:
    """Validate serial logging and keep worker reports private."""
    if not is_worker():
        if config.option.help or config.option.collectonly:
            return
        workers = os.environ.get("VFY_NATIVE_WORKERS", "1")
        _validate_startup_grace()
        if os.environ.get("VFY_NATIVE_SCHEDULING") == "demand" and (
            os.environ.get("VFY_WORKER_START") != "fork"
            or not workers.isdecimal()
            or int(workers) < 2
            or os.environ.get("VFY_WORKER_COLLECTION") == "1"
        ):
            raise pytest.UsageError(
                "demand scheduling requires collected fork and at least two explicit workers"
            )
        if os.environ.get("VFY_WORKER_START") == "fork":
            if sys.platform != "linux" or not workers.isdecimal() or int(workers) < 2:
                raise pytest.UsageError(
                    "collected fork workers require Linux and at least two explicit workers"
                )
            if os.environ.get("VFY_WORKER_COLLECTION") == "1":
                raise pytest.UsageError("collected fork workers require controller collection")
        reason = _serial_logging_reason(config)
        if workers not in {"1", "auto", "logical"} and reason is not None:
            # Validate before pytest's logging plug-in opens the shared file.
            raise pytest.UsageError(f"native pytest requires --workers=1 with {reason}")
        if workers not in {"1", "auto", "logical"}:
            implementation = next(
                (
                    hook
                    for hook in config.hook.pytest_configure.get_hookimpls()
                    if hook.plugin_name == "logging"
                ),
                None,
            )
            if implementation is not None:
                original = implementation.function

                def configure_logging(config: pytest.Config) -> None:
                    # Conftest may enable logging after the native early guard.
                    reason = _serial_logging_reason(config)
                    if reason is not None:
                        raise pytest.UsageError(f"native pytest requires --workers=1 with {reason}")
                    original(config)

                # Pytest already copied this HookImpl into the current dispatch;
                # later registration cannot intercept logging. Restore it at cleanup.
                setattr(implementation, "function", configure_logging)  # noqa: B010
                config.add_cleanup(lambda: setattr(implementation, "function", original))
        return
    basetemp = config.option.basetemp
    for name, seed in worker_seeds().items():
        setattr(config.option, name, seed)
    if basetemp is not None and config.pluginmanager.hasplugin("tmpdir"):
        config.option.basetemp = str(
            Path(basetemp) / f"native-worker-{os.environ['VFY_NATIVE_PARALLEL_INDEX']}"
        )
    config.stash[_worker_html] = bool(config.option.htmlpath)
    config.option.htmlpath = None
    if config.option.xmlpath:
        # Collection failures still invoke pytest's JUnit writer before native
        # execution can unregister it; preserve its options and properties privately.
        config.option.xmlpath = str(
            Path(os.environ["VFY_NATIVE_PARALLEL_DIR"])
            / f"junit.{os.environ['VFY_NATIVE_PARALLEL_INDEX']}.xml"
        )
    prepare_worker_coverage(config.option)


def _serial_logging_reason(config: pytest.Config) -> str | None:
    if not config.pluginmanager.hasplugin("logging"):
        return None
    if get_option_ini(config, "log_file"):
        return "file logging (--log-file or log_file)"
    if config.pluginmanager.hasplugin("terminal") and (
        config.option.log_cli_level is not None or config.getini("log_cli")
    ):
        return "live logging (--log-cli-level or log_cli)"
    return None


def prepare_worker_coverage(options: argparse.Namespace) -> None:
    """Leave coverage thresholds and report rendering to the controller."""
    options.cov_report = {"": None}
    options.cov_fail_under = 0
    options.cov_fail_under_decisions = None
    options.cov_fail_under_conditions = None
    options.cov_fail_under_mcdc = None
    options.cov_decision_report = None


def worker_html_enabled(config: pytest.Config) -> bool:
    """Retain HTML attachments while workers leave publication to the controller."""
    return config.stash.get(_worker_html, False)


def worker_metadata(config: pytest.Config) -> list[tuple[str, str]] | None:
    if not worker_html_enabled(config):
        return None
    return [(str(key), str(value)) for key, value in config.stash[metadata_key].items()]


def silence_worker_terminal(config: pytest.Config) -> None:
    """Suppress duplicate framework output without redirecting test streams."""
    if not is_worker():
        return
    terminal = config.pluginmanager.getplugin("terminalreporter")
    if terminal is not None:
        # The config owns this spool across session hooks, until unconfigure.
        stream = tempfile.TemporaryFile(  # noqa: SIM115 - pytest owns cleanup across hook calls
            mode="w+", encoding="utf-8", newline=""
        )
        config.add_cleanup(stream.close)
        config.stash[_worker_terminal] = stream
        terminal._tw._file = stream


def finish_worker(session: pytest.Session) -> None:
    """Expose worker failures and retain diagnostics emitted during unconfigure."""
    stream = session.config.stash.get(_worker_terminal, None)
    if stream is None:
        return
    if (
        session.exitstatus not in {pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED}
        or sys.exc_info()[0] is not None
    ):
        stream.seek(0)
        while chunk := stream.read(65536):
            sys.stderr.write(chunk)
    terminal = session.config.pluginmanager.getplugin("terminalreporter")
    if terminal is not None and terminal._tw._file is stream:
        # Pytest renders exits with custom return codes during unconfigure,
        # after sessionfinish has drained the worker's hidden progress.
        terminal._tw._file = sys.stderr


def open_worker(session: pytest.Session) -> NativeParallelWorker:
    try:
        return _pytest.NativeParallelWorker(
            os.environ["VFY_NATIVE_PARALLEL_DIR"],
            int(os.environ["VFY_NATIVE_PARALLEL_INDEX"]),
            [item.nodeid for item in session.items],
        )
    except _pytest.NativeParallelError as error:
        if error.returncode == pytest.ExitCode.INTERRUPTED:
            raise session.Interrupted(str(error)) from error
        raise


def run(
    session: pytest.Session,
    maxfail: int,
    initial_failures: int,
    *,
    retain_success_output: bool,
) -> NativeRunResult | None:
    """Run workers, or keep automatic serial execution in the current session."""
    raw_workers = os.environ["VFY_NATIVE_WORKERS"]
    reason = _serial_logging_reason(session.config)
    if reason is not None:
        if raw_workers not in {"auto", "logical"}:
            # Conftest configuration can enable logging after the early guard.
            raise pytest.UsageError(f"native pytest requires --workers=1 with {reason}")
        sys.stderr.write(f"native pytest uses one worker for {reason}\n")
        return None
    requested = int(raw_workers) if raw_workers.isdecimal() else raw_workers
    width = worker_count(requested, len(session.items))
    with tempfile.TemporaryDirectory(prefix="vfy-native-workers-") as directory:
        coordinator = _pytest.NativeParallelSession(
            [item.nodeid for item in session.items],
            width,
            directory,
            maxfail,
            initial_failures,
            worker_mode="automatic" if raw_workers in {"auto", "logical"} else "explicit",
        )
        if raw_workers in {"auto", "logical"} and coordinator.planned_worker_count == 1:
            return None
        coordinator.report_progress(session.config)

        requested_start = os.environ.get("VFY_WORKER_START")
        if requested_start not in {None, "exec", "fork"}:
            raise pytest.UsageError("VFY_WORKER_START must be fork or exec")
        fork_start: ForkStart | None = None
        if requested_start == "fork":
            fork_start = ForkStart.EXPLICIT
        elif requested_start is None and os.environ.get("VFY_WORKER_COLLECTION") is None:
            fork_start = ForkStart.AUTOMATIC
        if fork_start is not None:
            admitted = _admit_fork_start(session, fork_start)
            if admitted is not None:
                return _run_collected_fork(
                    session,
                    coordinator,
                    admitted,
                    fork_start,
                    retain_success_output=retain_success_output,
                )

        environment = _worker_environment(session, retain_success_output)
        try:
            result = coordinator.run(
                sys.executable,
                ["-m", "pytest", *session.config.invocation_params.args],
                environment,
                str(session.config.invocation_params.dir),
            )
        except _pytest.NativeParallelError as exc:
            _exit_parallel_error(session, exc)
    return result


def _admit_fork_start(session: pytest.Session, start: ForkStart) -> list[int] | None:
    """Return the owned descriptors for a fork, or ``None`` when fresh workers must run.

    An explicit request reports every rejection. An automatic start decides
    before any worker exists, so a rejected snapshot falls back to fresh workers
    with the same assignments.
    """
    match start:
        case ForkStart.EXPLICIT:
            return _admit_collected_fork(session)
        case ForkStart.AUTOMATIC:
            if compact_collection.native_mutation_controller(session.config) is not None:
                return None
            try:
                allowed_fds = _admit_collected_fork(session)
                _pytest.NativeParallelSession.check_collected_fork(allowed_fds)
            except (pytest.UsageError, ValueError):
                return None
            return allowed_fds


def _run_collected_fork(
    session: pytest.Session,
    coordinator: NativeParallelSession,
    allowed_fds: list[int],
    start: ForkStart,
    *,
    retain_success_output: bool,
) -> NativeRunResult:
    retain = retain_success_output or bool(os.environ.get("PYTEST_NATIVE_RESULT"))
    prepared = None
    groups = None
    if os.environ.get("VFY_NATIVE_SCHEDULING") == "demand":
        # native_pytest imports this module during initialization.
        from . import native_pytest  # noqa: PLC0415

        # The bootstrap closure owns the compiled snapshot until child os._exit;
        # Config and Items must not retain it.
        observer = None
        if native_pytest._vfy_bridge_plugin_enabled(session.config):
            _, observer = native_pytest._native_bridge_adapters(session)
        prepared = _pytest.NativeExecutionSession.prepare_collected(
            session.items,
            capture=session.config.getoption("capture") != "no",
            vfy_results=observer,
            retain_success_output=retain,
            retain_report_locations=native_pytest._retains_native_junit_locations(session.config),
            retain_capture_report_locations=native_pytest._retains_native_capture_locations(
                session.config
            ),
        )
        groups = prepared.demand_groups()
        # Metadata callbacks can change the reviewed process and cleanup state.
        allowed_fds = _admit_collected_fork(session)

    def bootstrap(directory: str, index: int, worker: NativeParallelWorker) -> int:
        return _collected_child(session, directory, index, worker, retain, prepared, start)

    coverage = session.config.pluginmanager.getplugin("_cov")
    coverage_base = None if coverage is None else coverage._native_parallel_base()
    try:
        result = coordinator.run_collected(bootstrap, allowed_fds, groups, coverage_base)
    except (_pytest.NativeParallelError, ValueError) as exc:
        if isinstance(exc, _pytest.NativeParallelError):
            _exit_parallel_error(session, exc)
        raise pytest.UsageError(str(exc)) from exc
    session.config.stash[_collected_fork_controller] = result
    return result


def _exit_parallel_error(session: pytest.Session, error: _pytest.NativeParallelError) -> NoReturn:
    # No accepted result owns publication after a supervisor failure. Remove the
    # empty stock reporter before sessionfinish snapshots its hook implementations.
    from . import native_pytest  # noqa: PLC0415

    native_pytest._disable_junit_reporter(session.config)
    pytest.exit(str(error), returncode=error.returncode)


def _worker_environment(session: pytest.Session, retain_success_output: bool) -> dict[str, str]:
    if session.config.option.basetemp is not None and session.config.pluginmanager.hasplugin(
        "tmpdir"
    ):
        # Pytest owns the requested base; workers own disjoint children of it.
        factory = getattr(session.config, "_tmp_path_factory", None)
        if not isinstance(factory, pytest.TempPathFactory):
            raise pytest.UsageError(
                "native workers with --basetemp require pytest's tmpdir plug-in"
            )
        factory.getbasetemp()
    environment = os.environ.copy()
    worker_seed_environment(session.config, environment)
    # Command-line optimization flags are absent from the inherited environment.
    environment["PYTHONOPTIMIZE"] = str(sys.flags.optimize)
    environment["VFY_NATIVE_PARALLEL_RETAIN_CAPTURE_LOCATIONS"] = (
        "1" if environment.get("PYTEST_NATIVE_RESULT") else "0"
    )
    # Only the coordinator writes the execution record, but worker phases
    # must retain the passing output that record consumes.
    retain_success_output = retain_success_output or bool(environment.get("PYTEST_NATIVE_RESULT"))
    environment["VFY_NATIVE_PARALLEL_RETAIN_OUTPUT"] = "1" if retain_success_output else "0"
    # Worker pytest owns collection coverage; automatic process startup would
    # create an enclosing Coverage instance before that adapter can start.
    for name in (
        "VFY_PROCESS_START",
        "VFY_PROCESS_OPTIONS",
        "VFY_TEST_HISTORY",
        "VFY_WATCH_STAGE",
        "PYTEST_NATIVE_RESULT",
    ):
        environment.pop(name, None)
    coverage = session.config.pluginmanager.getplugin("_cov")
    if coverage is not None:
        base = coverage._native_parallel_base()
        if base is not None:
            environment["VFY_NATIVE_PARALLEL_COVERAGE_BASE"] = base
    return environment


def run_bootstrap(
    session: pytest.Session,
    maxfail: int,
    initial_failures: int,
    collection_callback: Callable[
        [
            int,
            list[str],
            list[tuple[str, str, int, str]],
            int,
            str | None,
            list[tuple[str, str, int, str]],
            bool,
        ],
        None,
    ],
    *,
    retain_success_output: bool,
) -> NativeRunResult | None:
    """Launch collectors before Rust selects and publishes their execution plan."""
    reason = _serial_logging_reason(session.config)
    if reason is not None:
        raise pytest.UsageError(f"native pytest requires --workers=1 with {reason}")
    with tempfile.TemporaryDirectory(prefix="vfy-native-workers-") as directory:
        coordinator = _pytest.NativeParallelSession.bootstrap(
            int(os.environ["VFY_NATIVE_WORKERS"]),
            directory,
            maxfail,
            initial_failures,
        )
        coordinator.report_progress(session.config)
        environment = _worker_environment(session, retain_success_output)
        # The launcher records completion after unconfigure, including collection
        # failures before a worker can open its execution plan.
        environment["VFY_WORKER_COLLECTION"] = "1"
        try:
            return coordinator.run_bootstrap(
                sys.executable,
                ["-m", "vfy._pytest.worker_main", *session.config.invocation_params.args],
                environment,
                str(session.config.invocation_params.dir),
                collection_callback,
            )
        except _pytest.NativeParallelError as exc:
            _exit_parallel_error(session, exc)


def _fork_coverage_is_admissible(config: pytest.Config) -> bool:
    """Accept disabled coverage or a plain native session whose collector can restart in a child.

    A child restarts coverage through the at-fork hook with a fresh collector and
    an empty store, so a plug-in's tracer or context switcher state and an
    enclosing session's data would be inherited without an owner.
    """
    coverage = config.pluginmanager.getplugin("_cov")
    if coverage is None:
        return True
    from vfy.pytest_plugin import VfyCovPlugin  # noqa: PLC0415 - plug-in imports parallel

    if type(coverage) is not VfyCovPlugin or coverage.config is not config:
        return False
    if coverage._disabled:
        return (
            coverage.options.no_cov is True
            and config.getoption("no_cov", default=False) is True
            and coverage.coverage is None
            and coverage._started is False
            and coverage._subprocess_scope is None
            and coverage._native_child_data is None
        )
    # Disabled runs must not import the coverage API. Coverage's public adapter
    # imports parallel while configuring native workers.
    from vfy.api import Coverage  # noqa: PLC0415 - api imports the pytest plug-in chain

    collector = coverage.coverage
    return (
        type(collector) is Coverage
        and coverage.options.no_cov is False
        and coverage._native_bridge
        and coverage._started
        and not coverage._automatic
        and not coverage._finished
        and not coverage._is_worker
        and not coverage._is_distributed
        and not coverage._fork_worker
        and not coverage._native_disabled_call
        and coverage._subprocess_scope is not None
        and coverage._native_child_data is not None
        and collector._collector is not None
        and not collector._collection_invalidated
        and collector._at_fork_registered
        and collector._pid == os.getpid()
        and collector._pending_collection is None
        and collector._reported_collection_data is None
        and not collector._plugins.order
    )


def _fork_capture_descriptors(config: pytest.Config) -> list[int]:
    """Return the descriptors pytest's suspended global capture owns.

    Stock fd capture holds, per stream, a saved copy of the standard descriptor
    and a temporary file (``/dev/null`` for stdin). They are owned, not foreign:
    ``_renew_child_capture`` gives each child private files under the same
    numbers, so no child shares the controller's capture storage. Anything else
    that pytest's capture does not account for stays unowned.
    """
    method = config.getoption("capture")
    if method == "no":
        return []
    manager = config.pluginmanager.getplugin("capturemanager")
    if type(manager) is not CaptureManager or manager._method != method:
        raise pytest.UsageError("collected fork workers require pytest's capture manager")
    capture = manager._global_capturing
    if method == "sys":
        return []
    if type(capture) is not MultiCapture or capture._state != "suspended":
        raise pytest.UsageError("collected fork workers require suspended global fd capture")
    devnull = os.stat(os.devnull)
    descriptors: list[int] = []
    for stream, target in ((capture.in_, 0), (capture.out, 1), (capture.err, 2)):
        if (
            type(stream) is not FDCapture
            or stream.targetfd != target
            or stream._state != "suspended"
            or stream.targetfd_invalid is not None
            or type(stream.syscapture) is not SysCapture
        ):
            raise pytest.UsageError("collected fork workers require pytest's stock fd capture")
        try:
            saved = os.fstat(stream.targetfd_save)
            storage = os.fstat(stream.tmpfile.fileno())
        except (OSError, ValueError) as error:
            raise pytest.UsageError(
                "collected fork workers require open fd capture files"
            ) from error
        if not os.path.samestat(saved, os.fstat(target)) or not (
            stat.S_ISCHR(storage.st_mode) and os.path.samestat(storage, devnull)
            if target == 0
            else stat.S_ISREG(storage.st_mode)
        ):
            raise pytest.UsageError("collected fork workers require pytest's stock fd capture")
        descriptors.extend((stream.targetfd_save, stream.tmpfile.fileno()))
    return descriptors


def _renew_child_capture(session: pytest.Session) -> None:
    """Rebind the inherited global capture to this child's redirected streams.

    ``sys`` capture keeps its in-memory buffers, which fork copies. Stock fd
    capture owns descriptors that the controller still shares with every sibling:
    each saved standard descriptor must name the child's own redirected stream,
    or ``done()`` at unconfigure would write the child's later output to the
    controller's terminal, and each temporary file is an open file description
    whose offset and contents all children would share. Replace the
    descriptors under their existing numbers so modules that retained pytest's
    capture streams during collection keep working.
    """
    manager = session.config.pluginmanager.getplugin("capturemanager")
    capture = None if manager is None else manager._global_capturing
    if capture is None:
        return
    if type(capture) is MultiCapture and type(capture.out) is FDCapture:
        for stream in (capture.in_, capture.out, capture.err):
            assert type(stream) is FDCapture
            os.dup2(stream.targetfd, stream.targetfd_save)
            if stream.targetfd == 0:
                replacement = os.open(os.devnull, os.O_RDONLY)
            else:
                with tempfile.TemporaryFile(buffering=0) as private:
                    replacement = os.dup(private.fileno())
            try:
                os.dup2(replacement, stream.tmpfile.fileno())
            finally:
                os.close(replacement)
        capture.out.syscapture._old = sys.stdout
        capture.err.syscapture._old = sys.stderr
        return
    if capture.out is not None:
        capture.out._old = sys.stdout
    if capture.err is not None:
        capture.err._old = sys.stderr


def _fork_unittest_is_synchronous(item: pytest_unittest.TestCaseFunction) -> bool:
    # The optional unittest path inspects its class before touching a case instance.
    from unittest import IsolatedAsyncioTestCase  # noqa: PLC0415

    if type(item.parent) is not pytest_unittest.UnitTestCase:
        return False
    parent = item.parent
    return (
        not issubclass(parent.obj, IsolatedAsyncioTestCase)
        and not inspect.iscoroutinefunction(item.obj)
        and not inspect.isasyncgenfunction(item.obj)
    )


def _admit_collected_fork(session: pytest.Session) -> list[int]:
    """Validate the bounded snapshot contract before creating any children."""
    config = session.config
    if sys.platform != "linux" or sys.implementation.name != "cpython":
        raise pytest.UsageError("collected fork workers require Linux CPython")
    if threading.active_count() != 1 or sys.gettrace() is not None or sys.getprofile() is not None:
        raise pytest.UsageError("collected fork workers reject threads, tracing and profiling")
    if hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled():
        raise pytest.UsageError("collected fork workers require the CPython GIL")
    capture_fds = _fork_capture_descriptors(config)
    if not _fork_coverage_is_admissible(config):
        raise pytest.UsageError(
            "collected fork workers require disabled coverage or vfy's native coverage "
            "without plug-ins or an enclosing session"
        )
    if (
        getattr(config.option, "htmlpath", None)
        or hasattr(config, "workerinput")
        or config.pluginmanager.hasplugin("dsession")
        or config.pluginmanager.hasplugin("_vfy_ci_collection")
        or os.environ.get("VFY_MUTATION_TEST_ISOLATION")
        or os.environ.get("VFY_WATCH_STAGE")
    ):
        raise pytest.UsageError(
            "collected fork workers reject HTML, xdist, mutation and incremental publication"
        )
    if _serial_logging_reason(config) is not None:
        raise pytest.UsageError("collected fork workers reject live or file logging")
    fixture_manager = session._fixturemanager
    all_definitions = getattr(fixture_manager, "_get_all_fixture_defs", None)
    definitions = (
        all_definitions()
        if all_definitions is not None
        else (
            definition
            for group in fixture_manager._arg2fixturedefs.values()
            for definition in group
        )
    )
    if session._setupstate.stack or any(
        definition.cached_result is not None for definition in definitions
    ):
        raise pytest.UsageError("collected fork workers require uninitialized fixtures")
    if any(
        type(item) is not pytest_doctest.DoctestItem
        and not (
            type(item) is pytest_unittest.TestCaseFunction and _fork_unittest_is_synchronous(item)
        )
        and (
            type(item) is not pytest.Function
            or inspect.iscoroutinefunction(item.obj)
            or inspect.isasyncgenfunction(item.obj)
        )
        for item in session.items
    ):
        raise pytest.UsageError(
            "collected fork workers require plain synchronous Function, "
            "stock synchronous TestCaseFunction or stock DoctestItem items"
        )
    if os.environ.get("VFY_WORKER_COLLECTION") == "1":
        raise pytest.UsageError("collected fork workers require controller collection")
    if (
        config.option.xmlpath
        and type(config.stash.get(pytest_junitxml.xml_key, None)) is not pytest_junitxml.LogXML
    ):
        raise pytest.UsageError("collected fork workers require pytest's JUnit reporter")
    _admit_fork_hooks(config)
    _admit_fork_cleanup(config)
    allowed_fds = [0, 1, 2]
    allowed_fds.extend(capture_fds)
    allowed_fds.extend(_fork_logging_descriptors(config))
    handler_fd = config.stash.get(pytest_faulthandler.fault_handler_stderr_fd_key, None)
    if handler_fd is not None:
        allowed_fds.append(handler_fd)
    for name, original in _fork_os_functions:
        if getattr(os, name, None) is not original or not inspect.isbuiltin(original):
            raise pytest.UsageError(
                f"collected fork workers require the original os.{name} implementation"
            )
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise pytest.UsageError("collected fork workers require the default SIGCHLD handler")
    _initialize_fork_basetemp(config)
    sys.stdout.flush()
    sys.stderr.flush()
    return allowed_fds


def _fork_logging_descriptors(config: pytest.Config) -> list[int]:
    plugin = config.pluginmanager.getplugin("logging-plugin")
    if plugin is None:
        return []
    if type(plugin) is not pytest_logging.LoggingPlugin or plugin._config is not config:
        raise pytest.UsageError("collected fork workers require pytest's logging adapter")
    handler = plugin.log_file_handler
    stream = handler.stream
    if (
        type(handler) is not pytest_logging._FileHandler
        or handler.baseFilename != os.devnull
        or type(stream) is not TextIOWrapper
        or not os.path.samestat(os.fstat(stream.fileno()), os.stat(os.devnull))
    ):
        raise pytest.UsageError("collected fork workers require pytest's null log file")
    return [stream.fileno()]


def _initialize_fork_basetemp(config: pytest.Config) -> None:
    if config.option.basetemp is not None and config.pluginmanager.hasplugin("tmpdir"):
        factory = getattr(config, "_tmp_path_factory", None)
        if type(factory) is not pytest.TempPathFactory:
            raise pytest.UsageError(
                "collected fork workers require pytest's temporary path factory"
            )
        config.option.basetemp = str(factory.getbasetemp())


def _admit_fork_hooks(config: pytest.Config) -> None:
    manager = config.pluginmanager
    for hook_name in (
        "pytest_sessionfinish",
        "pytest_unconfigure",
        "pytest_runtestloop",
    ):
        for implementation in getattr(manager.hook, hook_name).get_hookimpls():
            if not _reviewed_fork_hook(implementation.function):
                raise pytest.UsageError(
                    f"collected fork workers reject unreviewed {hook_name} hook "
                    f"from {implementation.plugin_name}"
                )


def _reviewed_fork_hook(callback: object) -> bool:
    if type(callback) not in {FunctionType, MethodType}:
        return False
    function = callback.__func__ if isinstance(callback, MethodType) else callback
    if not isinstance(function, FunctionType):
        return False
    module_name = function.__module__
    if not module_name.startswith(("_pytest.", "vfy.")):
        return False
    module = sys.modules.get(module_name)
    if not isinstance(module, ModuleType) or not isinstance(module.__file__, str):
        return False
    if Path(function.__code__.co_filename).resolve() != Path(module.__file__).resolve():
        return False
    if isinstance(callback, MethodType):
        owner_type = type(callback.__self__)
        return (
            vars(module).get(owner_type.__name__) is owner_type
            and vars(owner_type).get(function.__name__) is function
        )
    return vars(module).get(function.__name__) is function


def _nested_codes(function: FunctionType) -> set[CodeType]:
    codes: set[CodeType] = set()

    def include(code: CodeType) -> None:
        codes.add(code)
        for value in code.co_consts:
            if isinstance(value, CodeType):
                include(value)

    include(function.__code__)
    return codes


def _trusted_fork_exception_cleanup(callback: partial[object], config: pytest.Config) -> bool:
    if (
        callback.args
        or set(callback.keywords) != {"config", "prev_hook"}
        or callback.keywords["config"] is not config
    ):
        return False
    if callback.func is pytest_threadexception.cleanup:
        return (
            callback.keywords["prev_hook"] is threading.__excepthook__
            and pytest_threadexception.thread_exceptions in config.stash
        )
    if callback.func is pytest_unraisableexception.cleanup:
        return (
            callback.keywords["prev_hook"] is sys.__unraisablehook__
            and pytest_unraisableexception.unraisable_exceptions in config.stash
        )
    return False


def _trusted_fork_warning_stack(owner: object, config: pytest.Config) -> bool:
    if type(owner) is not contextlib.ExitStack:
        return False
    callbacks = vars(owner).get("_exit_callbacks")
    if type(callbacks) is not deque or len(callbacks) != 1:
        return False
    entry = callbacks[0]
    if type(entry) is not tuple or len(entry) != 2:
        return False
    synchronous, callback = entry
    if (
        synchronous is not True
        or type(callback) is not MethodType
        or callback.__func__ is not contextlib._GeneratorContextManager.__exit__
    ):
        return False
    context = callback.__self__
    if type(context) is not contextlib._GeneratorContextManager:
        return False
    generator = context.gen
    if (
        type(generator) is not GeneratorType
        or generator.gi_frame is None
        or inspect.getgeneratorstate(generator) != inspect.GEN_SUSPENDED
    ):
        return False
    # Frame values are opaque Python objects; admission only compares their identities.
    state = cast("dict[str, object]", generator.gi_frame.f_locals)
    configured_factory = vars(Config).get("_catch_configured_warnings")
    configured_generator = (
        vars(configured_factory).get("__wrapped__")
        if type(configured_factory) is FunctionType
        else None
    )
    config_module = sys.modules.get("_pytest.config")
    if (
        type(configured_generator) is FunctionType
        and generator.gi_code is configured_generator.__code__
        and type(config_module) is ModuleType
        and _fork_function_origin(
            configured_generator, config_module, "Config._catch_configured_warnings"
        )
    ):
        # Pytest 9.2 moves this entered, nonrecording scope onto Config itself.
        return state.get("self") is config and state.get("record") is False
    warning_generator = getattr(pytest_warnings.catch_warnings_for_item, "__wrapped__", None)
    if (
        type(warning_generator) is not FunctionType
        or generator.gi_code is not warning_generator.__code__
    ):
        return False
    return (
        state.get("config") is config
        and state.get("ihook") is config.hook
        and state.get("when") == "config"
        and state.get("item") is None
        and state.get("record") is False
    )


def _trusted_fork_monkeypatch(owner: MonkeyPatch, config: pytest.Config) -> bool:
    if owner._setitem or owner._cwd is not None or owner._savesyspath is not None:
        return False
    if len(owner._setattr) == 1:
        target, name, old = owner._setattr[0]
        if target is not config or old is not _MONKEYPATCH_NOTSET:
            return False
        factory = getattr(config, "_tmp_path_factory", None)
        if type(factory) is not pytest.TempPathFactory:
            return False
        if name == "_tmp_path_factory":
            return True
        handler = getattr(config, "_tmpdirhandler", None)
        return (
            name == "_tmpdirhandler"
            and type(handler) is pytest_legacypath.TempdirFactory
            and handler._tmppath_factory is factory
        )
    expected = (
        (pytest_legacypath.Cache, "makedir", pytest_legacypath.Cache_makedir, None),
        (pytest.FixtureRequest, "fspath", pytest_legacypath.FixtureRequest_fspath, None),
        (
            pytest_legacypath.TerminalReporter,
            "startdir",
            pytest_legacypath.TerminalReporter_startdir,
            None,
        ),
        (Config, "invocation_dir", pytest_legacypath.Config_invocation_dir, None),
        (Config, "rootdir", pytest_legacypath.Config_rootdir, None),
        (Config, "inifile", pytest_legacypath.Config_inifile, None),
        (
            pytest.Session,
            "startdir",
            getattr(
                pytest_legacypath,
                "Session_startdir",
                getattr(pytest_legacypath, "Session_stardir", None),
            ),
            None,
        ),
        (Config, "_getini_unknown_type", pytest_legacypath.Config__getini_unknown_type, None),
        (
            pytest_legacypath.Node,
            "fspath",
            pytest_legacypath.Node_fspath,
            pytest_legacypath.Node_fspath_set,
        ),
    )
    if len(owner._setattr) != len(expected):
        return False
    for (target, name, old), (expected_target, expected_name, getter, setter) in zip(
        owner._setattr, expected, strict=True
    ):
        if target is not expected_target or name != expected_name:
            return False
        current = inspect.getattr_static(expected_target, expected_name)
        if name == "_getini_unknown_type":
            if (
                current is not getter
                or type(old) is not FunctionType
                or old.__code__.co_filename != Config.__init__.__code__.co_filename
                or old.__qualname__ != "Config._getini_unknown_type"
            ):
                return False
        elif old is not _MONKEYPATCH_NOTSET:
            return False
        elif name == "makedir":
            if current is not getter:
                return False
        elif (
            type(current) is not property
            or current.fget is not getter
            or current.fset is not setter
        ):
            return False
    return True


def _trusted_fork_debugging_cleanup(closure: dict[str, object], config: pytest.Config) -> bool:
    return (
        set(closure) == {"pdb"}
        and closure["pdb"] is sys.modules.get("pdb")
        and pytest_debugging.pytestPDB._config is config
        and pytest_debugging.pytestPDB._pluginmanager is config.pluginmanager
        and bool(pytest_debugging.pytestPDB._saved)
    )


def _fork_function_origin(function: object, module: ModuleType, qualname: str) -> bool:
    return (
        type(function) is FunctionType
        and function.__globals__ is vars(module)
        and function.__qualname__ == qualname
        and isinstance(module.__file__, str)
        and Path(function.__code__.co_filename).resolve() == Path(module.__file__).resolve()
    )


def _trusted_fork_mock_patcher(
    patcher: object, mock: ModuleType, plugin: ModuleType, method: str, original: object
) -> bool:
    patch_type = vars(mock).get("_patch")
    if type(patcher) is not patch_type:
        return False
    state = vars(patcher)
    if any(type(key) is not str for key in state):
        return False
    target_name = "AsyncMock" if "await" in method else "NonCallableMock"
    target = vars(mock).get(target_name)
    wrapper = vars(plugin).get(f"wrap_{method}")
    getter = state.get("getter")
    patch_object = vars(mock).get("_patch_object")
    exit_stack = state.get("_exit_stack")
    if (
        type(target) is not type
        or not _fork_function_origin(wrapper, plugin, f"wrap_{method}")
        or not _fork_function_origin(patch_object, mock, "_patch_object")
        or type(getter) is not FunctionType
        or getter.__code__ not in _nested_codes(cast("FunctionType", patch_object))
        or getter.__globals__ is not vars(mock)
        or getter.__code__.co_freevars != ("target",)
        or getter.__closure__ is None
        or getter.__closure__[0].cell_contents is not target
        or state.get("target") is not target
        or type(state.get("attribute")) is not str
        or state.get("attribute") != method
        or state.get("new") is not wrapper
        or inspect.getattr_static(target, method, None) is not wrapper
        or state.get("temp_original") is not original
        or state.get("is_local") is not (target_name == "NonCallableMock")
        or type(exit_stack) is not contextlib.ExitStack
        or any(type(key) is not str for key in vars(exit_stack))
        or set(vars(exit_stack)) != {"_exit_callbacks"}
        or not _fork_function_origin(
            vars(contextlib.ExitStack).get("__exit__"), contextlib, "ExitStack.__exit__"
        )
        or type(vars(exit_stack).get("_exit_callbacks")) is not deque
        or len(vars(exit_stack)["_exit_callbacks"]) != 0
    ):
        return False
    original_owner = "AsyncMockMixin" if target_name == "AsyncMock" else "NonCallableMock"
    if not _fork_function_origin(original, mock, f"{original_owner}.{method}"):
        return False
    # Reject instance overrides, extra patchers and nonstandard restoration options.
    expected_keys = {
        "getter",
        "attribute",
        "new",
        "new_callable",
        "spec",
        "create",
        "has_local",
        "spec_set",
        "autospec",
        "kwargs",
        "additional_patchers",
        "temp_original",
        "is_local",
        "target",
        "_exit_stack",
        "is_started",
    }
    return (
        not set(state).difference(expected_keys)
        and all(
            state.get(name) is None for name in ("new_callable", "spec", "spec_set", "autospec")
        )
        and state.get("create") is False
        and state.get("has_local") is False
        and state.get("is_started", True) is True
        and type(state.get("kwargs")) is dict
        and not state["kwargs"]
        and type(state.get("additional_patchers")) is list
        and not state["additional_patchers"]
    )


def _trusted_fork_mock_cleanup(callback: object, config: pytest.Config) -> bool:
    plugin = sys.modules.get("pytest_mock.plugin")
    package = sys.modules.get("pytest_mock")
    mock = sys.modules.get("unittest.mock")
    if not isinstance(plugin, ModuleType) or not isinstance(mock, ModuleType):
        return False
    configure = vars(plugin).get("pytest_configure")
    if (
        callback is not vars(plugin).get("unwrap_assert_methods")
        or not _fork_function_origin(callback, plugin, "unwrap_assert_methods")
        or not _fork_function_origin(configure, plugin, "pytest_configure")
        or not any(
            hook.function is configure and (hook.plugin is plugin or hook.plugin is package)
            for hook in config.hook.pytest_configure.get_hookimpls()
        )
    ):
        return False
    patch_type = vars(mock).get("_patch")
    if (
        type(patch_type) is not type
        or len(patch_type.__bases__) != 1
        or patch_type.__bases__[0] is not object
        or any(name in vars(patch_type) for name in ("__eq__", "__getattribute__", "__getattr__"))
        or vars(patch_type).get("attribute_name") is not None
        or not all(
            _fork_function_origin(vars(patch_type).get(name), mock, f"_patch.{name}")
            for name in ("stop", "__exit__")
        )
    ):
        return False
    patches = vars(plugin).get("_mock_module_patches")
    originals = vars(plugin).get("_mock_module_originals")
    active = vars(patch_type).get("_active_patches")
    methods = (
        "assert_called",
        "assert_called_once",
        "assert_called_with",
        "assert_called_once_with",
        "assert_any_call",
        "assert_has_calls",
        "assert_not_called",
        "assert_awaited",
        "assert_awaited_once",
        "assert_awaited_with",
        "assert_awaited_once_with",
        "assert_any_await",
        "assert_has_awaits",
        "assert_not_awaited",
    )
    if (
        type(patches) is not list
        or type(originals) is not dict
        or len(patches) != len(methods)
        or any(type(name) is not str for name in originals)
        or set(originals) != set(methods)
        or type(active) is not list
        or any(type(patch) is not patch_type for patch in active)
    ):
        return False
    # The global list may also own user patches; stopping these fourteen touches only
    # their exact stock class targets, with no equality or nested exit callbacks.
    return all(
        any(patch is member for member in active)
        and _trusted_fork_mock_patcher(patch, mock, plugin, method, originals[method])
        for patch, method in zip(patches, methods, strict=True)
    )


def _trusted_fork_cleanup(callback: object, config: pytest.Config) -> bool:
    from . import native_pytest  # noqa: PLC0415 - validation occurs after native adapter import

    if type(callback) is partial:
        return _trusted_fork_exception_cleanup(cast("partial[object]", callback), config)
    if type(callback) not in {FunctionType, MethodType, BuiltinFunctionType}:
        return False
    owner = getattr(callback, "__self__", None)
    function = getattr(callback, "__func__", callback)
    cleanup_owners: dict[object, object] = {
        getattr(Config, "_unconfigure_python_path", None): config,
        CaptureManager.stop_global_capturing: config.pluginmanager.getplugin("capturemanager"),
    }
    if function in cleanup_owners:
        return owner is cleanup_owners[function]
    if function is contextlib.ExitStack.close:
        return _trusted_fork_warning_stack(owner, config)
    if function is MonkeyPatch.undo and type(owner) is MonkeyPatch:
        return _trusted_fork_monkeypatch(owner, config)
    if (
        lazy_function._inventory is not None
        and owner is lazy_function._inventory
        and inspect.isbuiltin(callback)
    ):
        return callback == lazy_function._inventory.prune
    if not isinstance(function, FunctionType):
        return False
    closure = dict(
        zip(
            function.__code__.co_freevars,
            (cell.cell_contents for cell in function.__closure__ or ()),
            strict=True,
        )
    )
    if function.__code__ in _nested_codes(pytest_debugging.pytest_configure):
        return _trusted_fork_debugging_cleanup(closure, config)
    if function.__code__ in _nested_codes(pytest_assertion.install_importhook):
        state = config.stash.get(pytest_assertion.assertstate_key, None)
        return (
            closure.get("config") is config
            and state is not None
            and state.hook is not None
            and state.hook.config is config
            and any(hook is state.hook for hook in sys.meta_path)
        )
    if function.__code__ in _nested_codes(native_pytest.pytest_configure):
        return closure.get("config") is config and (
            "warning_recorder" not in closure
            or closure["warning_recorder"]
            is config.pluginmanager.getplugin("_vfy_native_session_warning_recorder")
        )
    if function.__code__ in _nested_codes(lazy_function.install):
        return closure.get("config") is config
    if function.__code__ in _nested_codes(lazy_function.install_sorter):
        implementation = closure.get("impl")
        return any(
            implementation is hook and hook.plugin is config.pluginmanager.getplugin("funcmanage")
            for hook in config.hook.pytest_collection_modifyitems.get_hookimpls()
        )
    if function.__code__ in _nested_codes(parameter_family.install):
        return closure.get("parametrize") is inspect.getattr_static(Metafunc, "parametrize")
    if function.__code__ in _nested_codes(prepare_worker):
        implementation = closure.get("implementation")
        return any(
            implementation is hook and hook.plugin_name == "logging"
            for hook in config.hook.pytest_configure.get_hookimpls()
        )
    return function is parameter_family._noop_restore or _trusted_fork_mock_cleanup(
        callback, config
    )


def _admit_fork_cleanup(config: pytest.Config) -> None:
    # Pytest stores cleanup callbacks directly or wrapped in an ExitStack.
    # Both representations require the same code and session-owner checks.
    if hasattr(config, "_cleanup_stack"):
        stack = config._cleanup_stack
        if type(stack) is not contextlib.ExitStack:
            raise pytest.UsageError("collected fork workers require pytest's config cleanup stack")
        wrapped_callbacks = cast("deque[tuple[bool, object]]", vars(stack)["_exit_callbacks"])
        callbacks = [getattr(wrapped, "__wrapped__", None) for _, wrapped in wrapped_callbacks]
    else:
        legacy_callbacks = getattr(config, "_cleanup", None)
        if type(legacy_callbacks) is not list:
            raise pytest.UsageError("collected fork workers require pytest's config cleanup list")
        callbacks = cast("list[object]", legacy_callbacks)
    for callback in callbacks:
        if not _trusted_fork_cleanup(callback, config):
            raise pytest.UsageError(
                "collected fork workers reject an unreviewed config cleanup callback"
            )


def _bind_collected_child(
    session: pytest.Session,
    directory: str,
    index: int,
    worker: NativeParallelWorker,
    retain_success_output: bool,
    start: ForkStart,
) -> None:
    """Rebind reviewed process-owned state while preserving collected aliases."""
    # The adapter imports parallel during initialization; enter it only after collection.
    from . import native_pytest  # noqa: PLC0415

    config = session.config
    root = Path(directory)
    os.environ["VFY_NATIVE_PARALLEL_RETAIN_CAPTURE_LOCATIONS"] = (
        "1" if os.environ.get("PYTEST_NATIVE_RESULT") else "0"
    )
    os.environ.update(
        VFY_NATIVE_PARALLEL_DIR=directory,
        VFY_NATIVE_PARALLEL_INDEX=str(index),
        VFY_NATIVE_WORKERS="1",
        VFY_NATIVE_PARALLEL_RETAIN_OUTPUT="1" if retain_success_output else "0",
        VFY_TEST_RESULTS=str(root / f"worker{index}.observer.json"),
    )
    coverage = config.pluginmanager.getplugin("_cov")
    # Active coverage keeps the controller's subprocess collection settings, which
    # stage shards in the directory that the controller combines.
    keep_subprocess_coverage = coverage is not None and not coverage._disabled
    for name in (
        "VFY_PROCESS_START",
        "VFY_PROCESS_OPTIONS",
        "VFY_TEST_HISTORY",
        "VFY_WATCH_STAGE",
        "PYTEST_NATIVE_RESULT",
    ):
        if not (keep_subprocess_coverage and name.startswith("VFY_PROCESS_")):
            os.environ.pop(name, None)
    if "PYTEST_NATIVE_STAGE_OUTPUT" in os.environ:
        os.environ["PYTEST_NATIVE_STAGE_OUTPUT"] = str(root / f"stage.{index}.json")
    handler_fd = config.stash.get(pytest_faulthandler.fault_handler_stderr_fd_key, None)
    if handler_fd is not None:
        os.dup2(2, handler_fd)
    _renew_child_capture(session)
    if config.option.xmlpath:
        config.option.xmlpath = str(root / f"junit.{index}.xml")
        config.stash[pytest_junitxml.xml_key].logfile = config.option.xmlpath
    if config.option.basetemp is not None:
        config.option.basetemp = str(Path(config.option.basetemp) / f"native-worker-{index}")
    elif start is ForkStart.EXPLICIT:
        config.option.basetemp = str(root / f"tmp.{index}")
    # An automatic fork keeps a fresh worker's default: pytest's own numbered base.
    if config.pluginmanager.hasplugin("tmpdir"):
        factory = getattr(config, "_tmp_path_factory", None)
        if type(factory) is not pytest.TempPathFactory:
            raise pytest.UsageError(
                "collected fork workers require pytest's temporary path factory"
            )
        fresh = pytest.TempPathFactory.from_config(config, _ispytest=True)
        # Preserve initialization aliases, including the stock legacy wrapper's factory.
        # The copied parent's cleanup stack remains parent-owned and must not run here.
        factory._given_basetemp = fresh._given_basetemp
        factory._basetemp = fresh._basetemp
        if hasattr(fresh, "_exit_stack"):
            factory._exit_stack = fresh._exit_stack
    if keep_subprocess_coverage:
        shard = worker.coverage_shard
        if shard is None:
            raise RuntimeError("a forked coverage worker has no registered shard")
        coverage._adopt_fork_worker(shard)
    observer = config.pluginmanager.getplugin("_vfy_run_observer")
    if observer is not None:
        observer.result_path = os.environ["VFY_TEST_RESULTS"]
    config.stash[native_pytest._session_warnings_key].clear()
    config.stash[native_pytest._parallel_worker_key] = worker
    config.stash[_collected_fork_child] = start
    silence_worker_terminal(config)


def _finish_runtime_threads(start: ForkStart) -> None:
    """Match the thread lifetime of the route that selected this fork.

    An explicit fork requires tests to join their threads. An automatic fork
    replaces a fresh worker, whose interpreter shutdown joins non-daemon threads
    and discards daemon threads, so the child does the same before ``os._exit``.
    """
    match start:
        case ForkStart.EXPLICIT:
            if threading.active_count() != 1:
                raise RuntimeError(
                    "collected fork workers require runtime threads to finish before cleanup"
                )
        case ForkStart.AUTOMATIC:
            # CPython runs threading's shutdown callbacks before joining. Pools
            # rely on those callbacks to wake their retained worker threads.
            # Typeshed omits this private interpreter-shutdown hook.
            threading._shutdown()  # ty: ignore[unresolved-attribute]


def _collected_child(
    session: pytest.Session,
    directory: str,
    index: int,
    worker: NativeParallelWorker,
    retain_success_output: bool,
    prepared_execution: NativeExecutionSession | None,
    start: ForkStart,
) -> int:
    # The native adapter imports parallel during module initialization.
    from . import native_pytest  # noqa: PLC0415

    _bind_collected_child(session, directory, index, worker, retain_success_output, start)
    config = session.config
    session.shouldfail = False
    session.shouldstop = False
    session.exitstatus = pytest.ExitCode.OK
    try:
        try:
            native_pytest._native_runtestloop(session, prepared_execution)
            session.exitstatus = (
                pytest.ExitCode.TESTS_FAILED if session.testsfailed else pytest.ExitCode.OK
            )
        except session.Failed:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        except (KeyboardInterrupt, pytest.exit.Exception) as error:
            session.exitstatus = (
                error.returncode
                if isinstance(error, pytest.exit.Exception) and error.returncode is not None
                else pytest.ExitCode.INTERRUPTED
            )
            config.hook.pytest_keyboard_interrupt(excinfo=ExceptionInfo.from_current())
        except BaseException:  # noqa: BLE001 - pytest's session boundary translates every body exception
            session.exitstatus = pytest.ExitCode.INTERNAL_ERROR
            config.notify_exception(ExceptionInfo.from_current(), config.option)
    finally:
        os.chdir(session.startpath)
        try:
            try:
                config.hook.pytest_sessionfinish(session=session, exitstatus=session.exitstatus)
            except pytest.exit.Exception as error:
                if error.returncode is not None:
                    session.exitstatus = error.returncode
                sys.stderr.write(f"{type(error).__name__}: {error}\n")
                worker.complete_exit(error.msg, int(session.exitstatus))
        finally:
            config._ensure_unconfigure()
            _finish_runtime_threads(start)
            sys.stdout.flush()
            sys.stderr.flush()
    # The fork launcher bypasses worker_main; certify the same lifecycle boundary
    # only after unconfigure and process-local shutdown have returned normally.
    return worker.complete_shutdown(session.exitstatus)
