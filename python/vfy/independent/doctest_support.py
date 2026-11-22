"""Collect and run doctests from modules and text files, as pytest's doctest plugin does."""

from __future__ import annotations

import bdb
import doctest
import inspect
import re
import sys
import traceback
import types
import warnings
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, override

from vfy import native

from .approx import approx
from .diagnostics import ReprFileLocation, TerminalRepr
from .facade import (
    ExceptionInfo,
    Exit,
    Failed,
    PytestWarning,
    Skipped,
    XFailed,
    exit as exit_session,
    skip,
)
from .protocol import File, Item

if TYPE_CHECKING:
    from _typeshed import MaybeNone

    from .protocol import Config

_REPORT_FLAGS = {
    "none": 0,
    "cdiff": doctest.REPORT_CDIFF,
    "ndiff": doctest.REPORT_NDIFF,
    "udiff": doctest.REPORT_UDIFF,
    "only_first_failure": doctest.REPORT_ONLY_FIRST_FAILURE,
}
_ALLOW_UNICODE = doctest.register_optionflag("ALLOW_UNICODE")
_ALLOW_BYTES = doctest.register_optionflag("ALLOW_BYTES")
_NUMBER = doctest.register_optionflag("NUMBER")
_OPTIONFLAGS = {
    "DONT_ACCEPT_TRUE_FOR_1": doctest.DONT_ACCEPT_TRUE_FOR_1,
    "DONT_ACCEPT_BLANKLINE": doctest.DONT_ACCEPT_BLANKLINE,
    "NORMALIZE_WHITESPACE": doctest.NORMALIZE_WHITESPACE,
    "ELLIPSIS": doctest.ELLIPSIS,
    "IGNORE_EXCEPTION_DETAIL": doctest.IGNORE_EXCEPTION_DETAIL,
    "COMPARISON_FLAGS": doctest.COMPARISON_FLAGS,
    "ALLOW_UNICODE": _ALLOW_UNICODE,
    "ALLOW_BYTES": _ALLOW_BYTES,
    "NUMBER": _NUMBER,
}


class ReprFailDoctest(TerminalRepr):
    """The source lines and difference shown for each failed example."""

    def __init__(
        self, reprlocation_lines: Sequence[tuple[ReprFileLocation, Sequence[str]]]
    ) -> None:
        self.reprlocation_lines = reprlocation_lines

    @override
    def toterminal(self, tw: object) -> None:
        writer: Any = tw
        for reprlocation, lines in self.reprlocation_lines:
            for line in lines:
                writer.line(line)
            reprlocation.toterminal(tw)


class MultipleDoctestFailures(Exception):  # noqa: N818 - pytest's public doctest exception name
    """All failed examples of one doctest when continuing after failures."""

    def __init__(self, failures: Sequence[doctest.DocTestFailure]) -> None:
        super().__init__()
        self.failures = failures


_OUTCOMES = (Skipped, Failed, XFailed, Exit)


class PytestDoctestRunner(doctest.DebugRunner):
    """Collect failures, or raise the first one, without printing doctest's report."""

    def __init__(
        self, checker: doctest.OutputChecker, optionflags: int, continue_on_failure: bool
    ) -> None:
        super().__init__(checker=checker, verbose=False, optionflags=optionflags)
        self.continue_on_failure = continue_on_failure

    @override
    def report_failure(
        self, out: Any, test: doctest.DocTest, example: doctest.Example, got: str
    ) -> None:
        self._record(out, doctest.DocTestFailure(test, example, got))

    @override
    def report_unexpected_exception(
        self,
        out: Any,
        test: doctest.DocTest,
        example: doctest.Example,
        exc_info: tuple[type[BaseException], BaseException, types.TracebackType],
    ) -> None:
        if isinstance(exc_info[1], _OUTCOMES):
            raise exc_info[1]
        if isinstance(exc_info[1], bdb.BdbQuit):
            exit_session("Quitting debugger")
        self._record(out, doctest.UnexpectedException(test, example, exc_info))

    def _record(
        self,
        out: list[doctest.DocTestFailure | doctest.UnexpectedException],
        failure: doctest.DocTestFailure | doctest.UnexpectedException,
    ) -> None:
        if self.continue_on_failure:
            out.append(failure)
        else:
            raise failure


def _runner(config: Config) -> PytestDoctestRunner:
    return PytestDoctestRunner(
        _LiteralsOutputChecker(),
        _optionflags(config),
        _continue_on_failure(config),
    )


class DoctestItem(Item):
    """One doctest; its examples run in a single item, as in pytest."""

    def __init__(
        self,
        name: str,
        parent: DoctestTextfile | DoctestModule,
        runner: PytestDoctestRunner,
        dtest: doctest.DocTest,
    ) -> None:
        super().__init__(name, parent)
        self.runner = runner
        self.dtest = dtest

    @override
    def setup(self) -> None:
        request: Any = self._request
        namespace: dict[str, object] = {"getfixture": request.getfixturevalue}
        namespace.update(request.getfixturevalue("doctest_namespace"))
        self.dtest.globs.update(namespace)

    @override
    def runtest(self) -> None:
        _check_all_skipped(self.dtest)
        failures: list[doctest.DocTestFailure] = []
        # The runner reports through this list; doctest calls it ``out``.
        self.runner.run(self.dtest, out=cast("Any", failures))
        if failures:
            raise MultipleDoctestFailures(failures)

    @override
    def repr_failure(self, excinfo: object) -> object:
        value = cast("ExceptionInfo", excinfo).value
        if isinstance(value, doctest.DocTestFailure | doctest.UnexpectedException):
            failures: Sequence[doctest.DocTestFailure | doctest.UnexpectedException] = [value]
        elif isinstance(value, MultipleDoctestFailures):
            failures = value.failures
        else:
            return super().repr_failure(excinfo)
        reprlocation_lines = []
        checker = _LiteralsOutputChecker()
        report_choice = _REPORT_FLAGS[cast("str", self.config.getoption("doctestreport"))]
        for failure in failures:
            example = failure.example
            test = failure.test
            lineno = None if test.lineno is None else test.lineno + example.lineno + 1
            reprlocation = ReprFileLocation(str(test.filename), lineno, type(failure).__name__)
            if lineno is not None and test.docstring is not None and test.lineno is not None:
                lines = [
                    f"{index + test.lineno + 1:03d} {line}"
                    for index, line in enumerate(test.docstring.splitlines(False))
                ]
                lines = lines[max(example.lineno - 9, 0) : example.lineno + 1]
            else:
                lines = ["EXAMPLE LOCATION UNKNOWN, not showing all tests of that example"]
                indent = ">>>"
                for line in example.source.splitlines():
                    lines.append(f"??? {indent} {line}")
                    indent = "..."
            if isinstance(failure, doctest.DocTestFailure):
                lines += checker.output_difference(example, failure.got, report_choice).split("\n")
            else:
                inner = ExceptionInfo.from_exc_info(failure.exc_info)
                lines += [f"UNEXPECTED EXCEPTION: {inner.value!r}"]
                lines += [
                    line.strip("\n") for line in traceback.format_exception(*failure.exc_info)
                ]
            reprlocation_lines.append((reprlocation, lines))
        return ReprFailDoctest(reprlocation_lines)

    @override
    def reportinfo(self) -> tuple[Path | str, int | None, str]:
        return self.path, self.dtest.lineno, f"[doctest] {self.name}"


def _optionflags(config: Config) -> int:
    flags = 0
    for name in cast("Iterable[str]", config.getini("doctest_optionflags")):
        flags |= _OPTIONFLAGS[name]
    return flags


def _continue_on_failure(config: Config) -> bool:
    if config.getvalue("doctest_continue_on_failure"):
        # pdb stops at each failure, so continuing makes no sense with it.
        return not config.getoption("usepdb", False)
    return False


class DoctestTextfile(File):
    """Collect the examples of one text file as a single doctest."""

    obj = None

    @override
    def collect(self) -> Iterable[DoctestItem]:
        text = self.path.read_text(cast("str", self.config.getini("doctest_encoding")))
        runner = _runner(self.config)
        test = doctest.DocTestParser().get_doctest(
            text, {"__name__": "__main__"}, self.path.name, str(self.path), 0
        )
        if test.examples:
            yield DoctestItem.from_parent(self, name=test.name, runner=runner, dtest=test)


def _check_all_skipped(test: doctest.DocTest) -> None:
    if all(example.options.get(doctest.SKIP, False) for example in test.examples):
        skip("all tests skipped by +SKIP option")


def _is_mocked(obj: object) -> bool:
    try:
        return getattr(obj, "pytest_mock_example_attribute_that_shouldnt_exist", None) is not None
    except Exception:  # noqa: BLE001 - a broken attribute protocol means the object is not a mock
        return False


@contextmanager
def _patch_unwrap_mock_aware() -> Generator[None]:
    """Stop ``inspect.unwrap`` at mocks, whose ``__wrapped__`` never ends."""
    real_unwrap = inspect.unwrap

    def _mock_aware_unwrap(
        func: Callable[..., Any], *, stop: Callable[[Callable[..., Any]], Any] | None = None
    ) -> Any:  # noqa: ANN401 - inspect.unwrap returns the innermost callable
        try:
            if stop is None or stop is _is_mocked:
                return real_unwrap(func, stop=_is_mocked)
            outer_stop = stop
            return real_unwrap(func, stop=lambda obj: _is_mocked(obj) or outer_stop(func))
        except Exception as error:
            warnings.warn(
                f"Got {error!r} when unwrapping {func!r}.  This is usually caused "
                "by a violation of Python's object protocol; see e.g. "
                "https://github.com/pytest-dev/pytest/issues/5080",
                PytestWarning,
                stacklevel=2,
            )
            raise

    # The stub types ``inspect.unwrap`` as a function, which rejects any replacement.
    patchable_inspect = cast("Any", inspect)
    patchable_inspect.unwrap = _mock_aware_unwrap
    try:
        yield
    finally:
        patchable_inspect.unwrap = real_unwrap


class _MockAwareDocTestFinder(doctest.DocTestFinder):
    def _find_lineno(self, obj: object, source_lines: list[str] | None) -> int | None:
        if isinstance(obj, property):
            obj = getattr(obj, "fget", obj)
        if hasattr(obj, "__wrapped__"):
            obj = inspect.unwrap(cast("Callable[..., Any]", obj))
        return cast("int | None", cast("Any", super())._find_lineno(obj, source_lines))

    def _find(
        self,
        tests: list[doctest.DocTest],
        obj: object,
        name: str,
        module: types.ModuleType,
        source_lines: list[str] | None,
        globs: dict[str, object],
        seen: dict[int, int],
    ) -> None:
        if _is_mocked(obj):
            return
        with _patch_unwrap_mock_aware():
            cast("Any", super())._find(tests, obj, name, module, source_lines, globs, seen)


class DoctestModule(File):
    """Collect the doctests in one Python module's docstrings."""

    obj = None

    def _import(self) -> types.ModuleType:
        if self.path.name == "conftest.py":
            for module in list(sys.modules.values()):
                if getattr(module, "__file__", None) == str(self.path):
                    return module
        try:
            return native._pytest.independent_import_path(self.config, self.path)
        except ImportError:
            if self.config.getvalue("doctest_ignore_import_errors"):
                skip(f"unable to import module {self.path!r}")
            raise

    @override
    def collect(self) -> Iterable[DoctestItem]:
        module = self._import()
        finder = _MockAwareDocTestFinder()
        runner = _runner(self.config)
        for test in finder.find(module, module.__name__):
            if test.examples:
                yield DoctestItem.from_parent(self, name=test.name, runner=runner, dtest=test)


class _LiteralsOutputChecker(doctest.OutputChecker):
    # pytest's checker: optional u''/b'' prefixes and approximate numbers.

    _unicode_literal_re = re.compile(r"(\W|^)[uU]([rR]?[\'\"])", re.UNICODE)
    _bytes_literal_re = re.compile(r"(\W|^)[bB]([rR]?[\'\"])", re.UNICODE)
    _number_re = re.compile(
        r"""
        (?P<number>
          (?P<mantissa>
            (?P<integer1> [+-]?\d*)\.(?P<fraction>\d+)
            |
            (?P<integer2> [+-]?\d+)\.
          )
          (?:
            [Ee]
            (?P<exponent1> [+-]?\d+)
          )?
          |
          (?P<integer3> [+-]?\d+)
          (?:
            [Ee]
            (?P<exponent2> [+-]?\d+)
          )
        )
        """,
        re.VERBOSE,
    )

    @override
    def check_output(self, want: str, got: str, optionflags: int) -> bool:
        if super().check_output(want, got, optionflags):
            return True
        allow_unicode = optionflags & _ALLOW_UNICODE
        allow_bytes = optionflags & _ALLOW_BYTES
        allow_number = optionflags & _NUMBER
        if not allow_unicode and not allow_bytes and not allow_number:
            return False
        if allow_unicode:
            want = self._unicode_literal_re.sub(r"\1\2", want)
            got = self._unicode_literal_re.sub(r"\1\2", got)
        if allow_bytes:
            want = self._bytes_literal_re.sub(r"\1\2", want)
            got = self._bytes_literal_re.sub(r"\1\2", got)
        if allow_number:
            got = self._remove_unwanted_precision(want, got)
        return super().check_output(want, got, optionflags)

    def _remove_unwanted_precision(self, want: str, got: str) -> str:
        wants = list(self._number_re.finditer(want))
        gots = list(self._number_re.finditer(got))
        if len(wants) != len(gots):
            return got
        offset = 0
        for wanted, received in zip(wants, gots, strict=True):
            fraction: str | MaybeNone = wanted.group("fraction")
            exponent: str | MaybeNone = wanted.group("exponent1")
            if exponent is None:
                exponent = wanted.group("exponent2")
            precision = 0 if fraction is None else len(fraction)
            if exponent is not None:
                precision -= int(exponent)
            if float(wanted.group()) == approx(float(received.group()), abs=10**-precision):
                got = (
                    got[: received.start() + offset]
                    + wanted.group()
                    + got[received.end() + offset :]
                )
                offset += received.end() - received.start() - (wanted.end() - wanted.start())
        return got
