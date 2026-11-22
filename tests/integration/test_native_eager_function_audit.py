"""Audit-hook constructor replacements affect eager items."""

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
import os
import sys
from pathlib import Path
from _pytest.python import Function

original_init = Function.__init__
armed = False
audit_events = []
constructed = []

def replacement(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    if type(self) is Function and self.originalname == 'test_victim':
        constructed.append(self.name)
        self.user_properties.append(('constructor', 'audit-replacement'))

def audit(event, args):
    global armed
    if (armed and event == 'object.__getattr__'
        and args[0] is original_init and args[1] == '__code__'):
        armed = False
        audit_events.append('constructor-code')
        Function.__init__ = replacement

sys.addaudithook(audit)

def pytest_generate_tests(metafunc):
    global armed
    metafunc.parametrize('value', range(4))
    if metafunc.function.__name__ != 'test_victim':
        return
    armed = True
    if os.environ['EAGER_AUDIT_ROUTE'] == 'pytest':
        # Match the post-audit state; stock constructors do not read __code__.
        original_init.__code__

def test_control(value):
    pass

def test_victim(value, request):
    assert ('constructor', 'audit-replacement') in request.node.user_properties
    with Path('executed').open('a') as stream:
        stream.write(str(value) + '\\n')
"""

_OBSERVER = """\
import gc
import json
import os
from pathlib import Path
from _pytest.fixtures import TopRequest
from _pytest.python import Function

def pytest_collection():
    gc.disable()

def pytest_collection_finish(session):
    assert len(session.items) == 8
    module = session.items[0].module
    assert module.audit_events == ['constructor-code']
    assert Function.__init__ is module.replacement
    rows = []
    for item in session.items:
        assert type(item) is Function
        raw = object.__getattribute__(item, '__dict__')
        assert raw is vars(item)
        assert type(raw['_request']) is TopRequest
        assert raw['_request']._pyfuncitem is item
        if item.originalname == 'test_victim':
            assert item.user_properties == [('constructor', 'audit-replacement')]
        rows.append([item.nodeid, list(item.user_properties)])
    payload = {'audit': module.audit_events, 'constructed': module.constructed, 'rows': rows}
    Path(f'collection-{os.getpid()}.json').write_text(json.dumps(payload))
    gc.enable()
    gc.collect()
    gc.freeze()
"""


def test_eager_function_batch_rechecks_constructor_after_audit(tmp_path: Path) -> None:
    observations: dict[str, tuple[dict[str, object], list[str]]] = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", _OBSERVER)
        project.write("test_family.py", _CASES)
        environment = {
            "VFY_WORKER_START": "exec",
            "EAGER_AUDIT_ROUTE": route,
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
    assert observations["pytest"][0]["constructed"] == [
        f"test_victim[{index}]" for index in range(4)
    ]
    assert observations["pytest"][1] == [str(index) for index in range(4)]
