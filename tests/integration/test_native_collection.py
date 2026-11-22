"""Public native collection and deselection contracts against stock pytest."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="native pytest lifecycle tests require Unix",
)


COLLECTION_SOURCE = """\
from pathlib import Path

import pytest


def record(request):
    with Path("collection-order.txt").open("a", encoding="utf-8") as stream:
        stream.write(request.node.nodeid + "\\n")


@pytest.fixture(scope="module", params=["module-a", "module-b"])
def module_resource(request):
    return request.param


@pytest.mark.parametrize("value", [1, 2])
def test_function_scope(module_resource, value, request):
    assert module_resource
    record(request)


def test_module_scope(module_resource, request):
    assert module_resource
    record(request)


@pytest.fixture(scope="class", params=["class-a", "class-b"])
def class_resource(request):
    return request.param


class TestClassScope:
    def test_one(self, class_resource, request):
        assert class_resource
        record(request)

    def test_two(self, class_resource, request):
        assert class_resource
        record(request)
"""


DESELECTION_SOURCE = """\
from pathlib import Path

import pytest


def record(value):
    with Path("selected.txt").open("a", encoding="utf-8") as stream:
        stream.write(value + "\\n")


@pytest.mark.hidden
@pytest.mark.parametrize("value", range(4))
def test_hidden(value):
    record(f"hidden-{value}")


@pytest.mark.hidden
@pytest.mark.parametrize("value", range(5000))
def test_large_hidden(value):
    record(f"large-{value}")


@pytest.mark.parametrize("value", range(3))
def test_visible(value):
    record(f"visible-{value}")
"""


COLLECTION_HOOK_SOURCE = """\
from pathlib import Path


def pytest_collection_modifyitems(items):
    Path("collection-hook-items.txt").write_text(
        "\\n".join(item.nodeid for item in items), encoding="utf-8"
    )
    items[:] = [item for item in items if not item.nodeid.endswith("[2]")]
"""


PARAMETER_SELECTION_SOURCE = """\
from pathlib import Path

import pytest


@pytest.mark.parametrize("value", [1, 2, 3])
def test_parameter(value):
    with Path("selected-parameters.txt").open("a", encoding="utf-8") as stream:
        stream.write(f"{value}\\n")
"""


INTERRUPT_SOURCE = """\
from pathlib import Path

import pytest


@pytest.fixture
def resource(request):
    request.addfinalizer(lambda: Path("finalized.txt").write_text("done", encoding="utf-8"))
    yield
    raise KeyboardInterrupt()


def test_first(resource):
    pass


def test_second():
    Path("second.txt").write_text("ran", encoding="utf-8")
"""


def _run_native_or_stock(
    project: VfyProject, engine: str, *arguments: str
) -> tuple[CommandResult, Path | None]:
    result_path = project.root / "native-result.json" if engine == "pytest-native" else None
    result = project.run_vfy(
        "test",
        f"--engine={engine}",
        "--workers=1",
        "--no-cov",
        *arguments,
        env={
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_NATIVE_RESULT": str(result_path) if result_path is not None else None,
        },
    )
    return result, result_path


def test_parametrized_class_setup_and_requests_share_each_case_instance(tmp_path: Path) -> None:
    source = """import warnings
import pytest
first_case_finished = False

class TestInstances:
    def __getattribute__(self, name):
        if name == 'test_case' and first_case_finished:
            warnings.warn('callable preparation sentinel', UserWarning)
        return object.__getattribute__(self, name)

    def setup_method(self):
        assert not hasattr(self, 'seen')
        self.ready = True

    @pytest.fixture
    def resource(self, request):
        assert request.instance is self
        assert self.ready
        return self

    @pytest.mark.parametrize('value', [1, 2])
    @pytest.mark.filterwarnings('ignore:callable preparation sentinel')
    def test_case(self, resource, request, value):
        global first_case_finished
        assert request.instance is self is resource
        self.seen = value
        first_case_finished = True
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_instances.py", source)
        result, _ = _run_native_or_stock(project, engine, "-q")
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "2 passed" in output, output
        assert "UserWarning: callable preparation sentinel" not in output, output


@pytest.mark.parametrize("field", ["params", "indices", "_arg2scope"])
def test_parameter_requests_preserve_custom_callspec_mappings(tmp_path: Path, field: str) -> None:
    source = """import pytest
class Parameters(dict):
    def __getitem__(self, name):
        value = super().__getitem__(name)
        return value + 40 if FIELD == 'params' else value

def pytest_generate_tests(metafunc):
    metafunc.parametrize('value', [1, 2])
    for callspec in metafunc._calls:
        object.__setattr__(callspec, FIELD, Parameters(getattr(callspec, FIELD)))

def test_case(value, request):
    assert isinstance(getattr(request.node.callspec, FIELD), Parameters)
    assert value == request.node.callspec.params['value']
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_parameters.py", f"FIELD = {field!r}\n" + source)
        result, _ = _run_native_or_stock(project, engine, "-q")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 passed" in result.stdout, result.stdout + result.stderr


def test_native_collection_order_matches_stock(tmp_path: Path) -> None:
    observations: dict[str, tuple[int, list[str]]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_collection.py", COLLECTION_SOURCE)
        result, result_path = _run_native_or_stock(project, engine, "-q")
        assert result.returncode == 0, result.stdout + result.stderr
        order = (project.root / "collection-order.txt").read_text(encoding="utf-8").splitlines()
        assert len(order) == 10
        if result_path is not None:
            record = json.loads(result_path.read_text(encoding="utf-8"))
            assert record["engine"] == "pytest-runner-native-v1"
            assert [case["nodeid"] for case in record["cases"]] == order
        observations[engine] = (result.returncode, order)

    assert observations["pytest-native"] == observations["pytest"]


def test_native_custom_collector_preserves_generated_callable(tmp_path: Path) -> None:
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_probe.py", "def test_pass():\n    pass\n")
        project.write(
            "conftest.py",
            "import pytest\n"
            "from _pytest.python import Function, Module\n"
            "\n"
            "def fail():\n"
            "    assert False, 'custom collector assertion'\n"
            "\n"
            "class CustomModule(Module):\n"
            "    def _genfunctions(self, name, funcobj):\n"
            "        yield Function.from_parent(self, name=name, callobj=fail)\n"
            "\n"
            "@pytest.hookimpl(wrapper=True)\n"
            "def pytest_collect_file(file_path, parent):\n"
            "    result = yield\n"
            "    if file_path.name == 'test_probe.py':\n"
            "        return [CustomModule.from_parent(parent, path=file_path)]\n"
            "    return result\n",
        )

        result, _result_path = _run_native_or_stock(project, engine, "-q")

        observation = (
            result.returncode,
            "AssertionError: custom collector assertion" in result.stdout,
        )
        assert observation == (1, True), result.stdout + result.stderr


def test_native_collection_hook_receives_each_parameter_case(tmp_path: Path) -> None:
    observations: dict[str, tuple[list[str], list[str]]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", COLLECTION_HOOK_SOURCE)
        project.write("test_parameter_selection.py", PARAMETER_SELECTION_SOURCE)

        result, _result_path = _run_native_or_stock(project, engine, "-q")

        assert result.returncode == 0, result.stdout + result.stderr
        hook_items = (
            (project.root / "collection-hook-items.txt").read_text(encoding="utf-8").splitlines()
        )
        selected = (
            (project.root / "selected-parameters.txt").read_text(encoding="utf-8").splitlines()
        )
        observations[engine] = (hook_items, selected)

    expected_items = [
        "test_parameter_selection.py::test_parameter[1]",
        "test_parameter_selection.py::test_parameter[2]",
        "test_parameter_selection.py::test_parameter[3]",
    ]
    expected = (expected_items, ["1", "3"])
    assert observations["pytest"] == expected
    assert observations["pytest-native"] == observations["pytest"]


def test_custom_module_collection_receives_parameter_items(tmp_path: Path) -> None:
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """import pytest
from _pytest.python import Module
class CustomModule(Module):
    def collect(self):
        items = list(super().collect())
        for item in items:
            if item.callspec.params['value'] == 1:
                item.add_marker(pytest.mark.skip(reason='custom collection skip'))
        return items
@pytest.hookimpl(wrapper=True)
def pytest_collect_file(file_path, parent):
    result = yield
    if file_path.name == 'test_case.py':
        return [CustomModule.from_parent(parent, path=file_path)]
    return result
""",
        )
        project.write(
            "test_case.py",
            """import pytest
@pytest.mark.parametrize('value', [0, 1])
def test_case(value):
    assert value == 0
""",
        )
        result, _ = _run_native_or_stock(project, engine, "-q", "-rs")
        transcript = result.stdout + result.stderr
        assert result.returncode == 0, transcript
        assert "1 passed, 1 skipped" in transcript, transcript
        assert "custom collection skip" in transcript, transcript


@pytest.mark.parametrize("marker", ["skip", "xfail"])
def test_runtime_markers_reach_later_parameter_cases(tmp_path: Path, marker: str) -> None:
    marker_options = ", strict=True" if marker == "xfail" else ""
    source = f"""import pytest
def test_prepare(request):
    for item in request.session.items:
        if item.originalname == 'test_case':
            item.add_marker(pytest.mark.{marker}(reason='runtime marker'{marker_options}))
@pytest.mark.parametrize('value', [0, 1])
def test_case(value):
    assert False, 'marked case must not pass'
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result, _ = _run_native_or_stock(project, engine, "-q", "-rsx")
        transcript = result.stdout + result.stderr
        assert result.returncode == 0, transcript
        outcome = "skipped" if marker == "skip" else "xfailed"
        assert f"1 passed, 2 {outcome}" in transcript, transcript
        assert "runtime marker" in transcript, transcript


@pytest.mark.parametrize("marker", ["skip", "xfail"])
@pytest.mark.parametrize("append", [True, False], ids=["append", "prepend"])
def test_runtime_marker_order_preserves_parameter_marker_precedence(
    tmp_path: Path, marker: str, append: bool
) -> None:
    options = ", strict=True" if marker == "xfail" else ""
    source = f"""import pytest
def test_prepare(request):
    for item in request.session.items:
        if item.originalname == 'test_case':
            item.add_marker(pytest.mark.{marker}(reason='runtime marker'{options}), append={append})
def pytest_generate_tests(metafunc):
    if metafunc.definition.name == 'test_case':
        metafunc.parametrize('value', [
            pytest.param(0, marks=pytest.mark.{marker}(reason='parameter marker'{options})),
            pytest.param(1, marks=pytest.mark.{marker}(reason='parameter marker'{options})),
        ])
def test_case(value):
    assert False, 'marked case must not pass'
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result, _ = _run_native_or_stock(project, engine, "-q", "--junitxml=result.xml")
        transcript = result.stdout + result.stderr
        assert result.returncode == 0, transcript
        observed[engine] = [
            skipped.get("message")
            for skipped in ET.parse(project.root / "result.xml").iter("skipped")
        ]
    reason = "parameter marker" if append else "runtime marker"
    assert observed == {"pytest": [reason, reason], "pytest-native": [reason, reason]}


def test_native_selected_parameter_matches_stock_nodeid(tmp_path: Path) -> None:
    observations: dict[str, list[str]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_parameter_selection.py", PARAMETER_SELECTION_SOURCE)

        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-replay",
            "--no-cov",
            "-q",
            "test_parameter_selection.py::test_parameter[2]",
        )

        result.expect(0)
        selected_values = (
            (project.root / "selected-parameters.txt").read_text(encoding="utf-8").splitlines()
        )
        observations[engine] = selected_values

    assert observations == {"pytest": ["2"], "pytest-native": ["2"]}


def test_native_compact_deselection_reports_expanded_counts(tmp_path: Path) -> None:
    observations: dict[str, tuple[int, str, list[str]]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_selection.py", DESELECTION_SOURCE)
        result, result_path = _run_native_or_stock(project, engine, "-q", "-m", "not hidden")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "3 passed" in result.stdout
        assert "5004 deselected" in result.stdout
        selected = (project.root / "selected.txt").read_text(encoding="utf-8").splitlines()
        assert selected == ["visible-0", "visible-1", "visible-2"]
        if result_path is not None:
            record = json.loads(result_path.read_text(encoding="utf-8"))
            assert record["engine"] == "pytest-runner-native-v1"
            assert record["counts"]["passed"] == 3
        observations[engine] = (result.returncode, result.stdout, selected)

    assert observations["pytest-native"][0] == observations["pytest"][0]
    assert observations["pytest-native"][2] == observations["pytest"][2]


def test_native_fixture_interrupt_runs_finalizers_and_stops_later_tests(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_interrupt.py", INTERRUPT_SOURCE)
        result, _result_path = _run_native_or_stock(project, engine, "-q")
        observations[engine] = {
            "returncode": result.returncode,
            "interrupted": "KeyboardInterrupt" in result.stdout + result.stderr,
            "finalized": (project.root / "finalized.txt").read_text(encoding="utf-8")
            if (project.root / "finalized.txt").is_file()
            else None,
            "second_ran": (project.root / "second.txt").exists(),
        }

    assert observations["pytest"] == {
        "returncode": 2,
        "interrupted": True,
        "finalized": "done",
        "second_ran": False,
    }
    assert observations["pytest-native"] == observations["pytest"]


@pytest.mark.parametrize("access", ["body", "transitive", "autouse", "generation"])
def test_public_session_inventory_preserves_future_parameter_mutation(
    tmp_path: Path, access: str
) -> None:
    observers = {
        "body": """def test_observe(request):
    inspect_inventory(request.session, request.node)
""",
        "transitive": """@pytest.fixture
def session_pair(request):
    return request.node.session, request.node

@pytest.fixture
def observe_inventory(session_pair):
    inspect_inventory(*session_pair)

@pytest.mark.usefixtures('observe_inventory')
def test_observe():
    pass
""",
        "autouse": """@pytest.fixture
def session_pair(request):
    return request.node.session, request.node

@pytest.fixture(autouse=True)
def observe_inventory(session_pair):
    session, current = session_pair
    if current.name == 'test_observe':
        inspect_inventory(session, current)

def test_observe():
    pass
""",
        "generation": """retained = None

def pytest_generate_tests(metafunc):
    global retained
    retained = metafunc.definition.session

def test_observe():
    inspect_inventory(retained, retained.items[0])
""",
    }
    source = """import json
from pathlib import Path
import pytest

seen = []

def inspect_inventory(session, current):
    items = session.items
    inventory = [(item.nodeid, getattr(item, 'callspec', None).params
                  if hasattr(item, 'callspec') else None) for item in items]
    Path('inventory.json').write_text(json.dumps({
        'items': inventory, 'current_is_first': items[0] is current,
    }))
    items[1].add_marker(pytest.mark.skip(reason='skip value one'))
    items[2].callspec.params['value'] = 10
    assert current is items[0]
    assert len(items) == 4
    assert items[1].callspec.params == {'value': 1}

OBSERVERS

@pytest.mark.parametrize('value', [1, 0, 2])
def test_future(value):
    seen.append(value)
    Path('body.json').write_text(json.dumps(seen))
""".replace("OBSERVERS", observers[access])
    inventories: dict[str, object] = {}
    outcomes: dict[str, list[tuple[str | None, list[tuple[str, str | None]]]]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_inventory.py", source)
        result, _ = _run_native_or_stock(project, engine, "-q", "--junitxml=result.xml")
        project.write("stdout.txt", result.stdout)
        project.write("stderr.txt", result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "3 passed, 1 skipped" in result.stdout
        inventories[engine] = json.loads((project.root / "inventory.json").read_text())
        assert json.loads((project.root / "body.json").read_text()) == [10, 2]
        outcomes[engine] = [
            (case.get("name"), [(child.tag, child.get("message")) for child in case])
            for case in ET.parse(project.root / "result.xml").iter("testcase")
        ]
    expected = {
        "items": [
            ["test_inventory.py::test_observe", None],
            ["test_inventory.py::test_future[1]", {"value": 1}],
            ["test_inventory.py::test_future[0]", {"value": 0}],
            ["test_inventory.py::test_future[2]", {"value": 2}],
        ],
        "current_is_first": True,
    }
    assert inventories["pytest-native"] == inventories["pytest"] == expected
    assert (
        outcomes["pytest-native"]
        == outcomes["pytest"]
        == [
            ("test_observe", []),
            ("test_future[1]", [("skipped", "skip value one")]),
            ("test_future[0]", []),
            ("test_future[2]", []),
        ]
    )


def test_parameter_request_node_is_the_exact_session_item(tmp_path: Path) -> None:
    source = """import json
from pathlib import Path
import pytest

observed = []

@pytest.mark.parametrize('value', [1, 0, 2])
def test_future(value, request):
    items = request.session.items
    snapshot = {
        'value': value,
        'nodeids': [item.nodeid for item in items],
        'params': [item.callspec.params if hasattr(item, 'callspec') else None for item in items],
        'node_is_collected': any(item is request.node for item in items),
        'same_session': request.session is request.node.session,
    }
    observed.append(snapshot)
    Path('identity.json').write_text(json.dumps(observed))
    assert snapshot['node_is_collected']
    assert snapshot['same_session']
    assert len(items) == 3
"""
    observations: dict[str, object] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_inventory.py", source)
        result, _ = _run_native_or_stock(project, engine, "-q")
        project.write("stdout.txt", result.stdout)
        project.write("stderr.txt", result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        observations[engine] = json.loads((project.root / "identity.json").read_text())
    expected = [
        {
            "value": value,
            "nodeids": [f"test_inventory.py::test_future[{parameter}]" for parameter in [1, 0, 2]],
            "params": [{"value": parameter} for parameter in [1, 0, 2]],
            "node_is_collected": True,
            "same_session": True,
        }
        for value in [1, 0, 2]
    ]
    assert observations["pytest-native"] == observations["pytest"] == expected


def test_sessionfinish_observes_complete_parameter_inventory(tmp_path: Path) -> None:
    conftest = """import json
from pathlib import Path
import pytest

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session):
    Path('finish.json').write_text(json.dumps([
        {'nodeid': item.nodeid, 'params': item.callspec.params,
         'request_cleared': item._request is False, 'funcargs_cleared': item.funcargs is None}
        for item in session.items
    ]))
"""
    source = """import pytest

@pytest.mark.parametrize('value', [1, 0, 2])
def test_future(value):
    pass
"""
    observations: dict[str, object] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", conftest)
        project.write("test_inventory.py", source)
        result, _ = _run_native_or_stock(project, engine, "-q")
        project.write("stdout.txt", result.stdout)
        project.write("stderr.txt", result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        observations[engine] = json.loads((project.root / "finish.json").read_text())
    expected = [
        {
            "nodeid": f"test_inventory.py::test_future[{value}]",
            "params": {"value": value},
            "request_cleared": True,
            "funcargs_cleared": True,
        }
        for value in [1, 0, 2]
    ]
    assert observations["pytest-native"] == observations["pytest"] == expected


def test_later_module_generation_observes_collected_items_and_frozen_metadata(
    tmp_path: Path,
) -> None:
    source_a = """import json
from pathlib import Path
import pytest

token = object()
before_items = []
seen = []

@pytest.mark.initial_marker('early', token)
@pytest.mark.parametrize('value', [1, 2])
def test_case(value):
    item = next(item for item in before_items
                if hasattr(item, 'callspec') and item.callspec.params['value'] == value)
    observation = {'value': value, 'body': 'original',
                   'token_retained': item.get_closest_marker('initial_marker').args[1] is token,
                   'mark_metadata_retained': item.keywords['pytestmark'] is original_marks,
                   'new_flag_absent': 'new_flag' not in item.keywords,
                   'initial_marker': item.get_closest_marker('initial_marker').args[0],
                   'late_skip_absent': item.get_closest_marker('skip') is None}
    seen.append(observation)
    Path('bodies.json').write_text(json.dumps(seen))
    assert observation == {'value': value, 'body': 'original', 'token_retained': True,
                           'mark_metadata_retained': True,
                           'new_flag_absent': True, 'initial_marker': 'early',
                           'late_skip_absent': True}

original_marks = test_case.pytestmark
"""
    source_b = """import json
from pathlib import Path
import pytest
import test_a

observed = []

def pytest_generate_tests(metafunc):
    items = metafunc.definition.session.items
    inventory = [(item.nodeid, item.callspec.params if hasattr(item, 'callspec') else None)
                 for item in items]
    observed.append(inventory)
    Path('generation.json').write_text(json.dumps(observed))
    test_a.before_items = list(items)
    original = test_a.test_case
    original.pytestmark = [pytest.mark.skip(reason='late function mark')]
    original.token = object()
    original.new_flag = 'late'
    def replacement(value):
        raise AssertionError('replacement callable ran')
    test_a.test_case = replacement

def test_observe():
    assert observed == [[('test_a.py::test_case[1]', {'value': 1}),
                         ('test_a.py::test_case[2]', {'value': 2})]]
"""
    observations: dict[str, tuple[object, object]] = {}
    outcomes: dict[str, list[tuple[str | None, list[str]]]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nmarkers=\n    initial_marker: original marker\n")
        project.write("test_a.py", source_a)
        project.write("test_b.py", source_b)
        result, _ = _run_native_or_stock(project, engine, "-q", "--junitxml=result.xml")
        project.write("stdout.txt", result.stdout)
        project.write("stderr.txt", result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "3 passed" in result.stdout
        observations[engine] = (
            json.loads((project.root / "generation.json").read_text()),
            json.loads((project.root / "bodies.json").read_text()),
        )
        outcomes[engine] = [
            (case.get("name"), [child.tag for child in case])
            for case in ET.parse(project.root / "result.xml").iter("testcase")
        ]
    expected_generation = [
        [
            ["test_a.py::test_case[1]", {"value": 1}],
            ["test_a.py::test_case[2]", {"value": 2}],
        ]
    ]
    expected_bodies = [
        {
            "value": value,
            "body": "original",
            "token_retained": True,
            "mark_metadata_retained": True,
            "new_flag_absent": True,
            "initial_marker": "early",
            "late_skip_absent": True,
        }
        for value in [1, 2]
    ]
    assert (
        observations["pytest-native"]
        == observations["pytest"]
        == (expected_generation, expected_bodies)
    )
    assert (
        outcomes["pytest-native"]
        == outcomes["pytest"]
        == [
            ("test_case[1]", []),
            ("test_case[2]", []),
            ("test_observe", []),
        ]
    )


def test_custom_marker_keywords_do_not_receive_unrelated_id_lookups(tmp_path: Path) -> None:
    source = """import json
from pathlib import Path
import pytest

lookups = []

class CustomKeywords(dict):
    def get(self, key, default=None):
        lookups.append(key)
        if key == 'ids':
            raise AssertionError('unrelated marker ids callback')
        return super().get(key, default)

def test_case():
    Path('lookups.json').write_text(json.dumps(lookups))

test_case.pytestmark = [pytest.Mark('skipif', (False,), CustomKeywords(reason='unused'))]
"""
    observations: dict[str, list[str]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result, _ = _run_native_or_stock(project, engine, "-q")
        project.write("stdout.txt", result.stdout)
        project.write("stderr.txt", result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
        observations[engine] = json.loads((project.root / "lookups.json").read_text())
    assert observations["pytest-native"] == observations["pytest"]
    assert "ids" not in observations["pytest-native"]


@pytest.mark.parametrize("mapping", ["_arg2scope", "indices"])
def test_future_parameter_lookup_matches_the_pytest_version(tmp_path: Path, mapping: str) -> None:
    source = f"""import json
from pathlib import Path
import pytest

@pytest.fixture(scope='module')
def value(request):
    return {{'value': request.param, 'scope': request.scope}}

def test_mutate(request):
    del request.session.items[1].callspec.{mapping}['value']

@pytest.mark.parametrize('value', [1], indirect=True, scope='function')
def test_future(value):
    Path('body.json').write_text(json.dumps(value))
"""
    expected_status = 0 if mapping == "_arg2scope" and pytest.__version__.startswith("8.0.") else 1
    observations: dict[str, object] = {}
    outcomes: dict[str, list[tuple[str | None, list[tuple[str, str | None]]]]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_cases.py", source)
        result, _ = _run_native_or_stock(project, engine, "-q", "--junitxml=result.xml")
        project.write("stdout.txt", result.stdout)
        project.write("stderr.txt", result.stderr)
        assert result.returncode == expected_status, result.stdout + result.stderr
        body = project.root / "body.json"
        observations[engine] = json.loads(body.read_text()) if body.exists() else None
        outcomes[engine] = [
            (case.get("name"), [(child.tag, child.get("message")) for child in case])
            for case in ET.parse(project.root / "result.xml").iter("testcase")
        ]
    assert (
        observations["pytest-native"]
        == observations["pytest"]
        == ({"value": 1, "scope": "module"} if expected_status == 0 else None)
    )
    assert outcomes["pytest-native"] == outcomes["pytest"]
    if expected_status:
        assert outcomes["pytest-native"][1] == (
            "test_future[1]",
            [("error", "failed on setup with \"KeyError: 'value'\"")],
        )


def test_inventory_expansion_keeps_families_pending_in_the_current_module(
    tmp_path: Path,
) -> None:
    source_a = """import pytest
@pytest.mark.parametrize('value', [0, 1])
def test_first(value):
    pass
"""
    source_b = """import json
from pathlib import Path
import pytest
@pytest.mark.parametrize('value', [0, 1])
def test_second(value):
    pass
def test_inventory(request):
    observed = [[item.nodeid, item.callspec.params if hasattr(item, 'callspec') else None]
                for item in request.session.items]
    Path('inventory.json').write_text(json.dumps(observed))
    assert len(observed) == 5
    assert request.session.items[-1] is request.node
"""
    observations: dict[str, object] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_a.py", source_a)
        project.write("test_b.py", source_b)
        result, _ = _run_native_or_stock(project, engine, "-q")
        assert result.returncode == 0, result.stdout + result.stderr
        observations[engine] = json.loads((project.root / "inventory.json").read_text())
    assert (
        observations["pytest-native"]
        == observations["pytest"]
        == [
            ["test_a.py::test_first[0]", {"value": 0}],
            ["test_a.py::test_first[1]", {"value": 1}],
            ["test_b.py::test_second[0]", {"value": 0}],
            ["test_b.py::test_second[1]", {"value": 1}],
            ["test_b.py::test_inventory", None],
        ]
    )


@pytest.mark.parametrize("argument", ["value", "composite"])
def test_deleted_future_parameter_reports_the_actual_pytest_caller(
    tmp_path: Path, argument: str
) -> None:
    source = f"""import pytest

@pytest.fixture(params=[1])
def value(request):
    return request.param

@pytest.fixture
def composite(value):
    return value

def test_mutate(request):
    del request.session.items[1].callspec.params['value']

def test_future({argument}):
    pass
"""
    outcomes: dict[str, list[tuple[str | None, list[tuple[str, str | None]]]]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_cases.py", source)
        result, _ = _run_native_or_stock(project, engine, "-q", "--junitxml=result.xml")
        project.write("stdout.txt", result.stdout)
        project.write("stderr.txt", result.stderr)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "1 passed, 1 error" in result.stdout
        outcomes[engine] = [
            (case.get("name"), [(child.tag, child.get("message")) for child in case])
            for case in ET.parse(project.root / "result.xml").iter("testcase")
        ]
    assert outcomes["pytest-native"] == outcomes["pytest"]
    message = outcomes["pytest-native"][1][1][0][1]
    assert message is not None
    assert "Requested here:" in message
    assert "_pytest/fixtures.py:" in message


@pytest.mark.parametrize("indirect", [False, True])
@pytest.mark.parametrize("mutation", ["replace", "delete"])
def test_changed_future_parameter_releases_the_old_generated_value(
    tmp_path: Path, indirect: bool, mutation: str
) -> None:
    fixture = "@pytest.fixture\ndef value(request):\n    return request.param" if indirect else ""
    change = "request.session.items[1].callspec.params['value']"
    change = f"{change} = 11" if mutation == "replace" else f"del {change}"
    source = f"""import gc
import json
import weakref
from pathlib import Path
import pytest

refs = []
def emit(value):
    with Path('trace.jsonl').open('a') as stream:
        stream.write(json.dumps(value) + '\\n')

class Value:
    def __init__(self):
        refs.append(weakref.ref(self, lambda _: emit('released')))

def pytest_generate_tests(metafunc):
    if 'value' in metafunc.fixturenames:
        metafunc.parametrize('value', [Value()], indirect={indirect!r})

def test_mutate(request):
    {change}
    gc.collect()
    emit(['after_{mutation}', refs[0]() is None])

{fixture}

def test_future(value):
    emit(['body', value])
"""
    observations: dict[str, list[object]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_cases.py", source)
        result, _ = _run_native_or_stock(project, engine, "-q")
        project.write("stdout.txt", result.stdout)
        project.write("stderr.txt", result.stderr)
        assert result.returncode == (0 if mutation == "replace" else 1), (
            result.stdout + result.stderr
        )
        assert ("2 passed" if mutation == "replace" else "1 passed, 1 error") in result.stdout
        observations[engine] = [
            json.loads(line) for line in (project.root / "trace.jsonl").read_text().splitlines()
        ]
    expected: list[object] = ["released", [f"after_{mutation}", True]]
    if mutation == "replace":
        expected.append(["body", 11])
    assert observations["pytest-native"] == observations["pytest"] == expected
