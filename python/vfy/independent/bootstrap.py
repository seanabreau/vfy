"""Bind native invocation ownership before restoring application interpreter startup."""

from __future__ import annotations

import os
import runpy
import site
import subprocess
import sys
import warnings
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast, override

from vfy import native
from vfy._closed_stdout import CLOSED_STDOUT_STATUS, discard_stdout


class _StreamOptions(TypedDict):
    encoding: str
    errors: str | None


class StdioOptions(TypedDict):
    stdout: _StreamOptions | None
    stderr: _StreamOptions | None


class StartupOptions(TypedDict):
    interpreter_args: list[str]
    pythonpath: str | None
    pythonwarnings: str | None
    warnoptions: list[str]
    ignore_environment: bool
    safe_path: bool
    user_site: bool
    site_enabled: bool
    stdio_options: StdioOptions


class _MultiprocessingUtil(Protocol):
    _args_from_interpreter_flags: Callable[[], list[str]]


class _TracebackModule(Protocol):
    sys: object


def _stream_options(name: Literal["stdout", "stderr"]) -> _StreamOptions | None:
    stream = getattr(sys, f"__{name}__", None)
    encoding = getattr(stream, "encoding", None)
    if encoding is None:
        stream = getattr(sys, name)
        encoding = getattr(stream, "encoding", None)
    if encoding is None:
        return None
    if not isinstance(encoding, str):
        raise TypeError(f"standard {name} encoding must be a string or None")
    errors = getattr(stream, "errors", None)
    if errors is not None and not isinstance(errors, str):
        raise TypeError(f"standard {name} errors must be a string or None")
    # The private bootstrap embeds literals, never caller-defined repr methods.
    return {
        "encoding": str.__str__(encoding),
        "errors": str.__str__(errors) if errors is not None else None,
    }


def snapshot_stdio() -> StdioOptions:
    """Retain caller stream codecs before an owned interpreter restores startup."""
    return {"stdout": _stream_options("stdout"), "stderr": _stream_options("stderr")}


def snapshot_startup(environment: Mapping[str, str]) -> StartupOptions:
    """Capture the caller's effective CPython settings before an owned bootstrap."""
    # CPython's spawn path uses this adapter; typeshed omits the private helper.
    arguments: list[str] = subprocess._args_from_interpreter_flags()  # ty: ignore[unresolved-attribute,unsound-assignment]
    # CPython's spawn adapter omits the effective bytecode-cache prefix.
    if sys.pycache_prefix is not None:
        arguments.extend(("-X", f"pycache_prefix={sys.pycache_prefix}"))
    return {
        "interpreter_args": arguments,
        "pythonpath": environment.get("PYTHONPATH"),
        "pythonwarnings": environment.get("PYTHONWARNINGS"),
        "warnoptions": sys.warnoptions[:],
        "ignore_environment": bool(sys.flags.ignore_environment),
        "safe_path": bool(sys.flags.safe_path),
        "user_site": bool(site.ENABLE_USER_SITE),
        "site_enabled": not sys.flags.no_site,
        "stdio_options": snapshot_stdio(),
    }


def prepare_bootstrap(
    command: list[str],
    environment: Mapping[str, str],
    *,
    startup: StartupOptions | None = None,
    coverage_options: Mapping[str, object] | None = None,
    controller_parent: int | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Withhold application import paths until the existing native owner binds."""
    if startup is None:
        options = snapshot_startup(environment)
    else:
        options = startup.copy()
        options["pythonpath"] = environment.get("PYTHONPATH")
        options["pythonwarnings"] = environment.get("PYTHONWARNINGS")
        options["stdio_options"] = snapshot_stdio()
    child_environment = dict(environment)
    child_environment.pop("PYTHONPATH", None)
    child_environment.pop("PYTHONWARNINGS", None)
    package_root = str(Path(__file__).resolve().parents[2])
    bootstrap = (
        "import sys; standard_paths = sys.path.copy(); "
        f"sys.path.append({package_root!r}); "
        "from vfy.independent.bootstrap import main; "
        f"main({command!r}, standard_paths, startup={options!r}, "
        f"coverage_options={dict(coverage_options) if coverage_options is not None else None!r}, "
        f"controller_parent={controller_parent!r})"
    )
    # This adapter emits attached -W arguments. Defer their category imports
    # until binding and application path restoration, using warnings' parser.
    arguments = [
        argument for argument in options["interpreter_args"] if not argument.startswith("-W")
    ]
    return [*arguments, "-P", "-S", "-c", bootstrap], child_environment


def _restore_stdio(options: StdioOptions) -> None:
    for name in ("stdout", "stderr"):
        setting = options[name]
        if setting is not None:
            getattr(sys, name).reconfigure(encoding=setting["encoding"], errors=setting["errors"])


@contextmanager
def _multiprocessing_startup(startup: StartupOptions) -> Iterator[None]:
    """Keep owned bootstrap flags out of application multiprocessing children."""
    from multiprocessing import util  # noqa: PLC0415 - owned interpreter startup only

    # Typeshed omits this CPython adapter re-exported by multiprocessing.util.
    flags = cast("_MultiprocessingUtil", util)
    original = flags._args_from_interpreter_flags

    def arguments() -> list[str]:
        # Delegate live warning and interpreter options to CPython's adapter.
        return [
            argument
            for argument in original()
            if not (
                (argument == "-S" and startup["site_enabled"])
                or (argument == "-P" and not startup["safe_path"])
            )
        ]

    flags._args_from_interpreter_flags = arguments
    try:
        yield
    finally:
        if flags._args_from_interpreter_flags is arguments:
            flags._args_from_interpreter_flags = original


class _TracebackFlags:
    __slots__ = ()
    no_site = 0

    def __getattr__(self, name: str) -> object:
        return getattr(sys.flags, name)


class _TracebackSys:
    __slots__ = ()
    flags = _TracebackFlags()

    def __getattr__(self, name: str) -> object:
        return getattr(sys, name)

    @override
    def __setattr__(self, name: str, value: object) -> None:
        setattr(sys, name, value)

    @override
    def __delattr__(self, name: str) -> None:
        delattr(sys, name)


@contextmanager
def _traceback_startup(startup: StartupOptions) -> Iterator[None]:
    """Format exceptions using restored site policy without changing real flags."""
    if sys.version_info < (3, 15) or not startup["site_enabled"] or not sys.flags.no_site:
        yield
        return
    import traceback  # noqa: PLC0415 - owned interpreter startup only

    # CPython's formatter derives import hints from this private module global.
    # Keep custom formatter dependencies authoritative and the caller's -S flag intact.
    original = getattr(traceback, "sys", None)
    if original is not sys:
        yield
        return
    dependency = cast("_TracebackModule", traceback)
    delegated = _TracebackSys()
    dependency.sys = delegated
    try:
        yield
    finally:
        if getattr(traceback, "sys", None) is delegated:
            dependency.sys = original


def main(
    command: list[str],
    standard_paths: list[str],
    *,
    startup: StartupOptions,
    coverage_options: Mapping[str, object] | None = None,
    controller_parent: int | None = None,
) -> None:
    """Bind before activating application paths, site hooks, or customization.

    The interpreter retains its bootstrap safe-path and no-site flags. The
    captured application startup policy also governs any nested owned workers.
    """
    owner = native._pytest.NativeIndependentInvocation(parent_pid=controller_parent)
    if controller_parent is None:
        owner.bind_worker(
            os.environ["VFY_NATIVE_PARALLEL_DIR"], int(os.environ["VFY_NATIVE_PARALLEL_INDEX"])
        )
    os.environ.pop("VFY_NATIVE_PARALLEL_PARENT_PID", None)
    pythonpath = startup["pythonpath"]
    if pythonpath is None:
        os.environ.pop("PYTHONPATH", None)
    else:
        os.environ["PYTHONPATH"] = pythonpath
    pythonwarnings = startup["pythonwarnings"]
    if pythonwarnings is None:
        os.environ.pop("PYTHONWARNINGS", None)
    else:
        os.environ["PYTHONWARNINGS"] = pythonwarnings

    def restore_startup() -> None:
        sys.argv = ["-m", *command[2:]]
        paths: list[str] = []
        if pythonpath is not None and not startup["ignore_environment"]:
            paths.extend(os.path.abspath(path) for path in pythonpath.split(os.pathsep))
        sys.path[:] = [*paths, *standard_paths]
        sys.warnoptions[:] = startup["warnoptions"]
        # CPython's untyped startup adapter precedes site hooks and their filter edits.
        warnings._processoptions(sys.warnoptions)  # ty: ignore[unresolved-attribute]
        with ExitStack() as resources:
            if coverage_options is not None:
                from vfy import process_startup  # noqa: PLC0415 - measured worker startup only

                resources.enter_context(process_startup.worker_startup(coverage_options))
                if startup["site_enabled"]:
                    bootstrap = str(Path(__file__).resolve().parents[1] / "bootstrap")
                    sys.path.insert(0, bootstrap)

                    def remove_bootstrap() -> None:
                        if bootstrap in sys.path:
                            sys.path.remove(bootstrap)

                    resources.callback(remove_bootstrap)
                else:
                    process_startup.start_worker()
            if startup["site_enabled"]:
                # No-site startup postpones user-site discovery. Restore the
                # caller's selected policy without changing interpreter flags.
                site.ENABLE_USER_SITE = startup["user_site"]
                site.main()
        # CPython inserts the -m entry after warnings and site customization.
        if not startup["safe_path"]:
            sys.path.insert(0, os.getcwd())

    try:
        with ExitStack() as resources:
            resources.callback(owner.close)
            resources.enter_context(_multiprocessing_startup(startup))
            resources.enter_context(_traceback_startup(startup))
            _restore_stdio(startup["stdio_options"])
            if command[:2] == ["-m", "vfy.independent.mutation_child"]:
                runpy.run_module(
                    "vfy.independent.mutation_child",
                    run_name="__main__",
                    alter_sys=True,
                    init_globals={
                        "_vfy_worker_invocation": owner,
                        "_vfy_worker_startup": restore_startup,
                    },
                )
            elif command[:2] == ["-m", "vfy.independent.entry"]:
                restore_startup()
                runpy.run_module(
                    "vfy.independent.entry",
                    run_name="__main__",
                    alter_sys=True,
                    init_globals={
                        "_vfy_worker_invocation": owner,
                        "_vfy_startup_options": startup,
                    },
                )
            else:
                raise ValueError(f"unsupported independent worker command: {command[:2]!r}")
    except BrokenPipeError:
        # The controller turns this status into the stock command's quiet exit.
        discard_stdout()
        raise SystemExit(CLOSED_STDOUT_STATUS) from None
