"""Exercise call/report APIs with actual collected items; load as a conftest plug-in."""

from __future__ import annotations

import gc
import inspect
import json
import warnings
import weakref
from pathlib import Path


def parameters(value) -> list[list[object]]:
    return [
        [
            name,
            parameter.kind.name,
            "required" if parameter.default is inspect.Parameter.empty else repr(parameter.default),
        ]
        for name, parameter in inspect.signature(value).parameters.items()
    ]


def call_observations(pytest, call_type, values):
    observed: dict[str, object] = {}
    namespace = {"CallInfo": call_type}
    exec(
        compile(
            "def observe(call: CallInfo[None]): pass", "<runtime-hook>", "exec", dont_inherit=True
        ),
        namespace,
    )
    observed["runtime_annotation"] = (
        namespace["observe"].__annotations__["call"].__origin__ is call_type
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        constructed = call_type("constructor value", None, 1.0, 2.0, 1.0, "collect")
    observed["call_constructor"] = {
        "result": constructed.result,
        "warnings": [[type(warning.message).__name__, str(warning.message)] for warning in caught],
    }
    call = call_type.from_call(lambda: values, "collect")
    assert isinstance(call, call_type)
    observed["call_success"] = {
        "result_identity": call.result is values,
        "excinfo_none": call.excinfo is None,
        "when": call.when,
        "timing": call.start <= call.stop and call.duration >= 0,
    }
    call.start, call.stop, call.duration, call.when = 1.0, 3.0, 2.0, "setup"
    call._result = "replaced"
    observed["call_mutation"] = {
        "timing": [call.start, call.stop, call.duration],
        "when": call.when,
        "result": call.result,
        "repr": repr(call),
    }
    call.when = "collect"
    marker = object()
    arbitrary = call_type.from_call(lambda: marker, "collect")
    observed["arbitrary_result_identity"] = arbitrary.result is marker

    error = RuntimeError("original collection error")

    def fail() -> None:
        raise error

    failed = call_type.from_call(fail, "collect")
    traceback = failed.excinfo.tb
    while traceback.tb_next is not None:
        traceback = traceback.tb_next
    try:
        _ = failed.result
    except AttributeError as unavailable:
        unavailable_contract = str(unavailable).endswith("has no valid result")
    else:
        unavailable_contract = False
    observed["call_failure"] = {
        "original_exception": failed.excinfo.value is error,
        "original_traceback": failed.excinfo.tb is error.__traceback__,
        "exception_info": isinstance(failed.excinfo, pytest.ExceptionInfo),
        "result_unavailable": unavailable_contract,
        "traceback_function": traceback.tb_frame.f_code.co_name,
    }
    assertion_events = []

    class CapturedAssertionError(AssertionError):
        def __repr__(self) -> str:
            assertion_events.append("repr")
            return "AssertionError('assert original')"

    assertion = CapturedAssertionError("original")

    def fail_assertion() -> None:
        raise assertion

    assertion_call = call_type.from_call(fail_assertion, "collect")
    observed["assertion_capture"] = {
        "events": assertion_events,
        "original_exception": assertion_call.excinfo.value is assertion,
    }
    failed.excinfo = None
    observed["cleared_exception_result"] = failed.result
    captured = []
    for control in (KeyboardInterrupt("kept"), SystemExit(7), GeneratorExit("kept")):

        def raise_control(error=control) -> None:
            raise error

        control_call = call_type.from_call(raise_control, "collect")
        captured.append(control_call.excinfo.value is control)
    observed["default_control_capture"] = captured
    reraised = []
    for reraise in (RuntimeError, (LookupError, RuntimeError)):
        try:
            call_type.from_call(fail, "collect", reraise=reraise)
        except RuntimeError as caught:
            reraised.append(caught is error)
        else:
            reraised.append(False)
    observed["explicit_reraise_identity"] = reraised

    reentry: list[object] = []
    reference = weakref.ref(call)

    class Reenter:
        def __repr__(self) -> str:
            owner = reference()
            owner.duration = 9.0
            reentry.append(["repr", owner.when, owner.duration])
            return "reentrant-result"

        def __del__(self) -> None:
            owner = reference()
            if owner is not None:
                owner.duration = 11.0
                reentry.append(["drop", owner.when, owner.duration])

    call._result = Reenter()
    observed["reentrant_repr"] = repr(call)
    call._result = None
    observed["call_reentry"] = reentry
    cycle: list[object] = []
    cyclic_call = call_type.from_call(lambda value=cycle: value, "collect")
    cyclic_call_ref = weakref.ref(cyclic_call)
    cycle.append(cyclic_call)
    del cycle, cyclic_call
    gc.collect()
    observed["call_cycle_collected"] = cyclic_call_ref() is None

    return observed, call


def report_observations(report_type, values, call):
    observed: dict[str, object] = {}
    sections = [("Captured stdout collect", "first"), ("Captured log collect", "log-one")]
    report = report_type(
        "test_subject.py",
        "passed",
        None,
        values,
        sections=(section for section in sections),
        note={"batch": "api"},
        when="setup",
    )
    observed["report_construct"] = {
        "result_identity": report.result is values,
        "sections_copied": report.sections is not sections,
        "sections": list(report.sections),
        "note": report.note,
        "when_extra": report.when,
        "repr": repr(report),
        "location": report.location,
        "head_line": report.head_line,
        "summary": report.count_towards_summary,
    }
    empty: list[object] = []
    empty_report = report_type("test_subject.py", "passed", None, empty)
    observed["empty_report"] = {
        "empty_replaced": empty_report.result is not empty,
        "result": empty_report.result,
        "none_result": report_type("test_subject.py", "passed", None, None).result,
    }
    try:
        report_type("test_subject.py", "passed", None, values, sections=None)
    except TypeError:
        observed["none_sections_rejected"] = True
    else:
        observed["none_sections_rejected"] = False
    report.when = "collect"
    report.sections.extend(
        [
            ("Captured stdout later", "second"),
            ("Captured stderr collect", "error"),
            ("Captured log later", "log-two"),
        ]
    )
    observed["captured_sections"] = {
        "stdout": report.capstdout,
        "stderr": report.capstderr,
        "log": report.caplog,
        "selected": list(report.get_sections("Captured stdout")),
    }
    lazy = report.get_sections("Captured stdout")
    report.sections = [("Captured stdout replacement", "replacement")]
    first = next(lazy)
    report.sections.append(("Captured stdout appended", "appended"))
    previous = report.sections
    report.sections = [("Captured stdout newer", "newer")]
    observed["lazy_sections"] = {
        "first": first,
        "remaining": list(lazy),
        "retained_iterator": previous is not report.sections,
    }
    report.nodeid = "other.py::scope"
    states = []
    for outcome in ("failed", "skipped", "passed"):
        report.outcome = outcome
        states.append([report.outcome, report.passed, report.failed, report.skipped])
    report.result = []
    replacement_sections = [("Captured stdout replacement", "replacement")]
    report.sections = replacement_sections
    observed["report_mutation"] = {
        "nodeid": report.nodeid,
        "fspath": report.fspath,
        "location": report.location,
        "head_line": report.head_line,
        "states": states,
        "repr": repr(report),
        "sections_identity": report.sections is replacement_sections,
        "stdout": report.capstdout,
    }
    report.call = call
    observed["temporary_call"] = report.__dict__.pop("call") is call and not hasattr(report, "call")

    report_reentry: list[object] = []
    report_reference = weakref.ref(report)

    class Representation:
        @staticmethod
        def toterminal(out: object) -> None:
            owner = report_reference()
            owner.result = []
            owner.outcome = "failed"
            report_reentry.append(["terminal", owner.nodeid, owner.failed])
            out.line(" delegated representation ")

        def __str__(self) -> str:
            return "str representation"

        def __del__(self) -> None:
            owner = report_reference()
            if owner is not None:
                owner.result = values
                owner.outcome = "skipped"
                report_reentry.append(["drop", owner.nodeid, owner.skipped])

    class Receiver:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def line(self, value: str) -> None:
            self.lines.append(value)

    report.longrepr = Representation()
    receiver = Receiver()
    report.toterminal(receiver)
    observed["longrepr"] = {"text": report.longreprtext, "lines": receiver.lines}
    report.longrepr = " plain text "
    observed["report_reentry"] = report_reentry
    observed["plain_longrepr"] = report.longreprtext
    report.longrepr = None
    observed["none_longrepr"] = report.longreprtext
    cyclic_report = report_type("test_subject.py", "failed", None, [])
    cyclic_report.result.append(cyclic_report)
    cyclic_report.longrepr = cyclic_report
    cyclic_report.sections.append(("owner", cyclic_report))
    cyclic_report.extra_owner = cyclic_report
    cyclic_report_ref = weakref.ref(cyclic_report)
    del cyclic_report
    gc.collect()
    observed["report_cycle_collected"] = cyclic_report_ref() is None
    return observed


def main(items: list) -> None:
    import pytest  # noqa: PLC0415 - the runtime under test provides the public module

    call_type, report_type = pytest.CallInfo, pytest.CollectReport
    assert len(items) == 1
    assert isinstance(items[0], pytest.Item)
    values = [items[0]]
    observed, call = call_observations(pytest, call_type, values)
    observed.update(report_observations(report_type, values, call))
    observed["actual_item"] = True
    observed["signatures"] = {
        "call_info": parameters(call_type),
        "from_call": parameters(call_type.from_call),
        "collect_report": parameters(report_type),
    }
    Path("api.json").write_text(json.dumps(observed, indent=2, sort_keys=True) + "\n")


def pytest_collection_modifyitems(items: list) -> None:
    main(items)
