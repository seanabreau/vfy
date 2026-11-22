"""Native fixture facts reflect selected definitions, parameters, and real scopes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()
_RECORDER = """import inspect, json, os
from pathlib import Path

def record(request):
    if os.environ.get('FIXTURE_INFO_ORACLE') == '1':
        info = request._fixturemanager.getfixtureinfo(request.node, request.function, None)
        arguments = inspect.signature(request.function).parameters
        rows = [(name, request._get_active_fixturedef(name).scope,
                 any(bool(definition.params) for definition in definitions), name in arguments)
                for name, definitions in info.name2fixturedefs.items()]
    else:
        rows = request.fixture_info()
    with Path('facts.jsonl').open('a') as output:
        output.write(json.dumps([request.node.nodeid, rows]) + '\\n')
"""


def _project(path: Path, case: str) -> VfyProject:
    project = VfyProject(path)
    project.write("recorder.py", _RECORDER)
    test = "from recorder import record\n"
    if case == "dependencies":
        project.write(
            "conftest.py",
            """import pytest
@pytest.fixture(autouse=True)
def auto(): return 'auto'
@pytest.fixture(scope='session')
def session_value(): return 'session'
@pytest.fixture(scope='module')
def dependency(session_value): return session_value
@pytest.fixture
def value(dependency): return dependency
""",
        )
        test += "def test_facts(auto, value, request): record(request)\n"
    elif case == "autouse_parameters":
        project.write(
            "conftest.py",
            """import pytest
@pytest.fixture(autouse=True, params=[1, 2])
def auto(request): return request.param
""",
        )
        test += "def test_facts(request): record(request)\n"
    elif case == "shadowed_parameters":
        project.write(
            "conftest.py",
            """import pytest
@pytest.fixture(params=[1, 2])
def value(request): return request.param
""",
        )
        project.write(
            "sub/conftest.py",
            """import pytest
@pytest.fixture(scope='session')
def value(): return 'shadow'
""",
        )
        project.write(
            "sub/test_facts.py", test + "def test_facts(value, request): record(request)\n"
        )
        return project
    elif case == "indirect_scope_override":
        project.write(
            "conftest.py",
            """import pytest
@pytest.fixture
def value(request): return request.param
""",
        )
        test += """import pytest
@pytest.mark.parametrize('value', [1, 2], indirect=True, scope='session')
def test_facts(value, request): record(request)
"""
    elif case == "direct_parameters":
        test += """import pytest
@pytest.mark.parametrize('value', [1, 2])
def test_facts(value, request): record(request)
"""
    elif case == "dynamic_discovery":
        project.write(
            "conftest.py",
            """import pytest
@pytest.fixture
def hidden(): return 'dynamic'
@pytest.fixture
def value(request): return request.getfixturevalue('hidden')
""",
        )
        test += "def test_facts(value, request): record(request)\n"
    elif case == "override_dependencies":
        project.write(
            "conftest.py",
            """import pytest
@pytest.fixture
def dependency(): return 'outer'
@pytest.fixture
def value(dependency): return dependency
""",
        )
        project.write(
            "sub/conftest.py",
            """import pytest
@pytest.fixture
def value(value): return value
""",
        )
        project.write(
            "sub/test_facts.py", test + "def test_facts(value, request): record(request)\n"
        )
        return project
    elif case == "dependency_discovery_order":
        project.write(
            "conftest.py",
            """import pytest
@pytest.fixture
def left(): return 'left'
@pytest.fixture
def right(): return 'right'
@pytest.fixture
def first(left): return left
@pytest.fixture
def second(right): return right
""",
        )
        test += "def test_facts(first, second, request): record(request)\n"
    else:
        assert case == "builtins"
        test += (
            "def test_facts(monkeypatch, tmp_path, tmp_path_factory, request): record(request)\n"
        )
    project.write("test_facts.py", test)
    return project


def _facts(project: VfyProject) -> list[list[object]]:
    return [json.loads(line) for line in (project.root / "facts.jsonl").read_text().splitlines()]


@pytest.mark.parametrize(
    "case",
    [
        "dependencies",
        "autouse_parameters",
        "shadowed_parameters",
        "indirect_scope_override",
        "direct_parameters",
        "dynamic_discovery",
        "override_dependencies",
        "dependency_discovery_order",
        "builtins",
    ],
)
def test_fixture_info_matches_actual_pytest_definitions(tmp_path: Path, case: str) -> None:
    project = _project(tmp_path, case)
    oracle = project.run_python("-m", "pytest", "-q", env={"FIXTURE_INFO_ORACLE": "1"})
    oracle.expect(0)
    expected = _facts(project)
    (project.root / "facts.jsonl").unlink()
    actual = project.run_python("-m", "vfy.independent.entry", env={"PYTHONPATH": _SOURCE})
    actual.expect(0)
    assert _facts(project) == expected
    rows = expected[0][1]
    if case == "shadowed_parameters":
        assert rows == [["value", "session", True, True]]
    elif case == "indirect_scope_override":
        assert rows == [["value", "function", False, True]]
    elif case == "direct_parameters":
        assert rows == []
    elif case == "dynamic_discovery":
        assert rows == [["value", "function", False, True]]
    elif case == "builtins":
        assert rows == [
            ["monkeypatch", "function", False, True],
            ["tmp_path", "function", False, True],
            ["tmp_path_factory", "session", False, True],
        ]


def test_pyfunc_arguments_reuse_fixtureinfo_name_objects(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import inspect
import json
from pathlib import Path

events = []
expected_names = []
def pytest_runtest_call(item):
    expected_names[:] = item._fixtureinfo.argnames
    parameters = inspect.signature(item.obj).parameters
    code_names = inspect.unwrap(item.obj).__code__.co_varnames
    events.append(['name identity',
                   [name is parameters[name].name for name in expected_names],
                   [name is next(code_name for code_name in code_names if code_name == name)
                    for name in expected_names]])

def pytest_sessionfinish():
    Path('arguments.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_arguments.py",
        """import functools
import pytest
from conftest import events, expected_names

def observe(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        keys = list(kwargs)
        identity = [key is expected for key, expected in zip(keys, expected_names)]
        events.append(['kwargs', keys, identity])
        return function(*args, **kwargs)
    return wrapper

@pytest.mark.parametrize(
    ('first_parameter_long_name', 'second_parameter_long_name'), [(1, 2), (3, 4)]
)
@observe
def test_values(first_parameter_long_name, second_parameter_long_name):
    pass

class TestMethods:
    @pytest.mark.parametrize(
        ('first_parameter_long_name', 'second_parameter_long_name'), [(5, 6), (7, 8)]
    )
    @observe
    def test_method(self, first_parameter_long_name, second_parameter_long_name):
        pass
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((project.root / "arguments.json").read_text())
    assert expected == [
        ["name identity", [True, True], [True, True]],
        ["kwargs", ["first_parameter_long_name", "second_parameter_long_name"], [True, True]],
        ["name identity", [True, True], [True, True]],
        ["kwargs", ["first_parameter_long_name", "second_parameter_long_name"], [True, True]],
        ["name identity", [True, True], [True, True]],
        ["kwargs", ["first_parameter_long_name", "second_parameter_long_name"], [True, True]],
        ["name identity", [True, True], [True, True]],
        ["kwargs", ["first_parameter_long_name", "second_parameter_long_name"], [True, True]],
    ]
    (project.root / "arguments.json").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((project.root / "arguments.json").read_text()) == expected
