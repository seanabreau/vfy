"""Fixture schemas preserve item mutation, shadow chains and family isolation."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CONFTEST = """\
import json
from pathlib import Path
import pytest

def record(phase, request, value):
    with Path('events.jsonl').open('a') as stream:
        stream.write(json.dumps([phase, request.node.nodeid, value]) + '\\n')

@pytest.fixture
def value(request):
    record('setup', request, 'root')
    yield 'root'
    record('teardown', request, 'root')

@pytest.fixture
def dynamic(request):
    record('setup', request, 'dynamic')
    yield object()
    record('teardown', request, 'dynamic')

@pytest.fixture
def extra(request):
    record('setup', request, 'extra')
    yield
    record('teardown', request, 'extra')
"""

_MUTATION = """\
class FixtureDefinitions(dict):
    pass

def pytest_collection_modifyitems(session, items):
    target = next(item for item in items if item.nodeid == 'test_a.py::test_second')
    definitions = FixtureDefinitions(target._fixtureinfo.name2fixturedefs)
    definitions['extra'] = session._fixturemanager.getfixturedefs('extra', target)
    object.__setattr__(target._fixtureinfo, 'name2fixturedefs', definitions)
    target.fixturenames = [*target.fixturenames, 'extra']
"""

_TEST_A = """\
import pytest
from conftest import record

def test_first(value, request):
    assert value == 'root'
    found = request.getfixturevalue('dynamic')
    assert request.getfixturevalue('dynamic') is found
    assert 'dynamic' in request.fixturenames
    record('call', request, value)

def test_second(value, request):
    assert value == 'root'
    assert 'dynamic' not in request.fixturenames
    record('call', request, value)

@pytest.mark.parametrize('number', [0, 1, 2], ids=['zero', 'one', 'two'])
def test_family(value, number, request):
    assert value == 'root'
    assert 'dynamic' not in request.fixturenames
    record('call', request, [value, number])

class TestShadow:
    @pytest.fixture
    def value(self, request):
        parent = request.getfixturevalue('value')
        record('setup', request, 'class')
        yield parent + ':class'
        record('teardown', request, 'class')

    @pytest.mark.parametrize('number', [0, 1], ids=['zero', 'one'])
    def test_family(self, value, number, request):
        assert value == 'root:class'
        assert not hasattr(self, 'visited')
        self.visited = True
        record('call', request, [value, number])
"""

_TEST_B = """\
import pytest
from conftest import record

@pytest.fixture
def value(value, request):
    record('setup', request, 'module')
    yield value + ':module'
    record('teardown', request, 'module')

def test_first(value, request):
    assert value == 'root:module'
    record('call', request, value)

def test_second(value, request):
    assert value == 'root:module'
    record('call', request, value)
"""


@pytest.mark.parametrize("mutate_item", [False, True], ids=["families", "custom-fixture-info"])
def test_fixture_schema_sharing_preserves_shadowing_and_item_isolation(
    tmp_path: Path, mutate_item: bool
) -> None:
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", _CONFTEST + (_MUTATION if mutate_item else ""))
        project.write("test_a.py", _TEST_A)
        project.write("test_b.py", _TEST_B)
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "--junitxml=junit.xml", "-q"
        )
        result.expect(0)
        assert "9 passed" in result.stdout
        events = [
            json.loads(line) for line in (project.root / "events.jsonl").read_text().splitlines()
        ]
        cases = [
            (case.get("classname"), case.get("name"), [child.tag for child in case])
            for case in ET.parse(project.root / "junit.xml").iter("testcase")
        ]
        observations[engine] = (events, cases)
    assert observations["pytest-native"] == observations["pytest"]
    events, cases = observations["pytest"]
    assert len(cases) == 9
    assert [event for event in events if event[2] == "dynamic"] == [
        ["setup", "test_a.py::test_first", "dynamic"],
        ["teardown", "test_a.py::test_first", "dynamic"],
    ]
    assert [event for event in events if event[2] == "extra"] == (
        [
            ["setup", "test_a.py::test_second", "extra"],
            ["teardown", "test_a.py::test_second", "extra"],
        ]
        if mutate_item
        else []
    )
