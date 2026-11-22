"""Automatic coverage startup for Python subprocesses."""

from __future__ import annotations

import atexit
import json
import os
import re
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from .api import Coverage

_coverage: Coverage | None = None
_finished = False
_explicit_coverage_run = False


@dataclass(slots=True)
class _WorkerStartup:
    pid: int
    thread: int
    options: Mapping[str, object] | None
    error: BaseException | None = None


_worker_startup: _WorkerStartup | None = None


@contextmanager
def worker_startup(options: Mapping[str, object]) -> Iterator[None]:
    """Own one in-process collector recipe until interpreter customization starts."""
    global _worker_startup
    previous = _worker_startup
    descriptor = _WorkerStartup(os.getpid(), threading.get_ident(), options)
    _worker_startup = descriptor
    try:
        yield
        # site.execsitecustomize reports and suppresses ordinary exceptions. An
        # owned worker still requires its collector to have started successfully.
        error = descriptor.error
        descriptor.error = None
        if error is not None:
            raise error
    finally:
        _worker_startup = previous
        descriptor.error = None


def start_worker() -> Coverage | None:
    """Consume only this process/thread's pending recipe before application hooks."""
    descriptor = _worker_startup
    if (
        descriptor is None
        or descriptor.pid != os.getpid()
        or descriptor.thread != threading.get_ident()
        or descriptor.options is None
    ):
        return None
    options = descriptor.options
    descriptor.options = None
    try:
        return start(options=options)
    except BaseException as error:
        descriptor.error = error
        raise


def _coverage_run_command() -> bool:
    """Whether this interpreter was invoked through Coverage.py's run command."""
    if sys.argv and sys.argv[0] == "-m":
        for index, arg in enumerate(sys.orig_argv[:-2]):
            if arg == "-m":
                return (
                    sys.orig_argv[index + 1] in {"coverage", "coverage.__main__"}
                    and sys.orig_argv[index + 2] == "run"
                )
        return False
    return (
        len(sys.argv) > 1
        and Path(sys.argv[0]).name in {"coverage", "coverage.exe"}
        and sys.argv[1] == "run"
    )


# multiprocessing starts these stdlib helper interpreters with ``python -c``.
# They can outlive the interpreter that started them, so a shard they wrote
# could arrive after that session combined its data.
_MULTIPROCESSING_HELPER = re.compile(
    r"(?:import sys; )?from multiprocessing\.(?:forkserver|resource_tracker) import main\b"
)


def _multiprocessing_helper() -> bool:
    """Whether this interpreter is a multiprocessing forkserver or resource tracker."""
    arguments = sys.orig_argv
    for index, argument in enumerate(arguments[:-1]):
        if argument == "-c":
            return _MULTIPROCESSING_HELPER.match(arguments[index + 1]) is not None
    return False


class ChildOptions(TypedDict):
    """The existing subprocess bootstrap settings exported by a collector."""

    data_file: str
    config_file: bool | str
    branch: bool
    decisions: bool
    source: list[str]
    source_pkgs: list[str]
    include: list[str]
    omit: list[str]
    context: str
    concurrency: list[str]
    sigterm: bool


def child_options(coverage: Coverage, data_file: str | None = None) -> ChildOptions:
    """Return the settings a child interpreter needs to measure like its parent."""
    return {
        "data_file": data_file or coverage.data.base_filename(),
        "config_file": coverage._subprocess_config_file,
        "branch": coverage.branch,
        "decisions": coverage.decisions,
        "source": coverage.source,
        "source_pkgs": coverage.source_pkgs,
        "include": coverage.include,
        "omit": coverage.omit,
        "context": coverage.context,
        "concurrency": coverage.concurrency,
        "sigterm": coverage._sigterm,
    }


def environment_options() -> dict[str, Any] | None:
    """Return the child settings an enclosing vfy session exported, if any."""
    if os.getenv("VFY_PROCESS_START") != "1":
        return None
    return json.loads(os.getenv("VFY_PROCESS_OPTIONS", "{}"))


def child_coverage(options: Mapping[str, Any]) -> Coverage:
    """Construct an unstarted collector that writes a process-specific shard."""
    from .api import Coverage  # noqa: PLC0415 - only measured children need coverage

    coverage = Coverage(
        data_file=options.get("data_file", ".coverage"),
        data_suffix=True,
        config_file=options.get("config_file", True),
        branch=bool(options.get("branch")),
        decisions=bool(options.get("decisions")),
        source=options.get("source"),
        source_pkgs=options.get("source_pkgs"),
        include=options.get("include"),
        omit=options.get("omit"),
        context=options.get("context"),
        concurrency=options.get("concurrency"),
    )
    coverage._sigterm = bool(options.get("sigterm", coverage._sigterm))
    return coverage


@contextmanager
def subprocess_environment(
    coverage: Coverage,
    extras: Mapping[str, str] | None = None,
    *,
    data_file: str | None = None,
) -> Iterator[None]:
    """Enable coverage in child interpreters and restore the environment on exit."""
    values = dict(extras or {})
    managed = {"PYTHONPATH", "VFY_PROCESS_OPTIONS", "VFY_PROCESS_START", *values}
    previous = {name: os.environ.get(name) for name in managed}
    bootstrap = str(Path(__file__).resolve().parent / "bootstrap")
    python_path = os.environ.get("PYTHONPATH")
    try:
        os.environ["PYTHONPATH"] = (
            f"{bootstrap}{os.pathsep}{python_path}" if python_path else bootstrap
        )
        os.environ["VFY_PROCESS_START"] = "1"
        os.environ["VFY_PROCESS_OPTIONS"] = json.dumps(
            child_options(coverage, data_file), separators=(",", ":")
        )
        os.environ.update(values)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def active_coverage() -> Coverage | None:
    return _coverage


def finish(*, save: bool = True) -> None:
    global _coverage, _finished
    if _coverage is None or _finished:
        return
    _finished = True
    _coverage.stop()
    # Coverage.py disables its automatic subprocess collector when
    # `coverage run` takes ownership of tracing.
    # Do the same for this automatically started collector.  The environment
    # flag alone is insufficient: ordinary descendants inherit COVERAGE_RUN.
    if save and not (_explicit_coverage_run and os.getenv("COVERAGE_RUN") == "true"):
        _coverage.save()
    _coverage = None


def start(*, options: Mapping[str, object] | None = None) -> Coverage | None:
    global _coverage, _explicit_coverage_run
    if _coverage is not None:
        return _coverage
    if options is None and os.getenv("VFY_PROCESS_START") != "1":
        return None
    if _multiprocessing_helper():
        return None
    if options is None:
        options = environment_options()
        if options is None:
            return None
    _explicit_coverage_run = _coverage_run_command()
    coverage = child_coverage(options)
    # Coverage.py's automatic process startup suppresses warnings that can only
    # be judged after the parent has combined all subprocess data.
    coverage._warn_no_data = False
    coverage._warn_unimported_source = False
    _coverage = coverage
    try:
        coverage.start()
    except BaseException:
        # Keep the original start failure if releasing partial resources also fails.
        with suppress(BaseException):
            coverage.stop()
        if _coverage is coverage:
            _coverage = None
        raise
    atexit.register(finish)
    return coverage
