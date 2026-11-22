"""Fixture metadata preserves dictionary callbacks and live code changes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.native import run_parameter_suite

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or sys.version_info[:2] != (3, 12) or pytest.__version__ != "9.1.1",
    reason="fixture exception-context oracle uses CPython 3.12 and pytest 9.1.1",
)

_SOURCE = """\
import gc
import json
import os
import sys
from pathlib import Path
import pytest
from _pytest.python import get_direct_param_fixture_func

state = {'caught': None, 'finalized': False, 'armed': False, 'direct': 0}
mode = os.environ['FIXTURE_METADATA_MODE']

class Metadata(dict):
    def __getitem__(self, name):
        raise AssertionError('function attribute lookup dispatched dict-subclass override')

class Missing(AttributeError):
    def __del__(self):
        state['finalized'] = True

class Collision:
    def __hash__(self):
        return hash('_is_coroutine_marker')
    def __eq__(self, other):
        raise Missing('foreign dictionary key attribute failure')

def audit(event, args):
    if event != 'object.__getattr__' or args[1] != '__code__':
        return
    if args[0] is get_direct_param_fixture_func:
        state['direct'] += 1
    if state['armed'] and args[0] is function:
        state['armed'] = False
        function.__dict__ = Metadata({'_force_asyncio_fixture': False})
        function.__code__ = replacement.__code__

sys.addaudithook(audit)

@pytest.fixture(autouse=True)
def arm():
    assert gc.isenabled()
    assert get_direct_param_fixture_func.__dict__ == {}, 'direct function lost empty activation'
    state['direct'] = 0
    if mode == 'empty':
        function.__dict__ = Metadata()
    elif mode == 'collision':
        function.__dict__ = Metadata({Collision(): None})
    elif mode == 'audit':
        state['armed'] = True
        if os.environ['FIXTURE_METADATA_ROUTE'] == 'pytest':
            # Trigger the same live code mutation before stock fixture setup.
            function.__code__

@pytest.fixture
def resource(arm, value):
    if mode == 'collision':
        try:
            getattr(function, '_is_coroutine_marker')
        except AttributeError as error:
            state['caught'] = [list(error.args), error.name, error.obj is function]
        else:
            raise AssertionError('foreign key error did not propagate for explicit lookup')
        gc.collect()
    return value + 100

function = resource.__wrapped__

def replacement(arm, value):
    return value + 200

@pytest.mark.parametrize('value', [17])
def test_live_fixture(resource, value):
    assert resource == (217 if mode == 'audit' else 117)
    if mode == 'collision':
        assert state['finalized']
    assert not state['armed']
    Path('observed.json').write_text(json.dumps([resource, state['caught'], state['finalized']]))
    activation = [state['direct'], not get_direct_param_fixture_func.__dict__]
    Path('activation.json').write_text(json.dumps(activation))
"""


@pytest.mark.parametrize("mode", ["empty", "collision", "audit"])
def test_empty_fixture_metadata_keeps_public_values_and_canonical_fallbacks(
    tmp_path: Path, mode: str
) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_metadata.py", _SOURCE)
        environment = {
            "FIXTURE_METADATA_ROUTE": route,
            "FIXTURE_METADATA_MODE": mode,
        }
        result = run_parameter_suite(project, route, env=environment)
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "1 passed" in output, output
        observations[route] = json.loads((project.root / "observed.json").read_text())
        if route == "native" and mode == "empty":
            reads, empty = json.loads((project.root / "activation.json").read_text())
            assert reads >= 2, reads
            assert empty
    assert observations["native"] == observations["pytest"]
