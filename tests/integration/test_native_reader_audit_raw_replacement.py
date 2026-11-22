"""Fixture replacements win over retained callspec raw state."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.native import run_parameter_suite

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or pytest.__version__ != "9.1.1",
    reason="dense reader differential requires Unix and pinned pytest 9.1.1",
)

_SOURCE = """\
import gc
import json
import os
from pathlib import Path
import pytest
from _pytest.python import CallSpec2
from _pytest.scope import Scope

state = {'events': []}

def replace_live_dictionary():
    if os.environ['AUDIT_READER_MODE'] == 'getter':
        original_getter = CallSpec2.__getattribute__
        state['original_getter'] = original_getter
        def getter(callspec, name):
            if name == 'params':
                return {'value': 101}
            return original_getter(callspec, name)
        CallSpec2.__getattribute__ = getter
        state['events'].append('replace-getter')
        return
    replacement = {
        'params': {'value': 101},
        'indices': {'value': 1001},
        '_arg2scope': {'value': Scope.Module},
        '_idlist': ['replacement'],
        'marks': [],
    }
    object.__setattr__(state['callspec'], '__dict__', replacement)
    assert CallSpec2.__dict__['__dict__'].__get__(state['callspec']) is replacement
    state['events'].append('replace')

@pytest.fixture(autouse=True)
def replace_callspec_state(request):
    assert gc.isenabled()
    spec = request.node.callspec
    raw = vars(spec)
    state.update(callspec=spec, oldraw=raw)
    replace_live_dictionary()
    try:
        yield
    finally:
        if os.environ['AUDIT_READER_MODE'] == 'getter':
            CallSpec2.__getattribute__ = state['original_getter']

@pytest.fixture
def value(request):
    assert state['events'], 'fixture replacement did not activate'
    result = [request.param, request.scope, state['callspec'].indices['value']]
    Path('observed.json').write_text(json.dumps([state['events'], result]))
    return result

@pytest.mark.parametrize('value', [0], indirect=True)
def test_live_value(value):
    expected = (
        [101, 'function', 0]
        if os.environ['AUDIT_READER_MODE'] == 'getter'
        else [101, 'module', 1001]
    )
    assert value == expected
"""


@pytest.mark.parametrize("mode", ["dictionary", "getter"])
def test_dense_reader_uses_live_state_after_fixture_replacement(tmp_path: Path, mode: str) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_audit.py", _SOURCE)
        project.write(
            "conftest.py",
            "import gc\nimport os\n"
            "def pytest_collection():\n    gc.disable()\n"
            "def pytest_collection_finish(session):\n"
            "    gc.enable()\n",
        )
        environment = {
            "AUDIT_READER_ROUTE": route,
            "AUDIT_READER_MODE": mode,
            "VFY_WORKER_START": "exec",
        }
        result = run_parameter_suite(project, route, env=environment)
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "1 passed" in output, output
        observations[route] = json.loads((project.root / "observed.json").read_text())
    expected = (
        [["replace-getter"], [101, "function", 0]]
        if mode == "getter"
        else [["replace"], [101, "module", 1001]]
    )
    assert observations["native"] == observations["pytest"] == expected
