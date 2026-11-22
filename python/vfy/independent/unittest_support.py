"""Bridge stdlib TestCase lifecycle and result callbacks to the native pytest owner."""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable, Iterator, Mapping
from types import FunctionType, TracebackType
from typing import Protocol, cast, override
from unittest import TestCase, case

from .collectors import Class, Function
from .facade import Failed, Skipped, XFailed, fail, fixture, skip, xfail
from .fixtures import FixtureFunctionDefinition

# CPython does not export types for these private stdlib result-protocol values.
_SubTest = vars(case)["_SubTest"]
_subtest_msg_sentinel = vars(case)["_subtest_msg_sentinel"]


class Subtest(Protocol):
    test_case: TestCase
    _message: object
    params: Mapping[str, object]


class UnexpectedSuccessReason(Protocol):
    reason: object


ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]


class ResultCallback(Protocol):
    def __call__(
        self,
        raw: ExcInfo | None,
        msg: str | None = None,
        params: Mapping[str, object] | None = None,
    ) -> None: ...


class LifecycleRequest(Protocol):
    cls: type[TestCase]
    instance: TestCase
    function: Callable[..., object]


class UnitTestCase(Class):
    nofuncargs = True

    @override
    def newinstance(self) -> TestCase:
        return cast(type[TestCase], self.obj)("runTest")


class TestCaseFunction(Function):
    nofuncargs = True
    failfast = False
    _vfy_unittest_result: ResultCallback
    instance: TestCase

    @property
    def _testcase(self) -> TestCase:
        return self.instance

    def startTest(self, testcase: TestCase) -> None:  # noqa: N802 - stdlib TestResult callback name.
        pass

    def stopTest(self, testcase: TestCase) -> None:  # noqa: N802 - stdlib TestResult callback name.
        pass

    def addSuccess(self, testcase: TestCase) -> None:  # noqa: N802 - stdlib TestResult callback name.
        pass

    def addDuration(self, testcase: TestCase, elapsed: float) -> None:  # noqa: N802 - stdlib TestResult callback name.
        pass

    def addError(self, testcase: TestCase, raw: ExcInfo) -> None:  # noqa: N802 - stdlib TestResult callback name.
        self._vfy_unittest_result(raw)

    def addFailure(self, testcase: TestCase, raw: ExcInfo) -> None:  # noqa: N802 - stdlib TestResult callback name.
        self._vfy_unittest_result(raw)

    def addSkip(self, testcase: TestCase, reason: str) -> None:  # noqa: N802 - stdlib TestResult callback name.
        try:
            skip(reason)
        except Skipped as error:
            error._use_item_location = True
            self._vfy_unittest_result(cast(ExcInfo, sys.exc_info()))
        if isinstance(testcase, _SubTest):
            self.addSubTest(cast(Subtest, testcase).test_case, testcase, None)

    def addExpectedFailure(self, testcase: TestCase, raw: ExcInfo, reason: str = "") -> None:  # noqa: N802 - stdlib TestResult callback name.
        try:
            xfail(str(reason))
        except XFailed:
            self._vfy_unittest_result(cast(ExcInfo, sys.exc_info()))

    def addUnexpectedSuccess(self, testcase: TestCase, reason: object = None) -> None:  # noqa: N802 - stdlib TestResult callback name.
        message = "Unexpected success"
        if reason:
            message += f": {cast(UnexpectedSuccessReason, reason).reason}"
        try:
            fail(message, pytrace=False)
        except Failed:
            self._vfy_unittest_result(cast(ExcInfo, sys.exc_info()))

    def addSubTest(self, testcase: TestCase, subtest: TestCase, raw: ExcInfo | None) -> None:  # noqa: N802 - stdlib TestResult callback name.
        context = cast(Subtest, subtest)
        msg = None if context._message is _subtest_msg_sentinel else str(context._message)
        self._vfy_unittest_result(raw, msg, context.params)

    @override
    def runtest(self) -> None:
        testcase = self.instance
        if inspect.iscoroutinefunction(self.obj) or inspect.isasyncgenfunction(self.obj):
            cast(Callable[..., object], testcase)(result=self)
            return
        # Hooks may replace obj after collection; TestCase.run must call that same method.
        setattr(testcase, self.name, self.obj)
        try:
            # stdlib TestCase consumes the structural result callback protocol.
            cast(Callable[..., object], testcase)(result=self)
        finally:
            delattr(testcase, self.name)


def _cleanup_errors(cls: type[TestCase]) -> None:
    exceptions = [error for _, error, _ in getattr(cls, "tearDown_exceptions", ())]
    if len(exceptions) == 1:
        raise exceptions[0]
    if exceptions:
        raise ExceptionGroup("Unittest class cleanup errors", exceptions)


def _optional_argument(function: Callable[..., object], cls: type[TestCase]) -> None:
    count = cast(FunctionType, function).__code__.co_argcount - int(inspect.ismethod(function))
    if count:
        function(cls)
    else:
        function()


def _skip_if_marked(obj: object) -> None:
    if getattr(obj, "__unittest_skip__", False):
        try:
            skip(getattr(obj, "__unittest_skip_why__", ""))
        except Skipped as error:
            error._use_item_location = True
            raise


def _class_fixture(
    cls: type[TestCase],
    setup: Callable[[], object] | None,
    teardown: Callable[[], object] | None,
) -> FixtureFunctionDefinition:
    cleanup = getattr(cls, "doClassCleanups", lambda: None)

    @fixture(name=f"_unittest_setUpClass_fixture_{cls.__qualname__}", scope="class", autouse=True)
    def setup_class(request: LifecycleRequest) -> Iterator[None]:
        _skip_if_marked(request.cls)
        if setup is not None:
            try:
                setup()
            except Exception:
                cleanup()
                _cleanup_errors(cls)
                raise
        yield
        try:
            if teardown is not None:
                teardown()
        finally:
            cleanup()
            _cleanup_errors(cls)

    return setup_class


def lifecycle_fixtures(cls: type[TestCase]) -> list[FixtureFunctionDefinition]:
    """Adapt class lifecycle calls into fixture declarations."""
    if getattr(cls, "__unittest_skip__", False):

        @fixture(name=f"_unittest_skip_fixture_{cls.__qualname__}", scope="class", autouse=True)
        def skip_class(request: LifecycleRequest) -> None:
            try:
                skip(getattr(cls, "__unittest_skip_why__", ""))
            except Skipped as error:
                error._use_item_location = True
                raise

        return [skip_class]

    fixtures: list[FixtureFunctionDefinition] = []
    setup = getattr(cls, "setup_method", None)
    teardown = getattr(cls, "teardown_method", None)
    if setup is not None or teardown is not None:

        @fixture(name=f"_unittest_setup_method_fixture_{cls.__qualname__}", autouse=True)
        def setup_method(request: LifecycleRequest) -> Iterator[None]:
            _skip_if_marked(request.instance)
            if setup is not None:
                setup(request.instance, request.function)
            yield
            if teardown is not None:
                teardown(request.instance, request.function)

        fixtures.append(setup_method)

    setup_cls = getattr(cls, "setUpClass", None)
    teardown_cls = getattr(cls, "tearDownClass", None)
    if setup_cls is not None or teardown_cls is not None:
        fixtures.append(_class_fixture(cls, setup_cls, teardown_cls))

    setup_xunit = getattr(cls, "setup_class", None)
    teardown_xunit = getattr(cls, "teardown_class", None)
    if setup_xunit is not None and hasattr(setup_xunit, "__vfy_fixture__"):
        setup_xunit = None
    if teardown_xunit is not None and hasattr(teardown_xunit, "__vfy_fixture__"):
        teardown_xunit = None
    if setup_xunit is not None or teardown_xunit is not None:

        @fixture(name=f"_xunit_setup_class_fixture_{cls.__qualname__}", scope="class", autouse=True)
        def xunit_class(request: LifecycleRequest) -> Iterator[None]:
            if setup_xunit is not None:
                _optional_argument(setup_xunit, cls)
            yield
            if teardown_xunit is not None:
                _optional_argument(teardown_xunit, cls)

        fixtures.append(xunit_class)
    return fixtures
