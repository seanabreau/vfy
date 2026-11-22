"""pytest's JUnit XML reporter and fixtures; Rust renders and publishes the document."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, cast

from .facade import PytestExperimentalApiWarning, PytestWarning, fixture, hookimpl

if TYPE_CHECKING:
    from vfy._pytest_native import (
        NativeIndependentReport,
        NativeIndependentRequest,
        NativeRunResult,
    )

    from .protocol import Config
    from .terminal import TerminalReporter


def _check_record_param_type(param: str, value: object) -> None:
    __tracebackhide__ = True
    if not isinstance(value, str):
        raise TypeError(f"{param} parameter needs to be a string, but {type(value).__name__} given")


class _Node(Protocol):
    nodeid: str
    user_properties: list[tuple[object, object]]

    def warn(self, warning: Warning) -> None: ...


def _node(request: NativeIndependentRequest) -> _Node:
    return cast("_Node", request.node)


_Pairs = list[tuple[str, str]]


def _add_properties(target: _Pairs, properties: Iterable[tuple[object, object]]) -> None:
    # pytest's finalize converts the value, then add_property converts the name
    # and escapes the converted value with a second ``str`` call. Pairs added
    # before a failing conversion remain in the testcase.
    for name, value in properties:
        text = str(value)
        target.append((str(name), _escaped(text)))


def _escaped(value: object) -> str:
    # ``bin_xml_escape`` returns ``re.sub``'s exact ``str``, so a ``str``
    # subclass returned by ``__str__`` is released at once. Rust escapes it.
    return str.__str__(str(value))


class _TestCase:
    """One pytest ``_NodeReporter``: a testcase element and its pending data."""

    def __init__(self) -> None:
        self.call_failure: _Pairs | None = None
        self.properties: _Pairs = []
        self.attributes: _Pairs = []
        self.finalized = False
        self.duration = 0.0
        self.teardown_unwritten = False
        # pytest names a testcase only when a report opens it; a passed setup
        # or teardown alone only accumulates its duration.
        self.opened = False

    def entry(self) -> tuple[_Pairs | None, _Pairs, _Pairs, bool]:
        return (self.call_failure, self.properties, self.attributes, self.teardown_unwritten)


class LogXML:
    """Collect the report data pytest's ``LogXML`` writes for one session."""

    def __init__(self, config: Config, logfile: str, *, worker: bool) -> None:
        logfile = os.path.expanduser(os.path.expandvars(logfile))
        self.logfile = os.path.normpath(os.path.abspath(logfile))
        self.config = config
        self.worker = worker
        self.report_duration = cast("str", config.getini("junit_duration_report"))
        family = cast("str", config.getini("junit_family"))
        self.family = "xunit1" if family == "legacy" else family
        self.global_properties: list[tuple[str, str]] = []
        self.open_cases: dict[str, _TestCase] = {}
        self.cases: dict[str, list[_TestCase]] = {}
        # Failed call reports, subtests' included, awaiting their teardown.
        self.open_reports: list[NativeIndependentReport] = []
        self.locations: dict[str, tuple[str, int | None]] = {}
        self.internal_errors: list[str] = []
        self.timestamp = datetime.now().astimezone().isoformat()
        self.result: NativeRunResult | None = None

    def add_global_property(self, name: str, value: object) -> None:
        __tracebackhide__ = True
        _check_record_param_type("name", name)
        self.global_properties.append((name, _escaped(value)))

    def attribute_recorder(self, nodeid: str) -> Callable[[object, object], None]:
        """Return ``record_xml_attribute``'s callable for one testcase."""
        case = self._testcase(nodeid)

        def add_attribute(name: object, value: object) -> None:
            # ``attrs[str(name)] = bin_xml_escape(value)`` converts the value first.
            escaped = _escaped(value)
            if case.finalized:
                raise AttributeError("'_NodeReporter' object has no attribute 'attrs'")
            case.attributes.append((str(name), escaped))

        return add_attribute

    def _testcase(self, nodeid: str) -> _TestCase:
        case = self.open_cases.get(nodeid)
        if case is None:
            case = self.open_cases[nodeid] = _TestCase()
            self.cases.setdefault(nodeid, []).append(case)
        return case

    def bind_result(self, result: NativeRunResult) -> None:
        self.result = result

    @hookimpl
    def pytest_sessionstart(self) -> None:
        self.timestamp = datetime.now().astimezone().isoformat()

    @hookimpl
    def pytest_runtest_logreport(self, report: NativeIndependentReport) -> None:
        file, line, _domain = report.location
        self.locations.setdefault(report.nodeid, (str(file), line))
        if not report.passed or report.when == "call":
            self._testcase(report.nodeid).opened = True
        if self.report_duration in {"total", report.when}:
            self._testcase(report.nodeid).duration += report.duration
        if report.when == "call" and report.failed:
            self.open_reports.append(report)
        elif report.when == "teardown":
            case = self._testcase(report.nodeid)
            call_failure = next(
                (pending for pending in self.open_reports if pending.nodeid == report.nodeid), None
            )
            if call_failure is not None:
                self.open_reports.remove(call_failure)
            if call_failure is not None and report.failed:
                case.call_failure = []
                # pytest opens the teardown testcase only after this conversion.
                case.teardown_unwritten = True
                _add_properties(case.call_failure, call_failure.user_properties)
                case.finalized = True
                case.teardown_unwritten = False
            case.opened = True
            del self.open_cases[report.nodeid]
            _add_properties(case.properties, report.user_properties)
            case.finalized = True

    @hookimpl
    def pytest_internalerror(self, excrepr: object) -> None:
        self.internal_errors.append(str(excrepr))

    @hookimpl
    def pytest_sessionfinish(self) -> None:
        result = self.result
        if result is None:
            return
        result.set_junit_case_data(
            {
                nodeid: [case.entry() for case in cases if case.opened]
                for nodeid, cases in self.cases.items()
            }
        )
        if self.worker:
            result.set_worker_junit_properties(self.global_properties)
            return
        import platform  # noqa: PLC0415 - hostname is needed only when writing JUnit
        import xml.etree.ElementTree as ET  # noqa: PLC0415 - preserve live namespaces when writing JUnit

        locations = dict(result.junit_locations(self.family))
        locations.update(self.locations)
        result.write_junit(
            self.logfile,
            {
                "suite_name": self.config.getini("junit_suite_name"),
                "prefix": self.config.getoption("junitprefix", None),
                "family": self.family,
                "logging": self.config.getini("junit_logging"),
                "report_duration": self.report_duration,
                "log_passing_tests": self.config.getini("junit_log_passing_tests"),
                "timestamp": self.timestamp,
                "hostname": platform.node(),
                "locations": locations,
                "global_properties": self.global_properties,
                "internal_errors": self.internal_errors,
                # ``register_namespace`` prefixes, which ElementTree applies to
                # ``{uri}local`` attribute names.
                "namespace_map": dict(cast("dict[str, str]", vars(ET)["_namespace_map"])),
                "duration_only": [
                    (case.attributes, case.duration)
                    for case in self.open_cases.values()
                    if not case.opened
                ],
            },
        )

    @hookimpl
    def pytest_terminal_summary(self, terminalreporter: TerminalReporter, config: Config) -> None:
        if not self.worker and config.get_verbosity() >= 0:
            terminalreporter.write_sep("-", f"generated xml file: {self.logfile}")


def _writer(request: NativeIndependentRequest) -> LogXML | None:
    return cast("LogXML | None", getattr(request.config, "_vfy_junit_writer", None))


def _warn_incompatibility_with_xunit2(request: NativeIndependentRequest, fixture_name: str) -> None:
    xml = _writer(request)
    if xml is not None and xml.family not in ("xunit1", "legacy"):
        _node(request).warn(
            PytestWarning(
                f"{fixture_name} is incompatible with junit_family '{xml.family}' "
                "(use 'legacy' or 'xunit1')"
            )
        )


@fixture
def record_property(request: NativeIndependentRequest) -> Callable[[str, object], None]:
    """Add an extra property to the calling test's JUnit testcase."""
    _warn_incompatibility_with_xunit2(request, "record_property")

    def append_property(name: str, value: object) -> None:
        _node(request).user_properties.append((name, value))

    return append_property


@fixture
def record_xml_attribute(request: NativeIndependentRequest) -> Callable[[str, object], None]:
    """Add an extra XML attribute to the calling test's JUnit testcase."""
    _node(request).warn(
        PytestExperimentalApiWarning("record_xml_attribute is an experimental feature")
    )
    _warn_incompatibility_with_xunit2(request, "record_xml_attribute")
    xml = _writer(request)
    if xml is None:

        def add_attr_noop(name: str, value: object) -> None:
            pass

        return add_attr_noop
    return xml.attribute_recorder(_node(request).nodeid)


@fixture(scope="session")
def record_testsuite_property(request: NativeIndependentRequest) -> Callable[[str, object], None]:
    """Record a property in the JUnit document's ``testsuite`` element."""
    __tracebackhide__ = True

    def record_func(name: str, value: object) -> None:
        """No-op function in case --junit-xml was not passed in the command-line."""
        __tracebackhide__ = True
        _check_record_param_type("name", name)

    xml = _writer(request)
    return record_func if xml is None else xml.add_global_property


class JUnitProvider:
    """Expose the fixtures and the writer's lifetime through plugin registration."""

    record_property = staticmethod(record_property)
    record_xml_attribute = staticmethod(record_xml_attribute)
    record_testsuite_property = staticmethod(record_testsuite_property)

    @hookimpl
    def pytest_unconfigure(self, config: Config) -> None:
        writer = getattr(config, "_vfy_junit_writer", None)
        if writer is not None:
            config.pluginmanager.unregister(writer)
            setattr(config, "_vfy_junit_writer", None)  # noqa: B010 - not a declared attribute
