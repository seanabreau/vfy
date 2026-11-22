"""Primitive compilation keeps public parameter reads live at fixture setup."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.native import run_parameter_suite

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or pytest.__version__ != "9.1.1",
    reason="dense compiler experiment requires Unix and pinned pytest 9.1.1",
)

_SOURCE = """\
import gc
import json
import os
from pathlib import Path
import pytest
from _pytest.python import CallSpec2
from _pytest.scope import Scope

class Payload:
    def __init__(self, number):
        self.number = number

@pytest.fixture(autouse=True)
def active_item(request):
    global current_item
    current_item = request.node

@pytest.mark.parametrize(('control',), [(Payload(i),) for i in range(4)])
def test_control(control):
    assert type(control) is Payload
    assert current_item.callspec.params['control'] is control
    assert current_item.callspec.indices == {'control': control.number}
    Path(f'control-{control.number}').touch()

@pytest.fixture
def value(request):
    original = request.param
    mode = os.environ['DENSE_READER_MODE']
    if original % 2 == 0:
        if mode == 'getter':
            if not hasattr(CallSpec2, '_proof_original_getattribute'):
                getter = CallSpec2.__getattribute__
                CallSpec2._proof_original_getattribute = getter
                def replacement(callspec, name):
                    result = getter(callspec, name)
                    if name == 'params' and 'value' in result and result['value'] % 2:
                        return {'value': result['value'] + 50}
                    return result
                CallSpec2.__getattribute__ = replacement
        else:
            target = next(
                item.callspec for item in request.session.items
                if item.originalname == 'test_family'
                and item.callspec.params['value'] == original + 1
            )
            if mode == 'updates':
                object.__setattr__(target, 'params', {'value': original + 101})
                object.__setattr__(target, 'indices', {'value': original + 1001})
                object.__setattr__(target, '_arg2scope', {'value': Scope.Module})
            else:
                object.__setattr__(target, 'indices', {})
    return request.param, request.scope, request._pyfuncitem.callspec.indices['value']

@pytest.mark.parametrize('value', range(4), indirect=True)
def test_family(value):
    Path(f'event-{value[0]}.json').write_text(json.dumps([current_item.nodeid, *value]))
"""


@pytest.mark.parametrize("mode", ["updates", "index_error", "getter"])
def test_dense_compilation_preserves_runtime_parameter_changes(tmp_path: Path, mode: str) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_family.py", _SOURCE)
        project.write(
            "conftest.py",
            "import gc\nimport os\n"
            "def pytest_collection():\n    gc.disable()\n"
            "def pytest_collection_finish(session):\n"
            "    assert len(session.items) == 8\n"
            "    gc.enable()\n",
        )
        environment = {
            "DENSE_READER_ROUTE": route,
            "DENSE_READER_MODE": mode,
        }
        result = run_parameter_suite(project, route, env=environment)
        output = result.stdout + result.stderr
        assert result.returncode == (1 if mode == "index_error" else 0), output
        assert len(list(project.root.glob("control-*"))) == 4, output
        if mode == "index_error":
            assert "KeyError: 'value'" in output
        observations[route] = sorted(
            json.loads(path.read_text()) for path in project.root.glob("event-*.json")
        )
    assert observations["native"] == observations["pytest"]
    expected = {
        "updates": [
            (0, "function", 0),
            (101, "module", 1001),
            (2, "function", 2),
            (103, "module", 1003),
        ],
        "index_error": [(0, "function", 0), (2, "function", 2)],
        "getter": [
            (0, "function", 0),
            (51, "function", 1),
            (2, "function", 2),
            (53, "function", 3),
        ],
    }
    assert [tuple(row[1:]) for row in observations["pytest"]] == expected[mode]
