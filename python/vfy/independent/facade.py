"""Bounded pytest objects for the explicitly selected independent runner.

Importing this module leaves pytest registration untouched. The runner installs
the facade before importing user modules and owns restoration of ``sys.modules``.
"""

from __future__ import annotations

import builtins
import importlib
import linecache
import re
import sys
import warnings
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from pprint import pformat
from traceback import format_exception, format_exception_only
from types import FrameType, ModuleType, TracebackType
from typing import Any, ClassVar, NoReturn, Self, cast, overload, override
from weakref import WeakSet

from .approx import approx
from .compat import NOTSET as NOTSET, NotSetType as NotSetType
from .fixture_lookup import FixtureLookupError
from .fixtures import (
    FixtureFunctionDefinition,
    fixture,
    reject_fixture_marks,
)
from .helpers import (
    MonkeyPatch as _MonkeyPatch,
    TempPathFactory,
    WarningsRecorder,
)
from .imports import PytestAssertRewriteWarning, register_assert_rewrite
from .raises import RaisesExc
from .stash import Stash, StashKey

_installed_facades: WeakSet[ModuleType] = WeakSet()


def is_installed_facade(module: ModuleType | None) -> bool:
    """Recognize owned module identities across nested invocation restoration."""
    return module is not None and module in _installed_facades


class MonkeyPatch(_MonkeyPatch):
    """Expose the fixture's actual compatibility type without changing its resource base."""

    __module__ = "_pytest.monkeypatch"


def _attach_metadata[T](target: T, name: str, value: object) -> T:
    setattr(target, name, value)
    return target


class ExitCode(IntEnum):
    """Public pytest session exit codes supported by the independent driver."""

    OK = 0
    TESTS_FAILED = 1
    INTERRUPTED = 2
    INTERNAL_ERROR = 3
    USAGE_ERROR = 4
    NO_TESTS_COLLECTED = 5
    MAX_WARNINGS_ERROR = 6


@dataclass(frozen=True)
class MarkDecorator:
    """Carry mark arguments until attached to a function or class."""

    name: str
    args: tuple[object, ...] = ()
    kwargs: dict[str, object] = field(default_factory=dict)

    @property
    def mark(self) -> MarkDecorator:
        return self

    def __call__(self, *args: object, **kwargs: object) -> object:
        if len(args) == 1 and not kwargs and callable(args[0]):
            target = args[0]
            if isinstance(target, FixtureFunctionDefinition):
                reject_fixture_marks()
            existing: object = (
                target.__dict__.get("pytestmark", [])
                if isinstance(target, type)
                else getattr(target, "pytestmark", [])
            )
            marks = existing if isinstance(existing, list) else [existing]
            return _attach_metadata(target, "pytestmark", [*marks, self.mark])
        return MarkDecorator(self.name, self.args + args, {**self.kwargs, **kwargs})


class MarkGenerator:
    def __getattr__(self, name: str) -> MarkDecorator:
        if name.startswith("_"):
            raise AttributeError(name)
        return MarkDecorator(name)


mark = MarkGenerator()


@dataclass(frozen=True)
class ParameterSet:
    values: tuple[object, ...]
    marks: tuple[MarkDecorator, ...]
    id: str | None


def param(
    *values: object,
    marks: MarkDecorator | Iterable[MarkDecorator] = (),
    id: str | None = None,  # noqa: A002 - pytest public keyword.
) -> ParameterSet:
    """Keep parameter marks and IDs available to the native collector."""
    if id is not None and not isinstance(id, str):
        raise TypeError("pytest.param id must be a string or None")
    normalized_marks = (marks,) if isinstance(marks, MarkDecorator) else tuple(marks)
    return ParameterSet(values, normalized_marks, id)


class OutcomeException(BaseException):
    """The base of the skip and failure outcomes that stop a test."""

    __module__ = "_pytest.outcomes"


class Skipped(OutcomeException):
    __module__ = "builtins"

    def __init__(self, reason: str = "", *, allow_module_level: bool = False) -> None:
        super().__init__(reason)
        self.msg = reason
        self.allow_module_level = allow_module_level
        self._use_item_location = False


class Interrupted(KeyboardInterrupt):
    __module__ = "builtins"


class Exit(Exception):  # noqa: N818 - pytest's public outcome class is named Exit.
    """Raised by the public helper to stop a pytest session."""

    __module__ = "_pytest.outcomes"

    def __init__(self, msg: str = "unknown reason", returncode: int | None = None) -> None:
        self.msg = msg
        self.returncode = returncode
        super().__init__(msg)


class _Exit:
    Exception: ClassVar[type[Exit]] = Exit

    def __call__(self, reason: str = "", returncode: int | None = None) -> NoReturn:
        __tracebackhide__ = True
        raise Exit(msg=reason, returncode=returncode)


exit = _Exit()  # noqa: A001 - pytest's public outcome helper uses this name.


class PytestWarning(UserWarning):
    pass


class PytestCollectionWarning(PytestWarning):
    """Warn when collection cannot collect a file or symbol in a module."""

    __module__ = "pytest"


class PytestDeprecationWarning(PytestWarning, DeprecationWarning):
    pass


class PytestRemovedIn10Warning(PytestDeprecationWarning):
    __module__ = "pytest"


class PytestExperimentalApiWarning(PytestWarning, FutureWarning):
    """Warn when a fixture exposes the experimental pytest attribute API."""


class PytestUnraisableExceptionWarning(PytestWarning):
    """Warn about a captured exception that Python cannot propagate."""


class PytestConfigWarning(PytestWarning):
    pass


class PytestFDWarning(PytestWarning):
    """Warn when the lsof plug-in finds leaked file descriptors."""

    __module__ = "pytest"


class PytestReturnNotNoneWarning(PytestWarning):
    """Warn when a test function returns a value other than None."""

    __module__ = "pytest"


class PytestUnhandledThreadExceptionWarning(PytestWarning):
    """Warn about an unhandled exception in a ``threading.Thread``."""

    __module__ = "pytest"


class PytestUnknownMarkWarning(PytestWarning):
    pass


class Failed(OutcomeException):
    __module__ = "builtins"

    def __init__(self, reason: str = "", *, pytrace: bool = True) -> None:
        super().__init__(reason)
        self.msg = reason
        self.pytrace = pytrace


class XFailed(Failed):
    __module__ = "_pytest.outcomes"


def skip(reason: str = "", *, allow_module_level: bool = False) -> None:
    __tracebackhide__ = True
    raise Skipped(reason, allow_module_level=allow_module_level)


def fail(reason: str = "", pytrace: bool = True) -> NoReturn:
    __tracebackhide__ = True
    raise Failed(reason, pytrace=pytrace)


def xfail(reason: str = "") -> None:
    __tracebackhide__ = True
    raise XFailed(reason)


# pytest consumers use these attributes when catching an explicit outcome.
_attach_metadata(skip, "Exception", Skipped)
_attach_metadata(fail, "Exception", Failed)
_attach_metadata(xfail, "Exception", XFailed)


class TracebackEntry:
    """One frame of a captured traceback, numbered from zero like pytest's."""

    def __init__(self, raw: TracebackType) -> None:
        self._rawentry = raw

    @property
    def frame(self) -> FrameType:
        return self._rawentry.tb_frame

    @property
    def lineno(self) -> int:
        return self._rawentry.tb_lineno - 1

    @property
    def path(self) -> Path | str:
        filename = self.frame.f_code.co_filename
        return Path(filename) if filename and not filename.startswith("<") else filename

    @property
    def name(self) -> str:
        return self.frame.f_code.co_name

    @property
    def locals(self) -> Mapping[str, Any]:
        return self.frame.f_locals

    @property
    def statement(self) -> str:
        return linecache.getline(self.frame.f_code.co_filename, self.lineno + 1).strip()

    def ishidden(self, excinfo: ExceptionInfo | None) -> bool:
        """Return whether the frame's ``__tracebackhide__`` hides it.

        A callable ``__tracebackhide__`` receives ``excinfo`` and decides.
        """
        hide: object = False
        for namespace in (self.frame.f_locals, self.frame.f_globals):
            try:
                hide = namespace["__tracebackhide__"]
            except Exception:  # noqa: BLE001 - pytest treats any failed namespace lookup as absent
                continue
            break
        if hide and callable(hide):
            return cast("bool", hide(excinfo))
        return cast("bool", hide)

    @override
    def __repr__(self) -> str:
        return f"<TracebackEntry {self.path}:{self.lineno + 1}>"

    @override
    def __str__(self) -> str:
        try:
            line = self.statement.lstrip()
        except Exception:  # noqa: BLE001 - pytest prints ??? for any unreadable source
            line = "???"
        return f"  File '{self.path}':{self.lineno + 1} in {self.name}\n  {line}\n"


class Traceback(list[TracebackEntry]):
    """The entries of a captured traceback."""

    @classmethod
    def from_traceback(cls, traceback: TracebackType | None) -> Self:
        entries = []
        while traceback is not None:
            entries.append(TracebackEntry(traceback))
            traceback = traceback.tb_next
        return cls(entries)

    def cut(self, path: object = None, lineno: int | None = None) -> Traceback:
        for index, entry in enumerate(self):
            if (path is None or str(entry.path) == str(path)) and (
                lineno is None or entry.lineno == lineno
            ):
                return Traceback(self[index:])
        return self

    def filter(
        self, excinfo_or_fn: ExceptionInfo | Callable[[TracebackEntry], bool], /
    ) -> Traceback:
        """Return the entries kept by a predicate, or those an ``ExceptionInfo`` does not hide."""
        if isinstance(excinfo_or_fn, ExceptionInfo):
            excinfo = excinfo_or_fn

            def keep(entry: TracebackEntry) -> bool:
                return not entry.ishidden(excinfo)

        else:
            keep = excinfo_or_fn
        return Traceback(entry for entry in self if keep(entry))


class ExceptionInfo:
    """Wrap a captured exception with pytest's public ``ExceptionInfo`` API."""

    _assert_start_repr = "AssertionError('assert "

    def __init__(
        self,
        excinfo: tuple[builtins.type[BaseException], BaseException, TracebackType | None]
        | None = None,
        striptext: str = "",
        traceback: Traceback | None = None,
        *,
        _ispytest: bool = False,
    ) -> None:
        self._value: BaseException | None = None
        self._traceback: TracebackType | None = None
        self._traceback_captured = False
        self._striptext = striptext
        self._entries = traceback
        if excinfo is not None:
            self._fill(excinfo[1], excinfo[2])

    @classmethod
    def from_exception(cls, exception: BaseException, exprinfo: str | None = None) -> Self:
        return cls.from_exc_info((type(exception), exception, exception.__traceback__), exprinfo)

    @classmethod
    def from_exc_info(
        cls,
        exc_info: tuple[builtins.type[BaseException], BaseException, TracebackType | None],
        exprinfo: str | None = None,
    ) -> Self:
        striptext = ""
        if exprinfo is None and isinstance(exc_info[1], AssertionError):
            exprinfo = cast("str | None", getattr(exc_info[1], "msg", None)) or repr(exc_info[1])
            if exprinfo.startswith(cls._assert_start_repr):
                striptext = "AssertionError: "
        return cls(exc_info, striptext)

    @classmethod
    def from_current(cls, exprinfo: str | None = None) -> Self:
        value = sys.exception()
        if value is None:
            raise AssertionError("no current exception")
        return cls.from_exc_info((type(value), value, value.__traceback__), exprinfo)

    @classmethod
    def for_later(cls) -> Self:
        return cls()

    def fill_unfilled(
        self, exc_info: tuple[builtins.type[BaseException], BaseException, TracebackType | None]
    ) -> None:
        if self._value is not None:
            raise AssertionError("ExceptionInfo was already filled")
        self._fill(exc_info[1], exc_info[2])

    def _fill(self, value: BaseException, traceback: TracebackType | None) -> None:
        self._value = value
        self._traceback = traceback
        self._traceback_captured = True
        self._entries = None

    @property
    def value(self) -> BaseException:
        if self._value is None:
            raise AssertionError("exception value is only available after the context exits")
        return self._value

    @property
    def type(self) -> builtins.type[BaseException]:
        return type(self.value)

    @property
    def tb(self) -> TracebackType | None:
        if self._traceback_captured:
            return self._traceback
        return self.value.__traceback__

    @property
    def typename(self) -> str:
        return self.type.__name__

    @property
    def traceback(self) -> Traceback:
        if self._entries is None:
            self._entries = Traceback.from_traceback(self.tb)
        return self._entries

    @traceback.setter
    def traceback(self, value: Traceback) -> None:
        self._entries = value

    def exconly(self, tryshort: bool = False) -> str:
        text = "".join(format_exception_only(self.type, self.value)).rstrip()
        if tryshort and text.startswith(self._striptext):
            text = text[len(self._striptext) :]
        return text

    def errisinstance(
        self, exc: builtins.type[BaseException] | tuple[builtins.type[BaseException], ...]
    ) -> bool:
        return isinstance(self.value, exc)

    def getrepr(self, *_args: object, **kwargs: object) -> ExceptionText:
        chain = kwargs.get("chain", True)
        lines = format_exception(self.type, self.value, self.tb, chain=bool(chain))
        return ExceptionText("".join(lines).rstrip("\n"))

    def group_contains(
        self,
        expected_exception: builtins.type[BaseException] | tuple[builtins.type[BaseException], ...],
        *,
        match: str | re.Pattern[str] | None = None,
        depth: int | None = None,
    ) -> bool:
        from vfy import native  # noqa: PLC0415 - group matching uses the native exception policy

        return native._pytest.exception_group_contains(self.value, expected_exception, match, depth)

    def match(self, regexp: str | re.Pattern[str]) -> bool:
        from vfy import native  # noqa: PLC0415 - match uses the installed native service

        return native._pytest.match_exception_message(self.value, regexp)

    @override
    def __repr__(self) -> str:
        if self._value is None:
            return "<ExceptionInfo for raises contextmanager>"
        return f"<ExceptionInfo {self.value!r} tblen={len(self.traceback)}>"


class ExceptionText(str):
    """The formatted text that ``ExceptionInfo.getrepr`` returns."""

    __slots__ = ()

    def toterminal(self, tw: object) -> None:
        for line in self.splitlines():
            cast("Any", tw).line(line)


def _regex_option(value: object) -> str | re.Pattern[str] | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, re.Pattern):
        pattern = value.pattern
        if isinstance(pattern, str):
            return re.compile(pattern, value.flags)
    raise TypeError("match must be a string or compiled regular expression")


@overload
def raises(
    expected_exception: type[BaseException] | tuple[type[BaseException], ...] | None = None,
    func: None = None,
    *,
    match: str | re.Pattern[str] | None = None,
    check: Callable[[BaseException], bool] | None = None,
) -> RaisesExc[BaseException]: ...


@overload
def raises(
    expected_exception: type[BaseException] | tuple[type[BaseException], ...],
    func: Callable[..., object],
    *args: object,
    **kwargs: object,
) -> ExceptionInfo: ...


def raises(
    expected_exception: type[BaseException] | tuple[type[BaseException], ...] | None = None,
    func: Callable[..., object] | None = None,
    *args: object,
    **kwargs: object,
) -> RaisesExc[BaseException] | ExceptionInfo:
    """Check a context or callable; callable keywords are forwarded unchanged."""
    context_form = func is None and not args
    if not context_form and not expected_exception:
        raise ValueError(
            "Expected an exception type or a tuple of exception types, "
            f"but got `{expected_exception!r}`. "
            "Raising exceptions is already understood as failing the test, so you don't need "
            "any special code to say 'this should never raise an exception'."
        )
    if context_form:
        if set(kwargs) - {"match", "check"}:
            raise TypeError(
                "Unexpected keyword arguments passed to pytest.raises: "
                + ", ".join(sorted(kwargs))
                + "\nUse context-manager form instead?"
            )
        return RaisesExc[BaseException](expected_exception, **kwargs)
    if not callable(func):
        raise TypeError(f"{func!r} object (type: {type(func)}) must be callable")
    with RaisesExc(expected_exception) as info:
        func(*args, **kwargs)
    return info


_attach_metadata(raises, "Exception", Failed)


class WarningsChecker(WarningsRecorder):
    """Record warnings and re-emit those that fail the expected category or pattern."""

    def __init__(
        self,
        expected_warning: type[Warning] | tuple[type[Warning], ...] = Warning,
        match_expr: str | re.Pattern[str] | None = None,
    ) -> None:
        expected = expected_warning if isinstance(expected_warning, tuple) else (expected_warning,)
        for category in expected:
            if not isinstance(category, type) or not issubclass(category, Warning):
                raise TypeError(f"exceptions must be derived from Warning, not {type(category)}")
        self.expected_warning = expected
        self.match_expr = match_expr
        self._context: warnings.catch_warnings[list[warnings.WarningMessage]] | None = None
        self._list: list[warnings.WarningMessage] = []

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}(record=True)"

    @property
    def list(self) -> builtins.list[warnings.WarningMessage]:
        return self._list

    def __enter__(self) -> Self:
        if self._context is not None:
            raise RuntimeError(f"Cannot enter {self!r} twice")
        self._context = warnings.catch_warnings(record=True)
        self._list = self._context.__enter__()
        warnings.simplefilter("always")
        return self

    def matches(self, record: warnings.WarningMessage) -> bool:
        return issubclass(record.category, self.expected_warning) and (
            self.match_expr is None or re.search(self.match_expr, str(record.message)) is not None
        )

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._context is None:
            raise RuntimeError(f"Cannot exit {self!r} without entering first")
        context, self._context = self._context, None
        context.__exit__(exception_type, exception, traceback)
        if exception is not None and (
            not isinstance(exception, Exception) or isinstance(exception, Exit)
        ):
            return
        emitted = pformat([record.message for record in self], indent=2)
        try:
            if not any(issubclass(record.category, self.expected_warning) for record in self):
                raise Failed(
                    f"DID NOT WARN. No warnings of type {self.expected_warning} were emitted.\n"
                    f" Emitted warnings: {emitted}."
                )
            if not any(self.matches(record) for record in self):
                hint = ""
                if isinstance(self.match_expr, str) and any(
                    self.match_expr == str(record.message)
                    for record in self
                    if issubclass(record.category, self.expected_warning)
                ):
                    hint = "\n Did you mean to `re.escape()` the regex?"
                raise Failed(
                    f"Regex pattern did not match any of the {len(self)} warnings emitted.\n"
                    f" Regex: {self.match_expr!r}\n Emitted warnings: {emitted}.{hint}"
                )
        finally:
            for record in self:
                if not self.matches(record):
                    warnings.warn_explicit(
                        message=record.message,
                        category=record.category,
                        filename=record.filename,
                        lineno=record.lineno,
                        module=record.__module__,
                        source=record.source,
                    )
            for record in self:
                if type(record.message) is UserWarning and record.message.args:
                    message = record.message.args[0]
                    if not isinstance(message, str):
                        raise TypeError(
                            f"Warning must be str or Warning, got {message!r} "
                            f"(type {type(message).__name__})"
                        )


@overload
def warns(
    expected_warning: type[Warning] | tuple[type[Warning], ...] = Warning,
    func: None = None,
    *,
    match: str | re.Pattern[str] | None = None,
) -> WarningsChecker: ...


@overload
def warns[R](
    expected_warning: type[Warning] | tuple[type[Warning], ...],
    func: Callable[..., R],
    *args: object,
    **kwargs: object,
) -> R: ...


def warns(
    expected_warning: type[Warning] | tuple[type[Warning], ...] = Warning,
    func: Callable[..., object] | None = None,
    *args: object,
    **kwargs: object,
) -> object:
    """Check warnings in a context, or return a warning-emitting callable's result."""
    return _warning_assertion(expected_warning, func, *args, **kwargs)


def _warning_assertion(
    expected_warning: type[Warning] | tuple[type[Warning], ...],
    func: object = None,
    *args: object,
    **kwargs: object,
) -> object:
    if func is None and not args:
        match = _regex_option(kwargs.pop("match", None))
        if kwargs:
            raise TypeError(
                "Unexpected keyword arguments passed to pytest.warns: "
                + ", ".join(sorted(kwargs))
                + "\nUse context-manager form instead?"
            )
        return WarningsChecker(expected_warning, match)
    if not callable(func):
        raise TypeError(f"{func!r} object (type: {type(func)}) must be callable")
    with WarningsChecker(expected_warning):
        return func(*args, **kwargs)


@overload
def deprecated_call(
    func: None = None,
    *,
    match: str | re.Pattern[str] | None = None,
) -> WarningsChecker: ...


@overload
def deprecated_call[R](func: Callable[..., R], *args: object, **kwargs: object) -> R: ...


def deprecated_call(
    func: Callable[..., object] | None = None, *args: object, **kwargs: object
) -> object:
    """Check deprecation, pending deprecation, or future warnings."""
    categories = (DeprecationWarning, PendingDeprecationWarning, FutureWarning)
    if func is None:
        return _warning_assertion(categories, *args, **kwargs)
    with WarningsChecker(categories):
        return func(*args, **kwargs)


def importorskip(
    modname: str,
    minversion: str | None = None,
    reason: str | None = None,
    *,
    exc_type: type[ImportError] | None = None,
) -> ModuleType:
    """Import an optional module, skipping when the selected import error occurs."""
    __tracebackhide__ = True
    compile(modname, "", "eval")
    expected_error = ModuleNotFoundError if exc_type is None else exc_type
    skipped: Skipped | None = None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            importlib.import_module(modname)
        except expected_error as exception:
            skipped = Skipped(
                reason if reason is not None else f"could not import {modname!r}: {exception}",
                allow_module_level=True,
            )
    if skipped is not None:
        raise skipped
    module = sys.modules[modname]
    if minversion is not None:
        from vfy.native import pytest_version_less_than  # noqa: PLC0415 - optional version check

        version = getattr(module, "__version__", None)
        if version is None or pytest_version_less_than(version, minversion):
            raise Skipped(
                f"module {modname!r} has __version__ {version!r}, required is: {minversion!r}",
                allow_module_level=True,
            )
    return module


@overload
def hookimpl[T](
    function: T,
    hookwrapper: bool = False,
    optionalhook: bool = False,
    tryfirst: bool = False,
    trylast: bool = False,
    specname: str | None = None,
    wrapper: bool = False,
) -> T: ...


@overload
def hookimpl[T](
    function: None = None,
    hookwrapper: bool = False,
    optionalhook: bool = False,
    tryfirst: bool = False,
    trylast: bool = False,
    specname: str | None = None,
    wrapper: bool = False,
) -> Callable[[T], T]: ...


def hookimpl[T](
    function: T | None = None,
    hookwrapper: bool = False,
    optionalhook: bool = False,
    tryfirst: bool = False,
    trylast: bool = False,
    specname: str | None = None,
    wrapper: bool = False,
) -> T | Callable[[T], T]:
    """Attach hook implementation options consumed by the independent hook bridge."""

    def decorate(target: T) -> T:
        return _attach_metadata(
            target,
            "__vfy_hookimpl__",
            {
                "wrapper": wrapper,
                "hookwrapper": hookwrapper,
                "optionalhook": optionalhook,
                "tryfirst": tryfirst,
                "trylast": trylast,
                "specname": specname,
            },
        )

    return decorate if function is None else decorate(function)


def hookspec[T](
    function: T | None = None,
    firstresult: bool = False,
    historic: bool = False,
    warn_on_impl: Warning | None = None,
    warn_on_impl_args: Mapping[str, Warning] | None = None,
) -> T | Callable[[T], T]:
    """Attach hook specification options consumed by the independent hook bridge."""

    def decorate(target: T) -> T:
        if historic and firstresult:
            raise ValueError("cannot have a historic firstresult hook")
        return _attach_metadata(
            target,
            "__vfy_hookspec__",
            {
                "firstresult": firstresult,
                "historic": historic,
                "warn_on_impl": warn_on_impl,
                "warn_on_impl_args": warn_on_impl_args,
            },
        )

    return decorate if function is None else decorate(function)


def set_trace(*args: object, **kwargs: object) -> None:
    """Enter the Python debugger at the caller's frame."""
    import pdb  # noqa: PLC0415, T100 - the public helper starts the debugger on request

    # Arguments are forwarded unchanged; pdb.Pdb validates the Pdb constructor arguments.
    debugger = cast("Callable[..., pdb.Pdb]", pdb.Pdb)
    debugger(*args, **kwargs).set_trace(sys._getframe(1))


def install_pytest_facade() -> ModuleType:
    """Register and return the facade for an independent-runner process."""
    # Import protocol types only during explicit facade installation.
    from vfy import native  # noqa: PLC0415 - load extension only for explicit facade installation

    from .cache import Cache, PytestCacheWarning  # noqa: PLC0415 - facade setup
    from .capture import LogCaptureFixture  # noqa: PLC0415 - facade setup
    from .collectors import (  # noqa: PLC0415 - actual native-owned collector resources
        Class,
        Dir,
        Directory,
        File,
        Function,
        FunctionDefinition,
        Module,
        Package,
    )
    from .protocol import (  # noqa: PLC0415 - protocols import mark definitions from this module
        Collector,
        Config,
        Item,
        Metafunc,
        Node,
        OptionGroup,
        Parser,
        PluginManager,
        Session,
    )
    from .terminal import TerminalReporter  # noqa: PLC0415 - facade setup

    public_package = Path(__file__).with_name("_children") / "pytest"
    public_source = str(public_package / "__init__.py")
    spec = spec_from_file_location("pytest", public_source)
    if spec is None:
        raise ImportError(f"cannot create module specification for {public_source}")
    module = module_from_spec(spec)
    module.__doc__ = "vfy independent runner's bounded pytest API"
    for name, value in {
        "fixture": fixture,
        "yield_fixture": fixture,
        "mark": mark,
        "param": param,
        "skip": skip,
        "xfail": xfail,
        "fail": fail,
        "exit": exit,
        "raises": raises,
        "RaisesExc": RaisesExc,
        "warns": warns,
        "deprecated_call": deprecated_call,
        "ExceptionInfo": ExceptionInfo,
        "CallInfo": native._pytest.NativeIndependentCallInfo,
        "CollectReport": native._pytest.NativeIndependentCollectReport,
        "TestReport": native._pytest.NativeIndependentReport,
        "Subtests": native._pytest.Subtests,
        "SubtestReport": native._pytest.SubtestReport,
        "approx": approx,
        "importorskip": importorskip,
        "register_assert_rewrite": register_assert_rewrite,
        "PytestAssertRewriteWarning": PytestAssertRewriteWarning,
        "PytestWarning": PytestWarning,
        "PytestUnraisableExceptionWarning": PytestUnraisableExceptionWarning,
        "PytestExperimentalApiWarning": PytestExperimentalApiWarning,
        "PytestCacheWarning": PytestCacheWarning,
        "PytestCollectionWarning": PytestCollectionWarning,
        "PytestDeprecationWarning": PytestDeprecationWarning,
        "PytestRemovedIn10Warning": PytestRemovedIn10Warning,
        "PytestConfigWarning": PytestConfigWarning,
        "PytestFDWarning": PytestFDWarning,
        "PytestReturnNotNoneWarning": PytestReturnNotNoneWarning,
        "PytestUnhandledThreadExceptionWarning": PytestUnhandledThreadExceptionWarning,
        "Mark": MarkDecorator,
        "MarkDecorator": MarkDecorator,
        "MarkGenerator": MarkGenerator,
        "OptionGroup": OptionGroup,
        "WarningsRecorder": WarningsRecorder,
        "TerminalReporter": TerminalReporter,
        "set_trace": set_trace,
        "PytestUnknownMarkWarning": PytestUnknownMarkWarning,
        "Stash": Stash,
        "StashKey": StashKey,
        "hookimpl": hookimpl,
        "hookspec": hookspec,
        "MonkeyPatch": MonkeyPatch,
        "TempPathFactory": TempPathFactory,
        "CaptureFixture": native._pytest.NativeCaptureFixture,
        "LogCaptureFixture": LogCaptureFixture,
        "FixtureRequest": native._pytest.NativeIndependentRequest,
        "FixtureLookupError": FixtureLookupError,
        "Config": Config,
        "Cache": Cache,
        "Session": Session,
        "Node": Node,
        "Collector": Collector,
        "Directory": Directory,
        "Dir": Dir,
        "File": File,
        "Package": Package,
        "Module": Module,
        "Class": Class,
        "FunctionDefinition": FunctionDefinition,
        "Item": Item,
        "Function": Function,
        "Metafunc": Metafunc,
        "Parser": Parser,
        "PytestPluginManager": PluginManager,
        "ExitCode": ExitCode,
        "UsageError": native._pytest.IndependentUsageError,
        "__version__": "9.1.1",
        "version_tuple": (9, 1, 1),
    }.items():
        setattr(module, name, value)

    def lazy_getattr(name: str) -> object:
        if name == "DoctestItem":
            # Keep the public class identical to the actual collector's item.
            from .doctest_support import DoctestItem  # noqa: PLC0415 - lazy public export

            vars(module)["DoctestItem"] = DoctestItem
            return DoctestItem
        raise AttributeError(f"module {module.__name__!r} has no attribute {name!r}")

    vars(module)["__all__"] = [name for name in vars(module) if not name.startswith("_")] + [
        "DoctestItem"
    ]
    vars(module)["__getattr__"] = lazy_getattr
    vars(module)["__dir__"] = lambda: sorted(set(vars(module)) | {"DoctestItem"})
    _installed_facades.add(module)
    sys.modules["pytest"] = module
    return module
