"""Specification validation and historic replay preserve pluggy edge behavior."""

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
marker = pytest.hookimpl
specmarker = pytest.hookspec
def rawimpl(**options):
    # Plug-ins can carry pluggy's marker attributes without importing pluggy.
    def decorate(function):
        function.pytest_impl = dict(wrapper=False, hookwrapper=False, optionalhook=False,
                                    tryfirst=False, trylast=False, specname=None, **options)
        return function
    return decorate
def rawspec(**options):
    def decorate(function):
        function.pytest_spec = dict(firstresult=False, historic=False, warn_on_impl=None,
                                    warn_on_impl_args=None, **options)
        return function
    return decorate
register = registry.register
addspecs = registry.add_hookspecs
unregister = registry.unregister
def call(kwargs): return registry.hook.pytest_case(**kwargs)
def historic(kwargs, callback=None):
    return registry.hook.pytest_case.call_historic(callback, kwargs)
def impls(): return registry.hook.pytest_case.get_hookimpls()
def callers(plugin):
    result = registry.get_hookcallers(plugin)
    return None if result is None else [hook.name for hook in result]
def has_spec(name):
    hook = getattr(registry.hook, name, None)
    return hook is not None and hook.has_spec()

def observe(function):
    try: return ["return", function()]
    except BaseException as error:
        return ["raise", type(error).__name__, str(error),
                getattr(error, "plugin", None) is plugin]

class Plugin:
    def __repr__(self): return "plugin"
plugin = Plugin()
"""


def _compare(tmp_path: Path, source: str, case: str) -> None:
    stock, actual = run_manager_probe(tmp_path, _PRELUDE + source, case)
    assert actual == stock


@pytest.mark.parametrize("kind", ["dict", "subclass", "collision"])
def test_first_result_options_preserve_live_lookup_callbacks(tmp_path: Path, kind: str) -> None:
    _compare(
        tmp_path,
        """\
class Options(dict):
    def get(self, *args):
        if args[0] == "firstresult":
            events.append(["get", list(args)])
            if fail:
                raise LookupError("option get")
        return super().get(*args)
class Key:
    def __hash__(self): return hash("firstresult")
    def __eq__(self, other):
        events.append(["eq", type(other).__name__, other])
        if fail:
            raise LookupError("option equality")
        return other == "firstresult"
options = Options() if case == "subclass" else {}
key = Key() if case == "collision" else "firstresult"
fail = False
class Spec:
    def pytest_case(): pass
    pytest_case.pytest_spec = options
class Plugin:
    @marker()
    def pytest_case(self): return "value"
plugin = Plugin()
addspecs(Spec)
register(plugin, "first")
register(Plugin(), "second")
results = []
for present, value in [(False, None), (True, True), (True, False), (True, None)]:
    if present:
        options[key] = value
    events.clear()
    results.append([observe(lambda: call({})), list(events)])
if case != "dict":
    fail = True
    events.clear()
    results.append([observe(lambda: call({})), list(events)])
assert any(entry[1] for entry in results) == (case != "dict")
print(json.dumps(results))
""",
        kind,
    )


@pytest.mark.parametrize("case", ["options", "fields"])
def test_warning_fields_snapshot_options_and_accept_assignment(tmp_path: Path, case: str) -> None:
    _compare(
        tmp_path,
        """\
class Options(dict):
    def get(self, *args):
        events.append(args[0])
        return super().get(*args)
options = Options(warn_on_impl=UserWarning("initial"),
                  warn_on_impl_args={"value": UserWarning("argument initial")})
class Spec:
    def pytest_case(value): pass
    pytest_case.pytest_spec = options
class Plugin:
    @marker()
    def pytest_case(self, value): pass
plugin = Plugin()
addspecs(Spec)
specification = registry.hook.pytest_case.spec
assert events == ["warn_on_impl", "warn_on_impl_args", "historic"]
if case == "options":
    options.update(warn_on_impl=UserWarning("changed"),
                   warn_on_impl_args={"value": UserWarning("argument changed")})
else:
    specification.warn_on_impl = UserWarning("assigned")
    specification.warn_on_impl_args = {"value": UserWarning("argument assigned")}
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    register(plugin, "plugin")
print(json.dumps([events, str(specification.warn_on_impl),
                  {key: str(value) for key, value in specification.warn_on_impl_args.items()},
                  [str(warning.message) for warning in caught]]))
""",
        case,
    )


@pytest.mark.parametrize("case", ["warning_bool", "arguments_bool", "arguments_get"])
def test_warning_callbacks_read_current_public_fields(tmp_path: Path, case: str) -> None:
    _compare(
        tmp_path,
        """\
class Spec:
    @specmarker()
    def pytest_case(first, second): pass
class Plugin:
    @marker()
    def pytest_case(self, first, second): pass
plugin = Plugin()
addspecs(Spec)
specification = registry.hook.pytest_case.spec
class Warning(UserWarning):
    def __bool__(self):
        events.append("warning bool")
        specification.warn_on_impl = UserWarning("replacement")
        return True
class Arguments(dict):
    def __bool__(self):
        events.append("arguments bool")
        if case == "arguments_bool":
            specification.warn_on_impl_args = {"first": UserWarning("first replacement"),
                                               "second": UserWarning("second replacement")}
        return True
    def get(self, key):
        events.append(["get", key])
        specification.warn_on_impl_args = {"second": UserWarning("second replacement")}
        return UserWarning("first original")
if case == "warning_bool":
    specification.warn_on_impl = Warning("original")
else:
    specification.warn_on_impl_args = Arguments()
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    register(plugin, "plugin")
assert events
print(json.dumps([events, [str(warning.message) for warning in caught]]))
""",
        case,
    )


@pytest.mark.parametrize("field", ["warn_on_impl", "warn_on_impl_args"])
def test_warning_fields_release_setter_callbacks(tmp_path: Path, field: str) -> None:
    _compare(
        tmp_path,
        """\
class Spec:
    @specmarker()
    def pytest_case(): pass
addspecs(Spec)
specification = registry.hook.pytest_case.spec
class Reenter:
    def __del__(self):
        try:
            empty = getattr(specification, case) is None
            setattr(specification, case, None)
            events.append(["setter release", empty])
        except BaseException as error:
            events.append(["setter error", type(error).__name__, str(error)])
setattr(specification, case, Reenter())
setattr(specification, case, None)
assert events == [["setter release", True]]
print(json.dumps(events))
""",
        field,
    )


@pytest.mark.parametrize(
    "case", ["both", "both_plain", "both_badarg", "both_historic", "both_unknown", "both_warning"]
)
def test_wrapper_validation_order_matches_stock_pytest(tmp_path: Path, case: str) -> None:
    _compare(
        tmp_path,
        """\
class Spec:
    @specmarker(historic=case == "both_historic",
                warn_on_impl=UserWarning("deprecated") if case == "both_warning" else None)
    def pytest_case(value): pass
class Plugin:
    def __repr__(self): return "plugin"
    if case == "both_plain":
        @marker(wrapper=True, hookwrapper=True)
        def pytest_case(self, value): return value
    elif case == "both_badarg":
        @marker(wrapper=True, hookwrapper=True)
        def pytest_case(self, absent): yield
    else:
        @marker(wrapper=True, hookwrapper=True)
        def pytest_case(self, value): yield
plugin = Plugin()
if case != "both_unknown": addspecs(Spec)
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    result = observe(lambda: register(plugin, "plugin"))
# Registration in vfy validates before it publishes; pluggy keeps a failed plug-in registered.
state = ([registry.get_plugin("plugin") is plugin, callers(plugin),
          [impl.plugin_name for impl in impls()]] if result[0] == "return" else None)
print(json.dumps([result, state,
                  [[type(w.message).__name__, str(w.message),
                    w.filename == __file__, w.lineno == Plugin.pytest_case.__code__.co_firstlineno]
                   for w in caught]]))
""",
        case,
    )


@pytest.mark.parametrize("case", ["impl_metadata", "spec_metadata", "spec_flags", "empty_specname"])
def test_malformed_metadata_matches_stock_pytest(tmp_path: Path, case: str) -> None:
    _compare(
        tmp_path,
        """\
class Spec:
    @rawspec()
    def pytest_case(value): pass
class Plugin:
    def __repr__(self): return "plugin"
    @rawimpl()
    def pytest_case(self, value): return value
plugin = Plugin()
if case == "impl_metadata":
    Plugin.pytest_case.pytest_impl = []
    addspecs(Spec)
    result = observe(lambda: register(plugin, "plugin"))
elif case == "spec_metadata":
    Spec.pytest_case.pytest_spec = []
    result = observe(lambda: addspecs(Spec))
elif case == "spec_flags":
    Spec.pytest_case.pytest_spec.update(firstresult=True, historic=True)
    result = observe(lambda: addspecs(Spec))
else:
    Plugin.pytest_case.pytest_impl["specname"] = ""
    addspecs(Spec)
    result = observe(lambda: register(plugin, "plugin"))
print(json.dumps([result, has_spec("pytest_case"), callers(plugin)]))
""",
        case,
    )


@pytest.mark.parametrize("case", ["unused", "required", "warning_error", "argument_warning_error"])
def test_spec_argument_diagnostics_match_stock_pytest(tmp_path: Path, case: str) -> None:
    _compare(
        tmp_path,
        """\
class Spec:
    @specmarker(warn_on_impl_args={"value": DeprecationWarning("argument warning")})
    def pytest_case(value, optional="default"): pass
class Plugin:
    def __repr__(self): return "plugin"
    if case == "unused":
        @marker()
        def pytest_case(self): return "unused"
    else:
        @marker()
        def pytest_case(self, value): return value
plugin = Plugin()
addspecs(Spec)
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("error" if case == "argument_warning_error" else "always")
    result = observe(lambda: register(plugin, "plugin"))
    if case != "argument_warning_error":
        warnings.simplefilter("error" if case == "warning_error" else "always")
        result = observe(lambda: call({}))
print(json.dumps([result, [[type(w.message).__name__, str(w.message)] for w in caught],
                  None if case == "argument_warning_error" else
                  registry.get_plugin("plugin") is plugin,
                  None if case == "argument_warning_error" else
                  [impl.plugin_name for impl in impls()]]))
""",
        case,
    )


@pytest.mark.parametrize("case", ["live_callback", "replay_callback", "reentrant_history"])
def test_historic_failure_and_reentrant_replay_match_stock_pytest(
    tmp_path: Path, case: str
) -> None:
    _compare(
        tmp_path,
        """\
class Spec:
    @specmarker(historic=True)
    def pytest_case(value): pass
class Plugin:
    def __init__(self, label): self.label = label
    def __repr__(self): return self.label
    @marker()
    def pytest_case(self, value):
        events.append([self.label, value])
        if case == "reentrant_history" and value == "first" and not nested[0]:
            nested[0] = True
            historic({"value": "nested"}, callback)
        return self.label + ":" + value
plugin = Plugin("plugin")
other = Plugin("other")
nested = [False]
failed = [False]
def callback(value):
    events.append(["callback", value])
    if case != "reentrant_history" and not failed[0]:
        failed[0] = True
        raise LookupError("callback failure")
addspecs(Spec)
if case == "live_callback":
    register(plugin, "plugin")
    result = observe(lambda: historic({"value": "first"}, callback))
    followup = observe(lambda: register(other, "other"))
else:
    historic({"value": "first"}, callback)
    result = observe(lambda: register(plugin, "plugin"))
    followup = observe(lambda: register(other, "other"))
print(json.dumps([result, followup, events, callers(plugin), callers(other),
                  [impl.plugin_name for impl in impls()]]))
""",
        case,
    )


def test_blocking_and_hookcaller_membership_match_stock_pytest(tmp_path: Path) -> None:
    _compare(
        tmp_path,
        """\
class Spec:
    @specmarker()
    def pytest_case(value): pass
class Plugin:
    def __repr__(self): return "plugin"
    @marker()
    def pytest_case(self, value): return value
plugin = Plugin()
addspecs(Spec)
register(plugin, "plugin")
events.append(callers(plugin))
registry.set_blocked("plugin")
events.append([registry.is_blocked("plugin"), callers(plugin), call({"value": "v"})])
events.append(register(plugin, "plugin"))
registry.set_blocked("plugin")
events.append([registry.unblock("plugin"), registry.unblock("plugin")])
events.append(register(plugin, "plugin"))
events.append(callers(plugin))
events.append(unregister(name="plugin") is plugin)
events.append(callers(plugin))
print(json.dumps(events))
""",
        "blocked",
    )


@pytest.mark.parametrize("case", ["duplicate", "malformed_later", "descriptor_later"])
def test_specification_failure_keeps_compatible_partial_state(tmp_path: Path, case: str) -> None:
    _compare(
        tmp_path,
        """\
class First:
    @rawspec()
    def pytest_case(value): pass
class Later:
    @rawspec()
    def pytest_aaa(value): pass
    @rawspec()
    def pytest_case(value): pass
    if case == "malformed_later":
        pytest_case.pytest_spec = []
    elif case == "descriptor_later":
        class Broken:
            def __get__(self, instance, owner): raise LookupError("descriptor failure")
        pytest_case = Broken()
if case == "duplicate": addspecs(First)
result = observe(lambda: addspecs(Later))
print(json.dumps([result, has_spec("pytest_aaa"), has_spec("pytest_case")]))
""",
        case,
    )
