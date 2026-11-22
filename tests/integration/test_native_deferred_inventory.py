"""Exercise compact callspecs through real CLI runs and ordinary pytest hooks."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import cast

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native workers require Unix")

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
    assert type(items) is list
    assert all(type(item) is Function for item in items)
    assert len({id(item) for item in items}) == 4
    Path(f'collection-{os.getpid()}.json').write_text(json.dumps([
        [item.nodeid for item in items],
    ]))
    last = items[-1]
    raw = vars(last)
    assert raw is vars(last) and raw is last.__dict__
    assert raw['keywords'] is last.keywords
    assert raw['stash'] is last.stash and last._store is last.stash
    assert type(raw['callspec']) is CallSpec2
    spec = raw['callspec']
    specraw = vars(spec)
    assert specraw is spec.__dict__ and specraw is vars(spec)
    assert specraw['params'] is spec.params
    # Dictionary writes bypass field descriptors after the public mapping has
    # materialized. They must remain the one authoritative mutable state.
    specraw['params'] = {'value': 17}
    raw['_obj'] = replacement
    raw['user_properties'].append(('observer', 'kept'))
    gc.enable()
    gc.collect()
    gc.freeze()
"""

_CASES = """\
import json
from pathlib import Path
import pytest

@pytest.mark.parametrize('value', [0, 1, 2, 3])
def test_case(value):
    assert value in [0, 1, 2]
    with Path('events.jsonl').open('a') as stream:
        stream.write(json.dumps(['original', value]) + '\\n')
"""


def _events(project: VfyProject) -> list[object]:
    return cast(
        list[object],
        sorted(
            json.loads(line) for line in (project.root / "events.jsonl").read_text().splitlines()
        ),
    )


def test_workers_preserve_dense_callspecs_and_public_vars_mutation(
    tmp_path: Path,
) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", _OBSERVER)
        project.write("test_cases.py", _CASES)
        if route == "pytest":
            result = project.run_pytest(
                "--junitxml=result.xml",
                "-q",
                load_xdist=False,
                env={
                    "LAZY_PROOF_ROUTE": route,
                },
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
                env={
                    "LAZY_PROOF_ROUTE": route,
                    "VFY_WORKER_START": "exec",
                    "VFY_NATIVE_SCHEDULING": "cases",
                },
            )
        result.expect(0)
        assert "4 passed" in result.stdout, result.stdout + result.stderr
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
        observations[route] = (_events(project), cases)
        collections = [
            json.loads(path.read_text()) for path in project.root.glob("collection-*.json")
        ]
        assert collections
        assert all(len(collection[0]) == 4 for collection in collections)
        if route == "native":
            assert len(collections) == 2, collections
    assert observations["native"] == observations["pytest"]


def test_generation_retained_dict_aliases_remain_live_in_workers(
    tmp_path: Path,
) -> None:
    conftest = """\
def pytest_collection_modifyitems(items):
    module = items[0].module
    assert module.generation_calls == 1
    assert vars(items[0].callspec) is module.saved_vars
    assert items[-1].callspec.params is module.saved_params
    module.saved_vars['params']['value'] = 13
    module.saved_params['value'] = 19
"""
    cases = """\
import json
from pathlib import Path
saved_params = None
saved_vars = None
generation_calls = 0

def pytest_generate_tests(metafunc):
    global saved_params, saved_vars, generation_calls
    generation_calls += 1
    metafunc.parametrize('value', [0, 1, 2])
    saved_vars = vars(metafunc._calls[0])
    saved_params = metafunc._calls[-1].params

def test_case(value):
    assert value in (13, 1, 19)
    with Path('events.jsonl').open('a') as stream:
        stream.write(json.dumps(value) + '\\n')
"""
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", conftest)
        project.write("test_cases.py", cases)
        if route == "pytest":
            result = project.run_pytest("-q", load_xdist=False)
        else:
            result = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--no-replay",
                "--no-cov",
                "-q",
                env={
                    "VFY_WORKER_START": "exec",
                    "VFY_NATIVE_SCHEDULING": "cases",
                },
            )
        result.expect(0)
        assert "3 passed" in result.stdout
        observations[route] = _events(project)
    assert observations["native"] == observations["pytest"] == [1, 13, 19]


def test_canonical_fields_preserve_constructor_time_bindings(tmp_path: Path) -> None:
    conftest = """\
def pytest_collection_modifyitems(items):
    for item in items:
        original_name, original_parent = item.name, item.parent
        fixtureinfo = item._fixtureinfo
        definitions = fixtureinfo.name2fixturedefs
        item.name = 'changed-after-construction'
        item.parent = None
        item._fixtureinfo = object()
        try:
            assert original_name in item.keywords
            assert 'changed-after-construction' not in item.keywords
            assert item.keywords.parent is original_parent
            assert item._request._arg2fixturedefs is definitions
        finally:
            item.name, item.parent = original_name, original_parent
            item._fixtureinfo = fixtureinfo
"""
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", conftest)
        project.write(
            "test_cases.py",
            "import pytest\n@pytest.mark.parametrize('value', [0, 1])\n"
            "def test_case(value):\n    assert value in [0, 1]\n",
        )
        if route == "pytest":
            result = project.run_pytest("-q", load_xdist=False)
        else:
            result = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--no-replay",
                "--no-cov",
                "-q",
                env={
                    "VFY_WORKER_START": "exec",
                    "VFY_NATIVE_SCHEDULING": "cases",
                },
            )
        result.expect(0)
        assert "2 passed" in result.stdout
