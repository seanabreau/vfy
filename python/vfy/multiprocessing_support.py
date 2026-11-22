"""Measure processes started by :mod:`multiprocessing` for ``concurrency = multiprocessing``.

A forked process inherits its parent's running session through the at-fork
hook, and a spawned or forkserver process receives the parent's settings with
its pickled preparation data. In both cases the child writes a process-specific
shard when ``Process.run`` returns, before multiprocessing calls ``os._exit``.
"""

from __future__ import annotations

import os
import weakref
from multiprocessing.process import BaseProcess
from typing import TYPE_CHECKING, Any, ClassVar, cast, override

from . import process_startup

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .api import Coverage

# multiprocessing calls this private method in the child to run the target.
_original_bootstrap = cast(
    "Callable[[BaseProcess, int | None], int]",
    BaseProcess._bootstrap,  # ty: ignore[unresolved-attribute] - the typeshed stub omits this private child entry point
)


class _State:
    parents: ClassVar[list[weakref.ref[Coverage]]] = []
    child_settings: ClassVar[Mapping[str, object] | None] = None
    patched: ClassVar[bool] = False


class _ChildSettings:
    """Carry measurement settings through spawn preparation data.

    Unpickling installs the bootstrap hook in a new interpreter before
    multiprocessing starts the target.
    """

    def __init__(self, settings: Mapping[str, object]) -> None:
        self.settings = settings

    @override
    def __getstate__(self) -> Mapping[str, object]:
        return self.settings

    def __setstate__(self, state: Mapping[str, object]) -> None:
        self.settings = state
        _install(state)


def measure_children(coverage: Coverage, *, data_file: str | None = None) -> None:
    """Measure multiprocessing children, optionally staging their shards at ``data_file``."""
    if not any(reference() is coverage for reference in _State.parents):
        _State.parents.append(weakref.ref(coverage))
    _install(process_startup.child_options(coverage, data_file))


def _install(settings: Mapping[str, object]) -> None:
    _State.child_settings = settings
    if _State.patched:
        return
    BaseProcess._bootstrap = _bootstrap  # ty: ignore[unresolved-attribute] - see _original_bootstrap
    try:
        from multiprocessing import spawn  # noqa: PLC0415 - spawn is unavailable on some platforms
    except ImportError:
        pass
    else:
        original_preparation = spawn.get_preparation_data

        def get_preparation_data(name: str) -> dict[str, Any]:
            data = original_preparation(name)
            if _State.child_settings is not None:
                data["vfy_coverage"] = _ChildSettings(_State.child_settings)
            return data

        spawn.get_preparation_data = get_preparation_data  # ty: ignore[invalid-assignment] - module functions are declarations in the stub
    _State.patched = True


def _bootstrap(process: BaseProcess, parent_sentinel: int | None = None) -> int:
    sessions = _child_sessions()
    try:
        return _original_bootstrap(process, parent_sentinel)
    finally:
        for session in sessions:
            session.stop()
            session.save()
        process_startup.finish()


def _child_sessions() -> list[Coverage]:
    """Return the sessions this child must save, starting one when none is active."""
    pid = os.getpid()
    # process_startup.finish saves an automatically started session.
    automatic = process_startup.active_coverage()
    inherited = [
        coverage
        for reference in _State.parents
        if (coverage := reference()) is not None
        and coverage is not automatic
        and coverage._collector is not None
        and coverage._pid == pid
    ]
    if inherited:
        exported = process_startup.environment_options() or _State.child_settings
        target = exported.get("data_file") if exported else None
        if isinstance(target, str):
            for coverage in inherited:
                coverage._use_child_data_file(target)
        return inherited
    if automatic is not None or _State.child_settings is None:
        return []
    coverage = process_startup.child_coverage(_State.child_settings)
    coverage.start()
    return [coverage]
