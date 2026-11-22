"""Generated direct parameters prune the shared fixture list without reordering it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("dependency_requests_request", [False, True])
def test_generated_parameters_preserve_fixture_list_and_setup_order(
    tmp_path: Path, dependency_requests_request: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

names = {}
observed = []
events = []

def pytest_generate_tests(metafunc):
    names[metafunc.function] = metafunc.fixturenames
    metafunc.fixturenames.reverse()
    metafunc.parametrize('value', [0, 1])

def pytest_itemcollected(item):
    observed.append([item.name, item.fixturenames is names[item.function],
                     list(item.fixturenames)])

def pytest_sessionfinish():
    Path('observations.json').write_text(json.dumps({'collection': observed, 'setup': events}))

@pytest.fixture
def dependency(DEPENDENCY_ARGUMENTS):
    raise AssertionError('shadowed fixture dependency must not execute')

@pytest.fixture
def value(dependency):
    return dependency

@pytest.fixture
def first():
    events.append('first')

@pytest.fixture
def second():
    events.append('second')
""".replace("DEPENDENCY_ARGUMENTS", "request" if dependency_requests_request else ""),
    )
    project.write(
        "test_subject.py",
        """def test_subject(value, first, second):
    assert value in (0, 1)
""",
    )
    observations = tmp_path / "observations.json"
    stock = project.run_python("-m", "pytest", "-q")
    stock.expect(0)
    expected = json.loads(observations.read_text())
    fixture_names = ["second", "first", "value"]
    if dependency_requests_request:
        fixture_names.insert(2, "request")
    assert expected == {
        "collection": [[f"test_subject[{index}]", True, fixture_names] for index in range(2)],
        "setup": ["second", "first", "second", "first"],
    }
    observations.unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    candidate.expect(0)
    assert json.loads(observations.read_text()) == expected


def test_direct_parameter_families_preserve_scope_and_shadowed_autouse_dependencies(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

events = []

def pytest_generate_tests(metafunc):
    metafunc.parametrize('value', ['first', 'second'], scope=metafunc.cls.parameter_scope)
    metafunc.parametrize('other', [10, 20], scope='function')

def pytest_sessionfinish():
    Path('observations.json').write_text(json.dumps(events))

@pytest.fixture
def resource():
    return 'conftest'
""",
    )
    project.write(
        "test_families.py",
        """import pytest
from conftest import events

@pytest.fixture
def resource(resource):
    return resource + '/module'

class Family:
    @pytest.fixture
    def resource(self, resource):
        return resource + '/' + type(self).__name__

    @pytest.fixture(autouse=True)
    def observe(self, value, other, resource):
        row = [type(self).__name__, value, other, resource]
        events.append(['setup', row])
        yield
        events.append(['teardown', row])

    def test_values(self, value, other, resource, request):
        assert value is request.getfixturevalue('value')
        assert other is request.getfixturevalue('other')
        events.append(['call', request.node.nodeid, value, other, resource,
                       request._fixture_defs['value'].scope,
                       request._fixture_defs['other'].scope,
                       request.node.callspec.indices['value'],
                       request.node.callspec.indices['other']])

class TestFunction(Family):
    parameter_scope = 'function'

class TestModule(Family):
    parameter_scope = 'module'
""",
    )
    observations = tmp_path / "observations.json"
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(0)
    expected = json.loads(observations.read_text())
    calls = [event for event in expected if event[0] == "call"]
    assert len(calls) == 8
    assert [event[5:7] for event in calls] == [["function", "function"]] * 4 + [
        ["module", "function"]
    ] * 4
    assert all(event[4].startswith("conftest/module/Test") for event in calls)
    observations.unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    candidate.expect(0)
    assert json.loads(observations.read_text()) == expected
