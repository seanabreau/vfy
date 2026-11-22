"""Private pytest module aliases for the independent facade.

Installed plug-ins import pytest's private modules when they load. This module
installs a fixed table of those names, each resolving to a ``vfy`` object, and
returns a callable that restores the previous ``sys.modules`` entries. It does
not import pytest's engine and does not activate the Hypothesis pytest plugin.

PYTEST_DONT_REWRITE
"""

from __future__ import annotations

import re
import sys
from types import ModuleType
from typing import TYPE_CHECKING

from vfy import native

from . import (
    facade,
    faulthandler_support,
    subtests_provider,
    threadexception,
    unraisable,
)
from .cache import Cache
from .compat import NOTSET, NotSetType
from .fixture_lookup import FixtureLookupError, FixtureLookupErrorRepr
from .fixtures import (
    FixtureFunctionDefinition,
    FixtureFunctionMarker,
    FuncFixtureInfo,
)
from .imports import AssertionImportHook
from .parameters import CallSpec2, Scope
from .saferepr import saferepr
from .subtests_provider import (
    Captured,
    CapturedLogs,
    failed_subtests_key,
    pytest_report_from_serializable,
    pytest_report_to_serializable,
)
from .terminal_writer import TerminalWriter, get_terminal_width

if TYPE_CHECKING:
    from collections.abc import Callable

    from .protocol import Config

_ANSI_ESCAPE_SEQ = re.compile(r"\x1b\[[\d;]+m")


def _remove_ansi_escape_sequences(text: str) -> str:
    return _ANSI_ESCAPE_SEQ.sub("", text)


_PACKAGES = frozenset({"_pytest", "_pytest._code", "_pytest.assertion", "_pytest.config"})


def _exports() -> dict[str, dict[str, object]]:
    """Return each private module's names, parents before their submodules."""
    from .capture import LogCaptureFixture  # noqa: PLC0415 - avoids a facade cycle
    from .collectors import Directory, File  # noqa: PLC0415 - avoids a facade cycle
    from .diagnostics import (  # noqa: PLC0415 - avoids a facade cycle
        ExceptionChainRepr,
        ExceptionRepr,
        ReprEntry,
        ReprEntryNative,
        ReprExceptionInfo,
        ReprFileLocation,
        ReprFuncArgs,
        ReprLocals,
        ReprTraceback,
        ReprTracebackNative,
        TerminalRepr,
    )
    from .protocol import (  # noqa: PLC0415 - avoids a facade cycle
        Collector,
        Config,
        Item,
        Node,
        Parser,
    )
    from .terminal import TerminalReporter  # noqa: PLC0415 - avoids a facade cycle

    return {
        "_pytest": {"__version__": "9.1.1"},
        "_pytest._code": {
            "ExceptionInfo": facade.ExceptionInfo,
            "Traceback": facade.Traceback,
            "TracebackEntry": facade.TracebackEntry,
        },
        "_pytest._code.code": {
            "ExceptionChainRepr": ExceptionChainRepr,
            "ExceptionInfo": facade.ExceptionInfo,
            "ExceptionRepr": ExceptionRepr,
            "ReprEntry": ReprEntry,
            "ReprEntryNative": ReprEntryNative,
            "ReprExceptionInfo": ReprExceptionInfo,
            "ReprFileLocation": ReprFileLocation,
            "ReprFuncArgs": ReprFuncArgs,
            "ReprLocals": ReprLocals,
            "ReprTraceback": ReprTraceback,
            "ReprTracebackNative": ReprTracebackNative,
            "TerminalRepr": TerminalRepr,
            "Traceback": facade.Traceback,
            "TracebackEntry": facade.TracebackEntry,
        },
        "_pytest.config": {"Config": Config, "ExitCode": facade.ExitCode},
        "_pytest.config.argparsing": {"Parser": Parser},
        "_pytest.outcomes": {
            "Exit": facade.Exit,
            "Failed": facade.Failed,
            "OutcomeException": facade.OutcomeException,
            "Skipped": facade.Skipped,
            "XFailed": facade.XFailed,
            "exit": facade.exit,
        },
        "_pytest.fixtures": {
            "FixtureFunctionDefinition": FixtureFunctionDefinition,
            "FixtureLookupError": FixtureLookupError,
            "FixtureLookupErrorRepr": FixtureLookupErrorRepr,
            "FixtureFunctionMarker": FixtureFunctionMarker,
            "FixtureRequest": native._pytest.NativeIndependentRequest,
            "FuncFixtureInfo": FuncFixtureInfo,
            "SubRequest": native._pytest.NativeIndependentRequest,
            "NOTSET": NOTSET,
            "NotSetType": NotSetType,
        },
        "_pytest.compat": {"NOTSET": NOTSET, "NotSetType": NotSetType},
        "_pytest.monkeypatch": {
            "MonkeyPatch": facade.MonkeyPatch,
            "NOTSET": NOTSET,
            "NotSetType": NotSetType,
        },
        "_pytest.nodes": {
            "Collector": Collector,
            "Directory": Directory,
            "File": File,
            "Item": Item,
            "Node": Node,
        },
        "_pytest.python": {"CallSpec2": CallSpec2},
        "_pytest.doctest": {},
        "_pytest.terminal": {"TerminalReporter": TerminalReporter},
        "_pytest.scope": {"Scope": Scope},
        "_pytest.cacheprovider": {"Cache": Cache},
        "_pytest.logging": {
            "LogCaptureFixture": LogCaptureFixture,
            "_remove_ansi_escape_sequences": _remove_ansi_escape_sequences,
        },
        "_pytest.subtests": {
            "Subtests": native._pytest.Subtests,
            "SubtestReport": native._pytest.SubtestReport,
            "SubtestContext": native._pytest.SubtestContext,
            "Captured": Captured,
            "CapturedLogs": CapturedLogs,
            "pytest_report_to_serializable": pytest_report_to_serializable,
            "pytest_report_from_serializable": pytest_report_from_serializable,
            "_SubTestContextManager": native._pytest._SubTestContextManager,
            "failed_subtests_key": failed_subtests_key,
            "subtests": subtests_provider.subtests,
            "saferepr": saferepr,
            "pytest_configure": native._pytest.configure_subtests,
            "pytest_report_teststatus": native._pytest.report_subtest_status,
        },
        "_pytest.unraisableexception": {
            "UnraisableMeta": unraisable.UnraisableMeta,
            "gc_collect_iterations_key": unraisable.gc_collect_iterations_key,
            "unraisable_exceptions": unraisable.unraisable_exceptions,
            "gc_collect_harder": unraisable.gc_collect_harder,
            "collect_unraisable": unraisable.collect_unraisable,
            "cleanup": unraisable.cleanup,
            "unraisable_hook": unraisable.unraisable_hook,
            "tracemalloc_message": unraisable.tracemalloc_message,
        },
        "_pytest.threadexception": {
            "ThreadExceptionMeta": threadexception.ThreadExceptionMeta,
            "thread_exceptions": threadexception.thread_exceptions,
            "collect_thread_exception": threadexception.collect_thread_exception,
            "cleanup": threadexception.cleanup,
            "thread_exception_hook": threadexception.thread_exception_hook,
        },
        "_pytest.faulthandler": {
            "fault_handler_original_stderr_fd_key": (
                faulthandler_support.fault_handler_original_stderr_fd_key
            ),
            "fault_handler_stderr_fd_key": faulthandler_support.fault_handler_stderr_fd_key,
            "get_stderr_fileno": faulthandler_support.get_stderr_fileno,
            "pytest_configure": faulthandler_support.pytest_configure,
            "pytest_unconfigure": faulthandler_support.pytest_unconfigure,
            "pytest_runtest_protocol": faulthandler_support.pytest_runtest_protocol,
            "pytest_enter_pdb": faulthandler_support.pytest_enter_pdb,
            "pytest_exception_interact": faulthandler_support.pytest_exception_interact,
        },
        "_pytest.warning_types": {
            "PytestWarning": facade.PytestWarning,
            "PytestUnraisableExceptionWarning": facade.PytestUnraisableExceptionWarning,
            "PytestUnhandledThreadExceptionWarning": (facade.PytestUnhandledThreadExceptionWarning),
        },
        "_pytest._io": {"TerminalWriter": TerminalWriter, "get_terminal_width": get_terminal_width},
        "_pytest.assertion": {},
        "_pytest.assertion.rewrite": {"AssertionRewritingHook": AssertionImportHook},
    }


def _install_lazy_doctest(module: ModuleType) -> None:
    def load(name: str) -> object:
        if name not in {"DoctestItem", "DoctestModule"}:
            raise AttributeError(f"module {module.__name__!r} has no attribute {name!r}")
        from . import (  # noqa: PLC0415 - lazy compatibility alias
            doctest_support,
        )

        value = getattr(doctest_support, name)
        vars(module)[name] = value
        return value

    def names() -> list[str]:
        return sorted({*vars(module), "DoctestItem", "DoctestModule"})

    vars(module)["__all__"] = ["DoctestItem", "DoctestModule"]
    vars(module)["__getattr__"] = load
    vars(module)["__dir__"] = names


def install_outcome_modules() -> Callable[[], None]:
    """Install the alias table and return the callable that restores the previous modules.

    The independent facade must already be ``pytest``, so real pytest is never
    mixed with the facade.
    """
    if not facade.is_installed_facade(sys.modules.get("pytest")):
        raise RuntimeError("independent outcome modules require the independent pytest facade")
    tables = _exports()
    previous = {name: sys.modules.get(name) for name in tables}
    for name, exports in tables.items():
        module = ModuleType(name)
        if name in _PACKAGES:
            module.__path__ = []
        vars(module).update(exports)
        if name == "_pytest.doctest":
            _install_lazy_doctest(module)
        parent, _, child = name.rpartition(".")
        if parent:
            setattr(sys.modules[parent], child, module)
        sys.modules[name] = module

    def restore() -> None:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    return restore


def pytest_load_initial_conftests(early_config: Config) -> None:
    """Install the aliases for the explicitly requested child facade scope."""
    early_config.add_cleanup(install_outcome_modules())
