"""Early fallback keeps customized public fixture reads live."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.native import run_parameter_suite

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or pytest.__version__ != "9.1.1",
    reason="native reader requires Unix and pinned pytest",
)

_SOURCE = """\
import gc
import json
import os
from pathlib import Path
import pytest
from _pytest.python import CallSpec2

events = []

class Collision:
    def __hash__(self):
        return hash('params')
    def __eq__(self, other):
        events.append(['unexpected-key-equality', other])
        return False

@pytest.fixture(autouse=True)
def prepare_live_reads(request):
    assert gc.isenabled()
    spec = request.node.callspec
    params = spec.params
    indices = spec.indices
    params['value'] = 101
    indices['value'] = 1001
    original_getter = CallSpec2.__getattribute__
    raw = object.__getattribute__(spec, '__dict__')
    # An untrusted raw key must not receive an additional equality callback
    # while the live public getter supplies the authoritative params.
    del raw['params']
    raw[Collision()] = None
    events.clear()
    def getter(callspec, name):
        return params if name == 'params' else original_getter(callspec, name)
    CallSpec2.__getattribute__ = getter
    try:
        yield
    finally:
        raw['params'] = params
        CallSpec2.__getattribute__ = original_getter

@pytest.fixture
def value(request):
    assert events == [], events
    observed = [request.param, request.scope, request.node.callspec.indices['value']]
    Path('observed.json').write_text(json.dumps(observed))
    return observed

@pytest.mark.parametrize('value', [0], indirect=True)
def test_live_reads(value):
    assert value == [101, 'function', 1001]
"""


def test_dense_reader_prefilter_preserves_live_getters_and_callback_rejection(
    tmp_path: Path,
) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_live.py", _SOURCE)
        result = run_parameter_suite(project, route, env={})
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "1 passed" in output, output
        observations[route] = json.loads((project.root / "observed.json").read_text())
    assert observations["native"] == observations["pytest"] == [101, "function", 1001]
