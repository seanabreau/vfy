"""Prove stock eager Function topology and customized-constructor fallback."""

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

retained_ids = None
class_bindings = []

def event(kind, value):
    with Path('events.jsonl').open('a') as stream:
        stream.write(json.dumps([kind, value]) + '\\n')

def pytest_generate_tests(metafunc):
    global retained_ids
    if metafunc.function.__name__ == 'test_plain':
        metafunc.parametrize('value', range(3), ids=['', 'β', 'third'])
    elif metafunc.function.__name__ == 'test_retained':
        metafunc.parametrize('value', range(2))
        retained_ids = metafunc._calls[0]._idlist
    elif metafunc.function.__name__ == 'test_raw':
        metafunc.parametrize('value', range(2))
        # Ordinary Python can obtain a raw instance mapping without invoking a
        # customized __getattribute__. Its fields must win over hidden rows.
        raw = object.__getattribute__(metafunc._calls[0], '__dict__')
        raw['marks'] = [pytest.mark.raw_first.mark]

@pytest.mark.alpha
def test_plain(value, request):
    assert request.node.keywords['alpha'] == 'source-alpha'
    event('plain', value)
test_plain.alpha = 'source-alpha'

def test_retained(value):
    event('retained', value)

def test_raw(value, request):
    assert bool(request.node.get_closest_marker('raw_first')) == (value == 0)
    event('raw', value)

class TestCases:
    @pytest.mark.gamma
    @pytest.mark.parametrize('value', range(2))
    def test_class(self, value):
        assert self.executed_on is self
        event('class', value)

class TestCustom:
    def pytest_generate_tests(self, metafunc):
        metafunc.parametrize('value', range(2))
        parent = metafunc.definition.parent
        def newinstance():
            instance = TestCustom()
            class_bindings.append(instance)
            return instance
        parent.newinstance = newinstance

    def test_custom(self, value):
        assert self.executed_on is self
        event('custom', value)
"""

_OBSERVER = """\
import gc
import json
import os
from pathlib import Path
from _pytest.mark.structures import NodeKeywords
from _pytest.python import Function
from _pytest.stash import Stash
from _pytest.fixtures import TopRequest

constructor_calls = []

def pytest_collection():
    gc.disable()
    if os.environ['EAGER_PROOF_MODE'] == 'constructor':
        original = Function.__init__
        def replacement(self, *args, **kwargs):
            if type(self) is Function:
                constructor_calls.append(kwargs['name'])
            original(self, *args, **kwargs)
        Function.__init__ = replacement

def pytest_collection_finish(session):
    items = session.items
    assert len(items) == 11 and all(type(item) is Function for item in items)
    assert len({id(item) for item in items}) == 11
    assert Function.__getattribute__ is object.__getattribute__
    assert Function.__setattr__ is object.__setattr__
    assert Function.__delattr__ is object.__delattr__
    if os.environ['EAGER_PROOF_MODE'] == 'constructor':
        assert len(constructor_calls) == 11
    module = items[0].module
    assert len(module.class_bindings) == 2
    classes = [item for item in items if item.instance is not None]
    assert len({id(item.instance) for item in classes}) == 4
    for item in classes:
        assert item.obj.__self__ is item.instance
        item.instance.executed_on = item.instance
    observations = []
    for item in items:
        raw = vars(item)
        assert raw is item.__dict__ and raw is vars(item)
        assert type(raw['keywords']) is NodeKeywords and raw['keywords'].node is item
        assert raw['keywords'].parent is item.parent
        assert type(raw['stash']) is Stash and item._store is raw['stash']
        assert type(raw['_request']) is TopRequest and raw['_request']._pyfuncitem is item
        assert raw['_request']._arg2fixturedefs is item._fixtureinfo.name2fixturedefs
        assert raw['fixturenames'] is item._fixtureinfo.names_closure
        assert type(raw['own_markers']) is list and type(raw['extra_keyword_matches']) is set
        assert type(raw['funcargs']) is dict and type(raw['user_properties']) is list
        assert type(raw['_report_sections']) is list
        raw['user_properties'].append(('observer', 'kept'))
        observations.append([
            item.nodeid, list(raw), list(vars(item._request)),
            [mark.name for mark in item.own_markers], list(item.keywords._markers),
        ])
    retained = next(item for item in items if item.originalname == 'test_retained')
    assert retained.callspec._idlist is module.retained_ids
    Path(f'collection-{os.getpid()}.json').write_text(json.dumps(observations))
    gc.enable()
    gc.collect()
    gc.freeze()
"""


@pytest.mark.parametrize("mode", ["plain", "constructor"])
def test_eager_batch_preserves_stock_fields_and_custom_construction(
    tmp_path: Path,
    mode: str,
) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "pytest.ini",
            "[pytest]\nfilterwarnings =\n    ignore::pytest.PytestUnknownMarkWarning\n",
        )
        project.write("test_cases.py", _CASES)
        project.write("conftest.py", _OBSERVER)
        environment = {
            "VFY_WORKER_START": "exec",
            "EAGER_PROOF_ROUTE": route,
            "EAGER_PROOF_MODE": mode,
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
        assert "11 passed" in result.stdout
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
        collections = [
            json.loads(path.read_text()) for path in project.root.glob("collection-*.json")
        ]
        assert len(collections) == (2 if route == "native" else 1)
        assert all(collection == collections[0] for collection in collections)
        observations[route] = events, cases, collections[0]
    assert observations["native"] == observations["pytest"]
