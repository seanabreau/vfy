"""Compare dense callspecs with canonical Function construction through workers."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native workers require Unix")

_CASES = """\
import json
from pathlib import Path
import pytest

def event(kind, value):
    with Path('events.jsonl').open('a') as stream:
        stream.write(json.dumps([kind, value]) + '\\n')

@pytest.mark.parametrize('value', range(4))
def test_case(value):
    assert value in (0, 1, 2)
    event('original', value)

class TestCases:
    @pytest.mark.parametrize('value', range(2))
    def test_class(self, value):
        assert self.executed_on is self
        event('class', value)
"""

_OBSERVER = """\
import gc
import json
import os
from pathlib import Path
from _pytest.python import Function, CallSpec2

def replacement(value):
    assert value == 17
    with Path('events.jsonl').open('a') as stream:
        stream.write(json.dumps(['replacement', value]) + '\\n')

def pytest_collection():
    gc.disable()

def pytest_collection_finish(session):
    items = session.items
    assert len(items) == 6 and all(type(item) is Function for item in items)
    assert len({id(item) for item in items}) == 6
    assert Function.__getattribute__ is object.__getattribute__
    assert Function.__setattr__ is object.__setattr__
    assert Function.__delattr__ is object.__delattr__
    for item in items:
        assert item.keywords.node is item and item.keywords.parent is item.parent
        assert item._request._pyfuncitem is item
        assert item._request._arg2fixturedefs is item._fixtureinfo.name2fixturedefs
        assert item._store is item.stash
    classes = [item for item in items if item.originalname == 'test_class']
    assert len({id(item.instance) for item in classes}) == 2
    for item in classes:
        assert item.obj.__self__ is item.instance
        item.instance.executed_on = item.instance
    last = [item for item in items if item.originalname == 'test_case'][-1]
    raw = vars(last)
    assert raw is vars(last) and raw is last.__dict__
    assert type(raw['callspec']) is CallSpec2
    specraw = vars(raw['callspec'])
    assert specraw is vars(raw['callspec'])
    specraw['params'] = {'value': 17}
    raw['_obj'] = replacement
    raw['user_properties'].append(('observer', 'kept'))
    owner = os.environ.get('VFY_NATIVE_PARALLEL_INDEX', 'controller')
    Path(f'collection-{os.getpid()}-{owner}.json').write_text(
        json.dumps([item.nodeid for item in items])
    )
    gc.enable()
    gc.collect()
    gc.freeze()
"""


def test_dense_callspecs_preserve_canonical_function_state(tmp_path: Path) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_cases.py", _CASES)
        project.write("conftest.py", _OBSERVER)
        environment = {
            "VFY_WORKER_START": "exec",
            "CANONICAL_PROOF_ROUTE": route,
            "VFY_NATIVE_SCHEDULING": "cases",
        }
        if route == "pytest":
            result = project.run_pytest(
                "--junitxml=result.xml", "-q", load_xdist=False, env=environment
            )
        else:
            result = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--no-replay",
                "--no-cov",
                "--junitxml=result.xml",
                "-q",
                env=environment,
            )
        result.expect(0)
        assert "6 passed" in result.stdout
        events = sorted(
            json.loads(line) for line in (project.root / "events.jsonl").read_text().splitlines()
        )
        cases = [
            (
                case.get("name"),
                [
                    (node.get("name"), node.get("value"))
                    for node in case.findall("properties/property")
                ],
            )
            for case in ET.parse(project.root / "result.xml").iter("testcase")
        ]
        collection_paths = list(project.root.glob("collection-*.json"))
        owners = [path.stem.rsplit("-", 1)[1] for path in collection_paths]
        assert sorted(owners) == (["0", "1"] if route == "native" else ["controller"])
        collections = [json.loads(path.read_text()) for path in collection_paths]
        assert all(collection == collections[0] for collection in collections)
        observations[route] = events, cases, collections[0]
    assert observations["native"] == observations["pytest"]
