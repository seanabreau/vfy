"""CPython bootstrap lifetime for a retained native independent collection."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self

from vfy import native

from .arguments import argument_parser
from .facade import install_pytest_facade
from .imports import install_import_hook

if TYPE_CHECKING:
    from types import TracebackType

    from vfy._pytest_native import NativeIndependentSession, NativeRunResult


class _NativeLifecycle(Protocol):
    def close(self, exitstatus: int = 0) -> None: ...


class _NativeSessionFinalizer:
    """Close a native handle before cyclic GC clears its Python callback graph."""

    def __init__(self, session: _NativeLifecycle) -> None:
        self.session: _NativeLifecycle | None = session

    def __del__(self) -> None:
        session, self.session = self.session, None
        if session is not None:
            session.close()


class IndependentSession:
    """Own the process facade until native close; providers cannot overlap."""

    def __init__(
        self,
        arguments: Sequence[str] = (),
        *,
        root: str | None = None,
        mutation_arguments: Sequence[str] | None = None,
        mutation_timeout: float = 30.0,
        mutation_guard: bool = False,
    ) -> None:
        invocation_dir = str(Path.cwd()) if root is None else root
        configuration = native.NativePytestConfiguration(
            list(arguments), invocation_dir, os.environ.get("PYTEST_ADDOPTS")
        )
        effective_args = configuration.projection()["effective_args"]
        parser = argument_parser()
        options, _unknown = parser.parse_known_intermixed_args(effective_args)
        if options.workers != 1:
            raise ValueError("retained independent sessions require one executing process")
        values = vars(options)
        values["_configuration"] = configuration
        values["_parser"] = parser
        values["_arguments"] = effective_args
        previous = sys.modules.get("pytest")
        if getattr(previous, "_vfy_retained_session_owner", False):
            raise RuntimeError("another independent session provider is active")
        self.native: NativeIndependentSession | None = None
        self._closed = False
        self._previous = previous
        self._hook = install_import_hook()
        self._facade = None
        try:
            self._facade = install_pytest_facade()
            self._facade.__dict__["_vfy_retained_session_owner"] = True
            self.native = native._pytest.NativeIndependentSession(
                options.paths,
                invocation_dir,
                options.keyword,
                options.basetemp,
                values,
                mutation_arguments=None if mutation_arguments is None else list(mutation_arguments),
                mutation_timeout=mutation_timeout,
                mutation_guard=mutation_guard,
            )
        except BaseException:
            self.close()
            raise

    def run(self, selected_nodeids: Sequence[str] | None = None) -> NativeRunResult:
        """Execute the retained suite once and restore the bootstrap environment."""
        session = self.native
        if self._closed or session is None:
            raise RuntimeError("independent session is closed")
        try:
            return session.run(None if selected_nodeids is None else list(selected_nodeids))
        finally:
            if session.state == "finished":
                self.close()

    def close(self) -> None:
        """Close native hooks before removing the facade and helper import hook."""
        if self._closed:
            return
        session = self.native
        if session is not None and session.state == "running":
            # A rejected native operation does not consume the bootstrap.
            session.close()
            return
        self._closed = True
        try:
            if session is not None:
                session.close()
        finally:
            self.native = None
            try:
                self._hook.close()
            finally:
                if sys.modules.get("pytest") is self._facade:
                    if self._previous is None:
                        sys.modules.pop("pytest", None)
                    else:
                        sys.modules["pytest"] = self._previous

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        if hasattr(self, "_facade"):
            self.close()
