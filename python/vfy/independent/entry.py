"""Bootstrap for the independent Rust test runner."""

from __future__ import annotations

import argparse
import importlib
import importlib.machinery
import os
import sys
import traceback
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

from vfy import native
from vfy._version import __version__

from .facade import install_pytest_facade
from .imports import install_import_hook
from .outcomes import install_outcome_modules
from .worker import run_parallel, run_worker

if TYPE_CHECKING:
    from types import ModuleType

    from vfy._pytest_native import NativeRunResult

    from .bootstrap import StartupOptions


_CHILD_FACADE = str(Path(__file__).with_name("_children"))


@contextmanager
def _child_facade_scope() -> Iterator[None]:
    """Let processes that tests start import the facade when pytest is absent.

    New interpreters read ``PYTHONPATH``; ``multiprocessing`` spawn workers
    replace their path with this process's ``sys.path``, so both name the
    directory. Restore only the path entry owned by this scope.
    """
    previous = os.environ.get("PYTHONPATH")
    expose = importlib.machinery.PathFinder.find_spec("pytest") is None
    owns_path = expose and _CHILD_FACADE not in sys.path
    try:
        if expose:
            os.environ["PYTHONPATH"] = os.pathsep.join(
                entry for entry in (_CHILD_FACADE, previous) if entry
            )
        if owns_path:
            sys.path.append(_CHILD_FACADE)
        yield
    finally:
        if previous is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous
        if owns_path and _CHILD_FACADE in sys.path:
            sys.path.remove(_CHILD_FACADE)


def _version_requested(arguments: Sequence[str]) -> bool:
    """Match pytest's early check: exactly one ``--version`` or ``-V`` argument."""
    return arguments.count("--version") + arguments.count("-V") == 1


def _run_parallel_session(
    owner: native._pytest.NativeIndependentInvocation,
    raw_arguments: list[str],
    root: str,
    startup: StartupOptions | None,
) -> NativeRunResult:
    """Run the configured session's worker topology from the parent process."""
    session = owner.pytest_session
    if session is None:
        raise RuntimeError("independent invocation has no configured session")
    from .coverage_support import IndependentCoverage  # noqa: PLC0415 - worker startup

    options = session.config.option
    maxfail = options.maxfail
    coverage = session.config.pluginmanager.getplugin("_cov")
    return run_parallel(
        raw_arguments,
        root,
        options.workers,
        config=session.config,
        maxfail=0 if maxfail is None else maxfail,
        collect_only=options.collect_only,
        continue_on_collection_errors=options.continue_on_collection_errors,
        coverage_options=(
            coverage.worker_options() if isinstance(coverage, IndependentCoverage) else None
        ),
        reporter=session.config.pluginmanager.getplugin("terminalreporter"),
        startup=startup,
    )


def main(
    arguments: Sequence[str] | None = None,
    *,
    invocation: native._pytest.NativeIndependentInvocation | None = None,
    topology_output: str | None = None,
    startup: StartupOptions | None = None,
) -> int:
    """Run tests without loading the installed pytest engine."""
    root = str(Path.cwd())
    raw_arguments = list(sys.argv[1:] if arguments is None else arguments)
    values: dict[str, object] = {"_arguments": raw_arguments}
    owner = invocation or native._pytest.NativeIndependentInvocation()
    with ExitStack() as bootstrap:
        try:
            # CPython also stores None to block an import.
            modules = cast("dict[str, ModuleType | None]", sys.modules)
            had_pytest = "pytest" in modules
            previous = modules.get("pytest")
            bootstrap.enter_context(_child_facade_scope())
            if had_pytest:
                bootstrap.callback(modules.__setitem__, "pytest", previous)
            else:
                bootstrap.callback(modules.pop, "pytest", None)
            hook = install_import_hook()
            bootstrap.callback(hook.close)
            requested_topology = os.environ.pop("VFY_INDEPENDENT_TOPOLOGY_OUTPUT", None)
            if topology_output is None:
                topology_output = requested_topology
            values["_cli_skip_observations"] = not topology_output
            # Retain the facade through bootstrap cleanup.
            _facade = install_pytest_facade()
            if _version_requested(raw_arguments):
                # Answer before configuring a session or loading plug-ins.
                sys.stdout.write(f"vfy {__version__}\n")
                return 0
            # Installed plug-ins import pytest's private modules when they load.
            bootstrap.callback(install_outcome_modules())
            if "VFY_NATIVE_PARALLEL_DIR" in os.environ:
                if invocation is None:
                    owner.bind_worker(
                        os.environ["VFY_NATIVE_PARALLEL_DIR"],
                        int(os.environ["VFY_NATIVE_PARALLEL_INDEX"]),
                    )
                return run_worker([], root, None, None, values, invocation=owner)

            try:
                result = owner.execute(
                    [],
                    root,
                    lambda: _run_parallel_session(owner, raw_arguments, root, startup),
                    options=values,
                )
            # The session handler already recorded an exit status; report the error and return it.
            except BaseException as error:
                if owner.disposition != "return" or owner.exitstatus is None:
                    raise
                if isinstance(
                    error,
                    (native._pytest.IndependentUsageError, native._pytest.UnsupportedFeatureError),
                ):
                    sys.stderr.write(f"vfy independent usage error: {error}\n")
                else:
                    _report_startup_error(error)
                return owner.exitstatus
            if result is None:
                if owner.exitstatus is None:
                    raise RuntimeError("independent command has no exit status")
                return owner.exitstatus
            session = owner.pytest_session
            if session is None:
                raise RuntimeError("completed independent invocation has no session")
            from .results import (  # noqa: PLC0415 - finalized results only
                publish_result,
            )

            publish_result(result)
            _report(result, session.config.option, topology_output=topology_output)
            if owner.exitstatus is None:
                raise RuntimeError("independent command has no exit status")
            return owner.exitstatus
        finally:
            owner.close()


def _report_startup_error(error: BaseException) -> None:
    """Describe a conftest import error that ended startup, as pytest does.

    The session reports every other startup error through its own hooks.
    """
    from .errors import ConftestImportFailure  # noqa: PLC0415 - error path only

    if isinstance(error, ConftestImportFailure):
        cause = error.cause
        frames = [
            frame
            for frame in traceback.extract_tb(cause.__traceback__)
            if not frame.filename.startswith(
                ("<frozen", str(Path(__file__).parents[1]), str(Path(importlib.__file__).parent))
            )
        ]
        lines = [f"ImportError while loading conftest '{error.path}'."]
        for frame in frames:
            location = os.path.relpath(frame.filename) if frame.filename else frame.filename
            lines.append(f"{location}:{frame.lineno}: in {frame.name}")
            if frame.line:
                lines.append(f"    {frame.line}")
        lines.extend(
            f"E   {line}" for line in "".join(traceback.format_exception_only(cause)).splitlines()
        )
        sys.stderr.write("".join(f"{line}\n" for line in lines))


def _report(
    result: NativeRunResult, options: argparse.Namespace, *, topology_output: str | None = None
) -> None:
    if topology_output:
        result.write_json(topology_output)
    if options.native_result_json:
        result.write_json(options.native_result_json)


if __name__ == "__main__":
    invocation = globals().pop("_vfy_worker_invocation", None)
    if invocation is not None and not isinstance(
        invocation, native._pytest.NativeIndependentInvocation
    ):
        raise TypeError("worker bootstrap requires its native invocation")
    raise SystemExit(
        main(
            invocation=invocation,
            topology_output=globals().pop("_vfy_topology_output", None),
            startup=globals().pop("_vfy_startup_options", None),
        )
    )
