"""Adversarial hook dispatch differentials against stock pytest."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.hook_probe import run_manager_probe
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PRELUDE = """\
import json
import warnings

import pytest

events = []
hookimpl = pytest.hookimpl
class Spec:
    @pytest.hookspec
    def pytest_case(value): pass
registry.add_hookspecs(Spec)
def call(): return registry.hook.pytest_case(value="input")

def observe(function):
    try:
        return ["return", function()]
    except BaseException as error:
        return ["raise", type(error).__name__, str(error)]
"""


def _compare(tmp_path: Path, source: str, case: str) -> None:
    stock, actual = run_manager_probe(tmp_path, _PRELUDE + source, case)
    assert actual == stock


@pytest.mark.parametrize("style", ["wrapper", "hookwrapper"])
@pytest.mark.parametrize(
    "failure", ["entry", "no_yield", "second_yield", "close", "stop", "teardown", "warning_error"]
)
def test_wrapper_failures_match_pluggy(tmp_path: Path, style: str, failure: str) -> None:
    _compare(
        tmp_path,
        """\
style, failure = case.split(":")
class Body:
    @hookimpl()
    def pytest_case(self, value):
        events.append("body")
        if failure == "stop": raise StopIteration("body stop")
        return value
class Inner:
    @hookimpl(**{style: True})
    def pytest_case(self):
        events.append("inner enter")
        if failure == "entry": raise LookupError("entry failure")
        if failure == "no_yield": return
        try:
            yield
            events.append("inner resume")
            if failure in {"teardown", "warning_error"}: raise LookupError("teardown failure")
            if failure in {"second_yield", "close"}: yield
        finally:
            events.append("inner finally")
            if failure == "close": raise OSError("close failure")
class Outer:
    @hookimpl(wrapper=True, tryfirst=True)
    def pytest_case(self):
        events.append("outer enter")
        try:
            return (yield)
        except BaseException as error:
            events.append(["outer error", type(error).__name__, str(error)])
            raise
        finally:
            events.append("outer finally")
registry.register(Body(), "body")
registry.register(Inner(), "inner")
registry.register(Outer(), "outer")
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("error" if failure == "warning_error" else "always")
    outcome = observe(call)
print(json.dumps([outcome, events,
                  [[type(w.message).__name__, str(w.message)] for w in caught]]))
""",
        f"{style}:{failure}",
    )


@pytest.mark.parametrize("mutation", ["traceback", "result", "exception", "clear_exception"])
def test_legacy_result_mutation_matches_pluggy(tmp_path: Path, mutation: str) -> None:
    _compare(
        tmp_path,
        """\
original = ValueError("body failure")
replacement = KeyError("replacement")
class Body:
    @hookimpl()
    def pytest_case(self, value):
        raise original
class Legacy:
    @hookimpl(hookwrapper=True)
    def pytest_case(self):
        result = yield
        saved = result.excinfo[2]
        events.append([result.exception is original, saved is original.__traceback__])
        original.__traceback__ = None
        for index in range(2):
            try:
                result.get_result()
            except ValueError as error:
                traceback = error.__traceback__
                while traceback is not None and traceback is not saved:
                    traceback = traceback.tb_next
                events.append([error is original, traceback is saved,
                               result.excinfo[2] is saved])
        if case == "result":
            value = ["forced"]
            result.force_result(value)
            events.append([result.exception, result.excinfo, result.get_result() is value])
        elif case == "exception":
            result.force_exception(replacement)
            events.append([result.exception is replacement, result.excinfo[2] is None])
        elif case == "clear_exception":
            result.force_exception(None)
            events.append([result.exception, result.excinfo, result.get_result()])
registry.register(Body(), "body")
registry.register(Legacy(), "legacy")
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    outcome = observe(call)
print(json.dumps([outcome, events,
                  [[type(w.message).__name__, str(w.message)] for w in caught]]))
""",
        mutation,
    )


def test_registration_during_dispatch_uses_snapshot(tmp_path: Path) -> None:
    _compare(
        tmp_path,
        """\
class Plain:
    def __init__(self, name): self.name = name
    def __del__(self): events.append(["released", self.name])
    @hookimpl()
    def pytest_case(self, value):
        events.append(self.name)
        return self.name
old = Plain("old")
new = Plain("new")
class Mutator:
    @hookimpl(wrapper=True, tryfirst=True)
    def pytest_case(self):
        events.append("enter")
        if registry.get_plugin("old") is not None:
            events.append(registry.unregister(name="old") is not None)
            events.append(registry.register(new, "new"))
        result = yield
        events.append(["exit", result])
        return result
registry.register(old, "old")
del old
registry.register(Mutator(), "mutator")
print(json.dumps([call(), call(), events]))
""",
        "snapshot",
    )


@pytest.mark.parametrize(
    "operation", ["duplicate_name", "duplicate_plugin", "missing", "unknown", "conflict"]
)
def test_registration_errors_match_pluggy(tmp_path: Path, operation: str) -> None:
    _compare(
        tmp_path,
        """\
# The registry's plug-in listing follows the first line and differs by engine configuration.
def observe(function):
    try:
        return ["return", function()]
    except BaseException as error:
        return ["raise", type(error).__name__, str(error).partition("\\n")[0]]
class Plugin:
    def __init__(self, label): self.label = label
    def __repr__(self): return self.label
    @hookimpl()
    def pytest_case(self): return self.label
one, two = Plugin("one"), Plugin("two")
registry.register(one, "one")
registry.register(two, "two")
if case == "duplicate_name":
    outcome = observe(lambda: registry.register(Plugin("three"), "one"))
elif case == "duplicate_plugin":
    outcome = observe(lambda: registry.register(one, "renamed"))
elif case == "missing":
    outcome = observe(lambda: registry.unregister())
elif case == "unknown":
    outcome = observe(lambda: registry.unregister(plugin=Plugin("unknown")))
else:
    outcome = observe(lambda: registry.unregister(plugin=one, name="two").label)
print(json.dumps([outcome, registry.get_plugin("one") is one,
                  registry.get_plugin("two") is two, call()]))
""",
        operation,
    )


def test_each_legacy_wrapper_receives_its_own_outcome_and_raised_traceback(tmp_path: Path) -> None:
    _compare(
        tmp_path,
        """\
saved = []
class Plugin:
    @hookimpl(hookwrapper=True, specname="pytest_case", tryfirst=True)
    def pytest_z_outer(self):
        result = yield
        events.append([result is saved[0], result.excinfo[2] is None])
        result.force_result("recovered")
    @hookimpl(hookwrapper=True, specname="pytest_case", tryfirst=True)
    def pytest_a_inner(self):
        result = yield
        saved.append(result)
        result.force_exception(ValueError("forced"))
registry.register(Plugin(), "legacy")
print(json.dumps([observe(call), events]))
""",
        "legacy_identity",
    )


@pytest.mark.parametrize("returned", ["none", "tuple", "object"])
def test_nested_wrapper_return_values_and_lifetimes_match_pluggy(
    tmp_path: Path, returned: str
) -> None:
    _compare(
        tmp_path,
        """\
class Token:
    def __repr__(self): return "token"
    def __del__(self): events.append("token released")
class Body:
    @hookimpl()
    def pytest_case(self, value): return value
class Inner:
    @hookimpl(wrapper=True)
    def pytest_case(self):
        received = yield
        events.append(["inner", received])
        if case == "none": return None
        if case == "tuple": return ("one", "two")
        return Token()
class Outer:
    @hookimpl(wrapper=True, tryfirst=True)
    def pytest_case(self):
        value = yield
        events.append(["outer", repr(value)])
        return "outer result"
registry.register(Body(), "body")
registry.register(Inner(), "inner")
registry.register(Outer(), "outer")
result = call()
assert result == "outer result"
assert events[:2] == [["inner", ["input"]], ["outer", {
    "none": "None", "tuple": "('one', 'two')", "object": "token"
}[case]]]
if case == "object": assert "token released" in events
print(json.dumps([result, events]))
""",
        returned,
    )


def test_custom_wrapper_send_uses_stop_iteration_value_after_type_mutation(tmp_path: Path) -> None:
    _compare(
        tmp_path,
        """\
import types
class Completion(StopIteration):
    @property
    def value(self):
        events.append("read value")
        return "custom value"
class Iterator:
    def __iter__(self): return self
    def __next__(self):
        events.append("enter")
        return None
    def send(self, value):
        events.append(["send", value])
        raise Completion("original value")
class Body:
    @hookimpl()
    def pytest_case(self, value): return value
class Wrapper:
    @hookimpl(wrapper=True)
    def pytest_case(self):
        yield
def replacement(self): return Iterator()
registry.register(Body(), "body")
registry.register(Wrapper(), "wrapper")
# The admitted function is still the same callable, but returns a custom iterator.
Wrapper.pytest_case.__code__ = replacement.__code__
original = types.GeneratorType
types.GeneratorType = Iterator
try:
    result = call()
finally:
    types.GeneratorType = original
assert result == "custom value"
assert events == ["enter", ["send", ["input"]], "read value"]
print(json.dumps([result, events]))
""",
        "custom_iterator",
    )
