"""Collected items keep stock parents, fixture owners and execution order."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()
_TARGETS = {
    "file": ["d/test_one.py", "d/test_one.py"],
    "directory": ["d", "d"],
    "selector": ["d/test_one.py::test_a", "d/test_one.py::test_a"],
    "class": ["d/test_one.py::TestGroup", "d/test_one.py::TestGroup"],
    "file-selector": ["d/test_one.py", "d/test_one.py::test_a"],
    "selector-file": ["d/test_one.py::test_a", "d/test_one.py"],
    "directory-file": ["d", "d/test_one.py"],
    "file-directory": ["d/test_one.py", "d"],
    "interleave": ["d/test_one.py", "d/test_two.py", "d/test_one.py"],
    "disjoint-selectors": ["d/test_one.py::test_a", "d/test_one.py::TestGroup::test_b"],
}
_BODY = """def test_a():
    pass
class TestGroup:
    def test_b(self):
        pass
"""
_OBSERVER = """import json
from pathlib import Path
import pytest

state = {'events': []}
_APPEND = False

def event(*values):
    state['events'].append(list(values))

def snapshot(items):
    return [dict(nodeid=item.nodeid, parent=item.parent.nodeid,
                 module=item.getparent(pytest.Module).nodeid) for item in items]

@pytest.fixture(scope='session', autouse=True)
def session_resource():
    event('session setup')
    yield
    event('session teardown')

@pytest.fixture(scope='module', autouse=True)
def module_resource(request):
    event('module setup', request.node.nodeid)
    yield
    event('module teardown', request.node.nodeid)

@pytest.fixture(scope='class', autouse=True)
def class_resource(request):
    event('class setup', request.node.nodeid)
    yield
    event('class teardown', request.node.nodeid)

@pytest.fixture(autouse=True)
def function_resource(request):
    event('function setup', request.node.nodeid)
    yield
    event('function teardown', request.node.nodeid)

def pytest_itemcollected(item):
    event('collected', item.nodeid, len(item.session.items))

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(session, items):
    state['before modification'] = snapshot(items)
    yield
    state['after fixture grouping'] = snapshot(items)
    if _APPEND:
        items.append(items[0])
    state['selected'] = snapshot(items)

def pytest_runtest_call(item):
    event('call', item.nodeid)

def pytest_sessionfinish(session, exitstatus):
    state['finish'] = [int(exitstatus), session.testscollected, session.testsfailed]
    Path('observation.json').write_text(json.dumps(state))
"""


def _run(project: VfyProject, engine: str, targets: list[str]) -> dict[str, Any]:
    arguments = ["-m", engine, "-q", *targets]
    result = project.run_python(*arguments, env={"PYTHONPATH": _SOURCE})
    result.expect(0)
    return json.loads((project.root / "observation.json").read_text())


@pytest.mark.parametrize("mode", _TARGETS)
def test_normalized_targets_keep_real_parent_and_fixture_owners(tmp_path: Path, mode: str) -> None:
    observations = []
    for engine in ("pytest", "vfy.independent.entry"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", _OBSERVER)
        project.write("d/test_one.py", _BODY)
        project.write("d/test_two.py", _BODY)
        observations.append(_run(project, engine, _TARGETS[mode]))
    assert observations[0] == observations[1]


@pytest.mark.parametrize("adjacent", [True, False])
def test_reappended_actual_item_runs_each_slot_like_stock(tmp_path: Path, adjacent: bool) -> None:
    observations = []
    for engine in ("pytest", "vfy.independent.entry"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", _OBSERVER.replace("_APPEND = False", "_APPEND = True"))
        project.write("d/test_one.py", _BODY)
        targets = ["d/test_one.py::test_a" if adjacent else "d/test_one.py"]
        observations.append(_run(project, engine, targets))
    assert observations[0] == observations[1]


def test_failed_class_collection_keeps_successful_siblings(tmp_path: Path) -> None:
    observations = []
    for engine in ("pytest", "vfy.independent.entry"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import json
from pathlib import Path
import pytest

events = []

def pytest_configure(config):
    original = pytest.Class.newinstance
    def newinstance(node):
        if node.name == 'TestBroken':
            raise ValueError('actual isolated class failure')
        return original(node)
    pytest.Class.newinstance = newinstance

def pytest_itemcollected(item):
    events.append(['item', item.nodeid])

def pytest_sessionfinish(session, exitstatus):
    events.append(['finish', int(exitstatus), session.testscollected, session.testsfailed])
    Path('observation.json').write_text(json.dumps(events))
""",
        )
        project.write(
            "test_siblings.py",
            """class TestBroken:
    def test_broken(self):
        pass
class TestGood:
    def test_good(self):
        pass
def test_plain():
    pass
""",
        )
        result = project.run_python("-m", engine, "-q", env={"PYTHONPATH": _SOURCE})
        result.expect(2)
        assert "actual isolated class failure" in result.stdout
        observations.append(json.loads((project.root / "observation.json").read_text()))
    assert observations[0] == observations[1]


@pytest.mark.parametrize("workers", [1, 2])
def test_reappended_item_uses_the_requested_worker_processes(tmp_path: Path, workers: int) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(items):
    yield
    items.append(items[0])
""",
    )
    project.write(
        "test_slots.py",
        """import os
from pathlib import Path

def note(name):
    with Path('body-events.txt').open('a') as output:
        output.write(f'{name} {os.getpid()}\\n')

def test_a():
    note('a')
""",
    )
    project.write(
        "test_slots_other.py",
        "from test_slots import note\n\ndef test_b():\n    note('b')\n",
    )
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(0)
    body = project.root / "body-events.txt"
    assert sorted(line.split()[0] for line in body.read_text().splitlines()) == ["a", "a", "b"]
    body.unlink()
    result = project.run_python(
        "-m",
        "vfy.independent.entry",
        "-q",
        f"--workers={workers}",
        env={"PYTHONPATH": _SOURCE},
    )
    result.expect(0)
    lines = [line.split() for line in body.read_text().splitlines()]
    assert sorted(line[0] for line in lines) == ["a", "a", "b"]
    assert len({line[1] for line in lines}) == workers
