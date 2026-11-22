"""Compare native parameter-family collection with pytest."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

pytestmark = requires_native_pytest

SOURCE = """\
import json
from pathlib import Path
import pytest

rows = []

def record(request):
    call = request.node.callspec
    rows.append([request.node.name, list(call.params.items()), list(call.indices.items()),
                 list(call._idlist), [mark.name for mark in call.marks],
                 [scope.value for scope in call._arg2scope.values()]])

@pytest.mark.parametrize("value", range(-6, 8, 3))
def test_range(value, request):
    record(request)

@pytest.mark.parametrize("value", range(10**30, 10**30 + 3))
def test_large(value, request):
    record(request)

@pytest.mark.parametrize("left", range(2))
@pytest.mark.parametrize("right", range(3))
def test_product(left, right, request):
    record(request)

@pytest.mark.parametrize("value", range(3), ids=lambda value: "custom-" + str(value))
def test_custom_id(value, request):
    record(request)

@pytest.mark.parametrize("value", range(0))
def test_empty(value):
    assert False

@pytest.mark.stress
@pytest.mark.parametrize("value", range(30))
def test_stress(value):
    assert False

def test_final():
    Path("rows.json").write_text(json.dumps(rows))
"""


def test_numeric_families_preserve_inventory_and_deselection(tmp_path: Path) -> None:
    outputs = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nmarkers = stress\naddopts = -m 'not stress'\n")
        project.write("test_ranges.py", SOURCE)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "18 passed, 1 skipped, 30 deselected" in output, output
        outputs.append((project.root / "rows.json").read_text())
    assert outputs[0] == outputs[1]


AUDIT_SOURCE = """\
import json
import sys
from pathlib import Path
import pytest
from _pytest import python
from _pytest.scope import Scope

state = {"armed": False, "events": [], "function": None}
original_ids = python.Metafunc._resolve_parameter_set_ids

def changed_ids(self, *args, **kwargs):
    state["events"].append("ids")
    return ["changed-" + value for value in original_ids(self, *args, **kwargs)]

def audit(event, args):
    if (state["armed"] and event == "object.__getattr__"
        and args[0] is state["function"] and args[1] == "__code__"):
        state["armed"] = False
        state["events"].append("audit")
        python.Metafunc._resolve_parameter_set_ids = changed_ids
        state["metafunc"]._calls = [python.CallSpec2(
            params={"prior": 42}, indices={"prior": 0},
            _arg2scope={"prior": Scope.Function}, _idlist=["prior"], marks=[])]

sys.addaudithook(audit)

def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ != "test_range":
        return
    state.update(armed=True, function=metafunc.function, metafunc=metafunc)
    try:
        metafunc.parametrize("value", range(3))
    finally:
        state["armed"] = False
        python.Metafunc._resolve_parameter_set_ids = original_ids

def test_range(value, request):
    assert request.node.callspec.params["prior"] == 42
    state["events"].append(request.node.name)

def test_final():
    Path("events.json").write_text(json.dumps(state["events"]))
"""


def test_numeric_family_continues_after_validation_callback(tmp_path: Path) -> None:
    outputs = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_callback.py", AUDIT_SOURCE)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        outputs.append((project.root / "events.json").read_text())
    assert outputs[0] == outputs[1]


REPLACEMENT_SOURCE = """\
import json
from pathlib import Path
import pytest
from _pytest import python

events = []
original = python.normalize_mark_list

class Replacement:
    def __eq__(self, other):
        events.append("equality")
        return True
    def __call__(self, marks):
        events.append("replacement")
        return original(marks)

replacement = Replacement()

def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ != "test_range":
        return
    python.normalize_mark_list = replacement
    try:
        metafunc.parametrize("value", range(3))
    finally:
        python.normalize_mark_list = original

def test_range(value, request):
    events.append(request.node.name)

def test_final():
    assert "equality" not in events
    assert events.count("replacement") == 3
    Path("replacement.json").write_text(json.dumps(events))
"""


def test_numeric_family_replacement_helper_never_compares_equality(tmp_path: Path) -> None:
    outputs = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_replacement.py", REPLACEMENT_SOURCE)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        outputs.append((project.root / "replacement.json").read_text())
    assert outputs[0] == outputs[1]


TABLE_SOURCE = """\
import operator
import pytest

versions = ["1.0", "1.1", "2.0"]
ops = [operator.lt, operator.le, operator.eq, operator.ne, operator.ge, operator.gt]

class TestVersion:
    @pytest.mark.parametrize(("left", "right", "op"), [
        (left, right, op) for op in ops for left in versions for right in versions
        if op(left, right)
    ])
    def test_comparison_true(self, left, right, op):
        assert op(left, right)

    @pytest.mark.parametrize(("left", "right", "op"), [
        (left, right, op) for op in ops for left in versions for right in versions
        if not op(left, right)
    ])
    def test_comparison_false(self, left, right, op):
        assert not op(left, right)

    @pytest.mark.parametrize(("left", "right"), [("é", "x"), ("é", "x"), ("é0", "x")])
    def test_duplicates(self, left, right):
        pass

    @pytest.mark.parametrize("outer", [1, 2])
    @pytest.mark.parametrize(("left", "right"), [("a", "b"), ("c", "d")])
    def test_stacked(self, outer, left, right):
        assert outer in (1, 2)
"""

TABLE_CONFTEST = """\
import json
from pathlib import Path
import pytest
from _pytest.python import Function

observed = {"inventory": [], "deselected": [], "execution": []}

def value_id(value):
    return getattr(value, "__name__", value)

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(items):
    for item in items:
        assert type(item) is Function
        call = item.callspec
        observed["inventory"].append([
            item.nodeid, item.name,
            [[name, value_id(value)] for name, value in call.params.items()],
            list(call.indices.items()),
            [[name, scope.value] for name, scope in call._arg2scope.items()],
            list(call._idlist), [mark.name for mark in call.marks],
        ])

    result = yield
    selected = {item.nodeid for item in items}
    observed["deselected"] = [
        row[0] for row in observed["inventory"] if row[0] not in selected
    ]
    return result

@pytest.fixture(autouse=True)
def observe_execution(request):
    observed["execution"].append(request.node.nodeid)

def pytest_sessionfinish():
    Path("table.json").write_text(json.dumps(observed, ensure_ascii=True))
"""


@pytest.mark.parametrize("selection", [[], ["-k", "comparison_true or duplicates"]])
def test_tuple_tables_preserve_public_items_and_selection(
    tmp_path: Path, selection: list[str]
) -> None:
    outputs = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", TABLE_CONFTEST)
        project.write("test_table.py", TABLE_SOURCE)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            *selection,
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        outputs.append((project.root / "table.json").read_text())
    assert outputs[0] == outputs[1]


TABLE_CALLBACK_SOURCE = """\
import json
from pathlib import Path
from _pytest import python
from _pytest.scope import Scope

state = {"events": [], "armed": True}
original = python.CallSpec2.setmulti

def changed_setmulti(self, **kwargs):
    state["events"].append(["setmulti", kwargs["param_index"], kwargs["id"]])
    return original(self, **kwargs)

class NamedValue:
    def __init__(self, value):
        self.value = value

    @property
    def __name__(self):
        state["events"].append(["id", self.value])
        if state["armed"]:
            state["armed"] = False
            python.CallSpec2.setmulti = changed_setmulti
            state["metafunc"]._calls = [python.CallSpec2(
                params={"prior": 42}, indices={"prior": 0},
                _arg2scope={"prior": Scope.Function}, _idlist=["prior"], marks=[])]
            # All rows must already be normalized before the ID callback.
            rows.clear()
        return self.value

rows = [(NamedValue("a"), "b"), (NamedValue("c"), "d")]

def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ != "test_table":
        return
    state["metafunc"] = metafunc
    try:
        metafunc.parametrize(("left", "right"), rows)
    finally:
        python.CallSpec2.setmulti = original

def test_table(left, right, request):
    assert request.node.callspec.params["prior"] == 42
    assert (left.value, right) in [("a", "b"), ("c", "d")]
    state["events"].append(["item", request.node.nodeid])

def test_final():
    Path("callback.json").write_text(json.dumps(state["events"]))
"""


def test_tuple_table_continues_after_id_callback_mutation(tmp_path: Path) -> None:
    outputs = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_table.py", TABLE_CALLBACK_SOURCE)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        outputs.append((project.root / "callback.json").read_text())
    assert outputs[0] == outputs[1]


LIFETIME_SOURCE = """\
import json
import sys
from pathlib import Path
from _pytest import python

state = {"events": [], "armed": False}
original_ids = python.Metafunc._resolve_parameter_set_ids
RESTORE_IDS = False

class Finalizable:
    def __init__(self, label):
        self.label = label

    def __del__(self):
        state["events"].append("destroy-" + self.label)

rows = [(Finalizable("rows"), "unused")]

def changed_ids(self, argnames, ids, parametersets, **kwargs):
    state["events"].append("ids")
    self._calls.append(Finalizable("calls"))
    self._calls = []
    state["events"].append("replaced-calls")
    rows.clear()
    parametersets[:] = [python.ParameterSet(("replacement", "row"), marks=[], id=None)]
    state["events"].append("replaced-rows")
    if RESTORE_IDS:
        python.Metafunc._resolve_parameter_set_ids = original_ids
    return ["replacement-row"]

def audit(event, args):
    if (state["armed"] and event == "object.__getattr__"
        and args[0] is state["function"] and args[1] == "__code__"):
        state["armed"] = False
        state["events"].append("audit")
        python.Metafunc._resolve_parameter_set_ids = changed_ids

sys.addaudithook(audit)

def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ != "test_table":
        return
    state.update(armed=True, function=metafunc.function)
    try:
        metafunc.parametrize(("left", "right"), rows)
    finally:
        state["armed"] = False
        python.Metafunc._resolve_parameter_set_ids = original_ids

def test_table(left, right, request):
    assert (left, right) == ("replacement", "row")
    assert state["events"] == ["audit", "ids", "destroy-calls", "replaced-calls",
                               "destroy-rows", "replaced-rows"]
    state["events"].append(request.node.name)
    Path("lifetime.json").write_text(json.dumps(state["events"]))
"""


@pytest.mark.parametrize("restore_helper", [False, True])
def test_parameter_callbacks_release_replaced_containers_and_rows(
    tmp_path: Path, restore_helper: bool
) -> None:
    outputs = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_lifetime.py",
            LIFETIME_SOURCE.replace("RESTORE_IDS = False", f"RESTORE_IDS = {restore_helper}"),
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        outputs.append((project.root / "lifetime.json").read_text())
    assert outputs[0] == outputs[1]


PATCHED_HELPER_SOURCE = """\
import json
from pathlib import Path
from _pytest import python
from _pytest.mark import structures

events = []
KIND = "param"


def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ != "test_value":
        return
    targets = {
        "param": (python.ParameterSet, "param"),
        "extract_from": (python.ParameterSet, "extract_from"),
        "parameter_new": (python.ParameterSet, "__new__"),
        "parameter_init": (python.ParameterSet, "__init__"),
        "parameter_del": (python.ParameterSet, "__del__"),
        "idmaker_del": (python.IdMaker, "__del__"),
        "callspec_del": (python.CallSpec2, "__del__"),
        "parameter_binding": (python, "ParameterSet"),
        "structures_binding": (structures, "ParameterSet"),
        "idmaker_new": (python.IdMaker, "__new__"),
        "idmaker_init": (python.IdMaker, "__init__"),
        "idmaker_binding": (python, "IdMaker"),
    }
    owner, name = targets[KIND]
    absent = object()
    descriptor = vars(owner).get(name, absent)
    original = getattr(owner, name, None)
    if KIND in ("parameter_del", "idmaker_del", "callspec_del"):
        def replacement(self):
            events.append(KIND)
    elif KIND in ("param", "extract_from"):
        def replacement(cls, *args, **kwargs):
            events.append(KIND)
            return original(*args, **kwargs)
        replacement = classmethod(replacement)
    elif KIND == "parameter_new":
        def replacement(cls, *args, **kwargs):
            events.append(KIND)
            return original(cls, *args, **kwargs)
        replacement = staticmethod(replacement)
    elif KIND == "parameter_init":
        def replacement(self, *args, **kwargs):
            events.append(KIND)
    elif KIND == "idmaker_new":
        def replacement(cls, *args, **kwargs):
            events.append(KIND)
            return original(cls)
        replacement = staticmethod(replacement)
    elif KIND == "idmaker_init":
        def replacement(self, *args, **kwargs):
            events.append(KIND)
            return original(self, *args, **kwargs)
    elif KIND == "parameter_binding":
        class replacement:
            @staticmethod
            def _for_parametrize(*args, **kwargs):
                events.append(KIND)
                return original._for_parametrize(*args, **kwargs)
    elif KIND == "structures_binding":
        class replacement(original):
            @classmethod
            def extract_from(cls, *args, **kwargs):
                events.append(KIND)
                return original.extract_from(*args, **kwargs)
    else:
        def replacement(*args, **kwargs):
            events.append(KIND)
            return original(*args, **kwargs)
    setattr(owner, name, replacement)
    try:
        metafunc.parametrize("value", range(3))
    finally:
        if descriptor is absent:
            delattr(owner, name)
        else:
            setattr(owner, name, descriptor)


def test_value(value, request):
    assert events and events[0] == KIND
    events.append([request.node.name, request.node.callspec.params["value"]])


def test_final():
    Path("helpers.json").write_text(json.dumps(events))
"""


@pytest.mark.parametrize(
    "helper",
    [
        "param",
        "extract_from",
        "parameter_new",
        "parameter_init",
        "parameter_del",
        "idmaker_del",
        "callspec_del",
        "parameter_binding",
        "structures_binding",
        "idmaker_new",
        "idmaker_init",
        "idmaker_binding",
    ],
)
def test_parameter_families_preserve_replaced_normalizers_and_id_constructors(
    tmp_path: Path, helper: str
) -> None:
    outputs = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_helpers.py", PATCHED_HELPER_SOURCE.replace('KIND = "param"', f"KIND = {helper!r}")
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        outputs.append((project.root / "helpers.json").read_text())
    assert outputs[0] == outputs[1]


BINDING_SOURCE = """\
import json
import sys
from pathlib import Path
from _pytest import python

state = {"events": [], "armed": False}
original_ids = python.Metafunc._resolve_parameter_set_ids
original_directness = python._resolve_args_directness
original_values = python.ParameterSet.values
original_setmulti = python.CallSpec2.setmulti
original_fixture = python.DirectParamFixtureDef


def changed_fixture(**kwargs):
    state["events"].append(["fixture", kwargs["argname"]])
    return original_fixture(**kwargs)


def changed_session(self):
    state["events"].append("session")
    python.DirectParamFixtureDef = changed_fixture
    return state["session"]


def changed_setmulti(self, **kwargs):
    assert kwargs["nodeid"] == state["initial_nodeid"]
    state["events"].append(["setmulti", kwargs["param_index"]])
    return original_setmulti(self, **kwargs)


def changed_values(self):
    state["events"].append("values")
    python.CallSpec2.setmulti = changed_setmulti
    return original_values.__get__(self, type(self))


def changed_ids(self, *args, **kwargs):
    assert kwargs["nodeid"] == self.definition.nodeid
    assert kwargs["nodeid"].endswith("-validated")
    state["events"].append(["ids", kwargs["nodeid"]])
    ids = original_ids(self, *args, **kwargs)
    definition = self.definition
    if hasattr(definition, "_nodeid"):
        definition._nodeid += "-ids"
    else:
        definition._id = type(definition.id).parse(definition.nodeid + "-ids")
    python.ParameterSet.values = property(changed_values)
    return ids


def changed_directness(argnames, indirect, nodeid):
    assert nodeid == state["metafunc"].definition.nodeid
    assert nodeid.endswith("-validated-ids")
    state["events"].append(["directness", nodeid])
    return original_directness(argnames, indirect, nodeid)


def audit(event, args):
    if (state["armed"] and event == "object.__getattr__"
        and args[0] is state["function"] and args[1] == "__code__"):
        state["armed"] = False
        definition = state["metafunc"].definition
        if hasattr(definition, "_nodeid"):
            definition._nodeid += "-validated"
        else:
            definition._id = type(definition.id).parse(definition.nodeid + "-validated")
        python.Metafunc._resolve_parameter_set_ids = changed_ids
        python._resolve_args_directness = changed_directness
        state["definition_type"].session = property(changed_session)

sys.addaudithook(audit)


def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ != "test_table":
        return
    definition_type = type(metafunc.definition)
    absent = object()
    session_descriptor = vars(definition_type).get("session", absent)
    state.update(armed=True, function=metafunc.function, metafunc=metafunc,
                 initial_nodeid=metafunc.definition.nodeid,
                 definition_type=definition_type, session=metafunc.definition.session)
    try:
        metafunc.parametrize(("left", "right"), [("a", "b"), ("c", "d")])
    finally:
        state["armed"] = False
        python.Metafunc._resolve_parameter_set_ids = original_ids
        python._resolve_args_directness = original_directness
        python.ParameterSet.values = original_values
        python.CallSpec2.setmulti = original_setmulti
        python.DirectParamFixtureDef = original_fixture
        if session_descriptor is absent:
            delattr(definition_type, "session")
        else:
            definition_type.session = session_descriptor


def test_table(left, right, request):
    assert (left, right) in [("a", "b"), ("c", "d")]
    assert [event for event in state["events"] if isinstance(event, list)
            and event[0] == "setmulti"] == [["setmulti", 1]]
    assert [event for event in state["events"] if isinstance(event, list)
            and event[0] == "fixture"] == [["fixture", "right"]]
    state["events"].append(request.node.name)


def test_final():
    Path("binding.json").write_text(json.dumps(state["events"]))
"""


def test_parameter_continuation_preserves_method_binding_and_live_nodeids(tmp_path: Path) -> None:
    outputs = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_binding.py", BINDING_SOURCE)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        outputs.append((project.root / "binding.json").read_text())
    assert outputs[0] == outputs[1]


ITERATOR_WARNING_SOURCE = """\
from pathlib import Path
import pytest


def values(label):
    with Path("consumed.txt").open("a") as stream:
        stream.write(label + "\\n")
    yield 1


@pytest.mark.parametrize(("left", "right"), zip(values("left"), values("right")))
def test_iterator(left, right):
    assert left == right == 1
"""


@pytest.mark.parametrize("action", ["always", "ignore", "error"])
def test_parameter_iterator_warning_uses_canonical_source_and_module_filters(
    tmp_path: Path, action: str
) -> None:
    locations = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_iterator.py", ITERATOR_WARNING_SOURCE)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "-W",
            f"{action}::pytest.PytestRemovedIn10Warning:_pytest.python",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        output = result.stdout + result.stderr
        assert result.returncode == (2 if action == "error" else 0), output
        consumed = project.root / "consumed.txt"
        if action == "error":
            assert "PytestRemovedIn10Warning" in output, output
            assert not consumed.exists()
        else:
            assert consumed.read_text() == "left\nright\n"
        if action == "always":
            source = re.findall(
                r"^\s*(.+/python\.py:\d+): PytestRemovedIn10Warning:", output, re.MULTILINE
            )
            assert len(source) == 1, output
            assert "/_pytest/python.py:" in source[0], output
            locations.append(source)
        elif action == "ignore":
            assert "PytestRemovedIn10Warning" not in output, output
    if action == "always":
        assert locations[0] == locations[1]


CALLING_SOURCE = """\
import inspect
import json
from pathlib import Path
from types import MethodType
from _pytest import python

observed = []


def custom_return(self, *args, **kwargs):
    return ("returned", args[0])


def pytest_generate_tests(metafunc):
    name = metafunc.function.__name__
    if name not in {"test_bound", "test_unbound", "test_keyword_self", "test_return"}:
        return
    unbound = python.Metafunc.parametrize
    bound = metafunc.parametrize
    original = getattr(unbound, "__wrapped__", unbound)
    signature = inspect.signature(original)
    assert inspect.signature(unbound) == signature
    assert inspect.signature(bound) == signature.replace(
        parameters=tuple(signature.parameters.values())[1:])
    assert isinstance(bound, MethodType)
    assert bound.__self__ is metafunc
    assert bound.__func__ is unbound
    assert bound == metafunc.parametrize
    assert unbound.__get__(metafunc, python.Metafunc) == bound
    assert bound.__name__ == unbound.__name__ == "parametrize"
    assert bound.__doc__ == original.__doc__
    if name == "test_return":
        code = original.__code__
        original.__code__ = custom_return.__code__
        try:
            assert bound("unused", []) == ("returned", "unused")
            observed.append("custom-return")
        finally:
            original.__code__ = code
        return
    if name == "test_bound":
        for call in [lambda: unbound(), lambda: unbound(metafunc, "value"),
                     lambda: bound("value", range(2), unknown=True),
                     lambda: bound("value", range(2), original=True)]:
            try:
                call()
            except TypeError as error:
                observed.append(str(error))
            else:
                raise AssertionError("invalid parametrization accepted")
        assert bound("value", range(2)) is None
    elif name == "test_unbound":
        assert unbound(metafunc, "value", range(2)) is None
    else:
        assert unbound(self=metafunc, argnames="value", argvalues=range(2)) is None


def test_bound(value):
    observed.append(["bound", value])


def test_unbound(value):
    observed.append(["unbound", value])


def test_keyword_self(value):
    observed.append(["keyword-self", value])


def test_return():
    pass


def test_final():
    Path("calling.json").write_text(json.dumps(observed))
"""


def test_parameter_dispatch_preserves_binding_signatures_calls_and_fallback_returns(
    tmp_path: Path,
) -> None:
    outputs = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_calling.py", CALLING_SOURCE)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        outputs.append((project.root / "calling.json").read_text())
    assert outputs[0] == outputs[1]


INDEX_SOURCE = """\
import json
import os
from pathlib import Path

def pytest_generate_tests(metafunc):
    metafunc.parametrize(('left', 'right'), [(i, i + 1000) for i in range(300)])
    call = metafunc._calls[257]
    observed = [call.params['left'], call.params['right'],
                call.indices['left'], call.indices['right'],
                call.indices['left'] is call.indices['right']]
    Path('indices.json').write_text(json.dumps(observed))

def test_values(left, right):
    assert right == left + 1000
"""


def test_first_parameter_table_shares_one_index_object_per_row(tmp_path: Path) -> None:
    observations = {}
    for route in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / route)
        project.write("test_indices.py", INDEX_SOURCE)
        completed = (
            project.run_pytest("-q", load_xdist=False)
            if route == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q")
        )
        output = completed.stdout + completed.stderr
        assert completed.returncode == 0, output
        assert "300 passed" in output, output
        observations[route] = json.loads((project.root / "indices.json").read_text())
        if route == "pytest":
            assert observations[route] == [257, 1257, 257, 257, True]
    assert observations["pytest-native"] == observations["pytest"], observations
