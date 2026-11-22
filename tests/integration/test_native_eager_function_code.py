"""Audit callbacks can change constructor code without rebinding the function."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.native import run_parameter_suite

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or pytest.__version__ != "9.1.1",
    reason="eager Function batching requires Unix and pinned pytest 9.1.1",
)

_CASES = """\
import gc
import os
import sys
from pathlib import Path
from _pytest.mark.structures import NodeKeywords
from _pytest.stash import Stash

keyword_init = NodeKeywords.__init__
stash_init = Stash.__init__
mode = os.environ['EAGER_MUTATION_MODE']
armed = False
audit_events = []

def replacement(self, node):
    self.node = node
    self.parent = node.parent
    self._markers = {node.name: True, 'audit_keyword': True}

def audit(event, args):
    global armed
    if not armed:
        return
    matches = (mode == 'code' and event == 'object.__getattr__'
               and args[0] is stash_init and args[1] == '__code__')
    matches = matches or (mode == 'referents' and event == 'gc.get_referents')
    if matches:
        armed = False
        audit_events.append(mode)
        keyword_init.__code__ = replacement.__code__

sys.addaudithook(audit)

def pytest_generate_tests(metafunc):
    global armed
    metafunc.parametrize('value', range(4))
    if metafunc.function.__name__ != 'test_victim':
        return
    armed = True
    if os.environ['EAGER_MUTATION_ROUTE'] == 'pytest':
        # Give stock constructors the same post-audit state.
        if mode == 'code':
            stash_init.__code__
        else:
            gc.get_referents(keyword_init, stash_init)

def record(label, value):
    with Path('executed').open('a') as stream:
        stream.write(f'{label}:{value}\\n')

def test_control(value):
    record('control', value)

def test_victim(value, request):
    assert 'audit_keyword' in request.node.keywords
    record('victim', value)
"""

_OBSERVER = """\
import gc
import json
import os
from pathlib import Path
from _pytest.fixtures import TopRequest
from _pytest.mark.structures import NodeKeywords
from _pytest.python import Function

def pytest_collection():
    gc.disable()

def pytest_collection_finish(session):
    assert len(session.items) == 8
    module = session.items[0].module
    assert module.audit_events == [os.environ['EAGER_MUTATION_MODE']]
    assert NodeKeywords.__init__ is module.keyword_init
    rows = []
    for item in session.items:
        assert type(item) is Function
        raw = object.__getattribute__(item, '__dict__')
        assert raw is vars(item)
        assert type(raw['_request']) is TopRequest
        assert raw['_request']._pyfuncitem is item
        assert type(item.keywords) is NodeKeywords
        changed = 'audit_keyword' in item.keywords
        assert changed == (item.originalname == 'test_victim')
        rows.append([item.nodeid, changed, list(item.user_properties)])
    payload = {'audit': module.audit_events, 'rows': rows}
    Path(f'collection-{os.getpid()}.json').write_text(json.dumps(payload))
    gc.enable()
    gc.collect()
    gc.freeze()
"""


@pytest.mark.parametrize("mode", ["code", "referents"])
def test_eager_batch_preserves_audited_constructor_code(tmp_path: Path, mode: str) -> None:
    observations: dict[str, tuple[dict[str, object], list[str]]] = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", _OBSERVER)
        project.write("test_family.py", _CASES)
        environment = {
            "VFY_WORKER_START": "exec",
            "EAGER_MUTATION_ROUTE": route,
            "EAGER_MUTATION_MODE": mode,
        }
        result = run_parameter_suite(project, route, env=environment)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "8 passed" in result.stdout
        collections = [
            json.loads(path.read_text()) for path in project.root.glob("collection-*.json")
        ]
        assert len(collections) == (2 if route == "native" else 1)
        assert all(value == collections[0] for value in collections)
        observations[route] = (
            collections[0],
            sorted((project.root / "executed").read_text().splitlines()),
        )
    assert observations["native"] == observations["pytest"]
    assert observations["pytest"][1] == [
        f"{label}:{value}" for label in ("control", "victim") for value in range(4)
    ]
