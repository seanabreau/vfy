"""Fixture dependency parameters preserve pytest's IDs and Cartesian order."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()


def test_class_fixture_dimensions_precede_direct_parameters_and_name_fallbacks(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_parameters.py",
        """from functools import partial
import pytest

class Factory:
    pass

class TestParameters:
    @pytest.fixture(params=(Factory, partial(Factory)))
    def factory(self, request):
        return request.param

    @pytest.fixture
    def resource(self, factory):
        return factory()

    @pytest.mark.parametrize('value', [None, True, [1], {}])
    def test_values(self, resource, value):
        pass

    @pytest.mark.parametrize('transform', [lambda x: x, lambda x: x])
    def test_functions(self, resource, transform):
        pass

@pytest.mark.parametrize('value', [1, 2, 3, 4, 5], ids=['a', 'a', 'a0', 'x1', 'x1'])
def test_duplicate_ids(value):
    pass
""",
    )
    oracle = project.run_python("-m", "pytest", "--collect-only", "-q")
    candidate = project.run_python(
        "-m", "vfy.independent.entry", "--collect-only", env={"PYTHONPATH": _SOURCE}
    )
    assert oracle.returncode == candidate.returncode == 0, (oracle.stderr, candidate.stderr)
    expected = [line for line in oracle.stdout.splitlines() if "::" in line]
    actual = [line for line in candidate.stdout.splitlines() if "::" in line]
    assert actual == expected
    assert actual[:4] == [
        "test_parameters.py::TestParameters::test_values[Factory-None]",
        "test_parameters.py::TestParameters::test_values[Factory-True]",
        "test_parameters.py::TestParameters::test_values[Factory-value2]",
        "test_parameters.py::TestParameters::test_values[Factory-value3]",
    ]


def test_stacked_mixed_parameters_preserve_ids_marks_and_call_order(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nmarkers = label(value): parameter label\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import test_parameters

def pytest_collection_finish(session):
    rows = [
        [item.nodeid, item.callspec.params, list(item.callspec._idlist),
         item.callspec.indices['right'],
         [mark.args[0] for mark in item.iter_markers('label')]]
        for item in session.items
    ]
    Path('collection.json').write_text(json.dumps([test_parameters.ids, rows]))
""",
    )
    project.write(
        "test_parameters.py",
        """import json
from pathlib import Path
import pytest

ids = []
def identify(value):
    ids.append(value)
    return str(value)

@pytest.fixture
def right(request):
    return request.param

@pytest.mark.parametrize('tail', [pytest.param('x', marks=pytest.mark.label('tail')), 'y'],
                         ids=identify)
@pytest.mark.parametrize(('left', 'right'),
                         [pytest.param(1, 2, marks=pytest.mark.label('pair')), (3, 4), (5, 6)],
                         indirect=['right'], ids=identify)
@pytest.mark.parametrize('head', ['a', 'b'], ids=identify)
def test_values(head, left, right, tail, request):
    with Path('calls.jsonl').open('a') as stream:
        stream.write(json.dumps([request.node.nodeid, head, left, right, tail]) + '\\n')
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected_collection = json.loads((tmp_path / "collection.json").read_text())
    expected_calls = (tmp_path / "calls.jsonl").read_text()
    assert expected_collection[0] == ["a", "b", 1, 2, 3, 4, 5, 6, "x", "y"]
    assert [row[0] for row in expected_collection[1]] == [
        f"test_parameters.py::test_values[{head}-{left}-{right}-{tail}]"
        for head in ["a", "b"]
        for left, right in [(1, 2), (3, 4), (5, 6)]
        for tail in ["x", "y"]
    ]
    (tmp_path / "calls.jsonl").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((tmp_path / "collection.json").read_text()) == expected_collection
    assert (tmp_path / "calls.jsonl").read_text() == expected_calls


def test_high_scope_parameter_grouping_matches_pytest_execution_order(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

events = []

@pytest.fixture(scope='session', params=['s0', 's1'])
def session_value(request):
    events.append(['setup-session', request.param])
    yield request.param
    events.append(['teardown-session', request.param])

@pytest.fixture(scope='module', params=['m0', 'm1'])
def module_value(request):
    events.append(['setup-module', request.node.nodeid, request.param])
    yield request.param
    events.append(['teardown-module', request.node.nodeid, request.param])

def pytest_collection_finish(session):
    events.append(['collected', [item.nodeid for item in session.items]])

def pytest_sessionfinish(session):
    Path('events.json').write_text(json.dumps(events))
""",
    )
    for name in ("one", "two"):
        project.write(
            f"test_{name}.py",
            """import pytest
from conftest import events

@pytest.fixture(scope='class', params=['c0', 'c1'])
def class_value(request):
    events.append(['setup-class', request.node.nodeid, request.param])
    yield request.param
    events.append(['teardown-class', request.node.nodeid, request.param])

class TestGroup:
    def test_first(self, session_value, module_value, class_value, request):
        events.append(['test', request.node.nodeid])

    def test_second(self, module_value, class_value, request):
        events.append(['test', request.node.nodeid])

def test_other(session_value, module_value, request):
    events.append(['test', request.node.nodeid])
""",
        )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "events.json").read_text())
    assert any(event[0] == "setup-class" for event in expected)
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((tmp_path / "events.json").read_text()) == expected
