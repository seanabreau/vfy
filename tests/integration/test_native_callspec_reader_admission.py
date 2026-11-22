"""A customized CallSpec2 getter keeps controlling parameter reads."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.native import run_parameter_suite

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or pytest.__version__ != "9.1.1",
    reason="dense callspec adapters require Unix and pinned pytest",
)

_EARLY_GETTER = """\
import os
from _pytest.python import CallSpec2

state = {'armed': False}
edge = os.environ['GETTER_EDGE']
if edge == 'early_getter':
    original = CallSpec2.__getattribute__
    def customized(callspec, name):
        result = original(callspec, name)
        if state['armed'] and name == 'params' and 'value' in result:
            return {'value': 101}
        return result
    CallSpec2.__getattribute__ = customized
else:
    raise AssertionError(edge)
"""

_SOURCE = """\
import gc
import json
import os
from pathlib import Path
import pytest
from _pytest.python import CallSpec2

state = {}

@pytest.fixture(autouse=True)
def change(request):
    assert gc.isenabled()
    state['request'] = request
    edge = os.environ['GETTER_EDGE']
    if edge != 'late_getter':
        import sitecustomize
        sitecustomize.state['armed'] = True
        try:
            yield
        finally:
            sitecustomize.state['armed'] = False
        return
    original = CallSpec2.__getattribute__
    def customized(callspec, name):
        result = original(callspec, name)
        if name == 'params' and 'value' in result:
            return {'value': 101}
        return result
    CallSpec2.__getattribute__ = customized
    try:
        yield
    finally:
        CallSpec2.__getattribute__ = original

@pytest.fixture
def value(request):
    return request.param

@pytest.mark.parametrize('value', range(2), indirect=True)
def test_live_value(value):
    request = state['request']
    spec = request.node.callspec
    index = spec.indices['value']
    assert value == 101
    observed = [value, index, spec.params['value'], request.node.nodeid]
    Path(f'observed-{index}.json').write_text(json.dumps(observed))
"""


@pytest.mark.parametrize(
    "edge",
    [
        "early_getter",
        "late_getter",
    ],
)
def test_customized_callspec_getter_is_honored_for_parameter_reads(
    tmp_path: Path, edge: str
) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_values.py", _SOURCE)
        project.write(
            "conftest.py",
            "import gc\n"
            "def pytest_collection():\n    gc.disable()\n"
            "def pytest_collection_finish(session):\n    gc.enable()\n",
        )
        environment = {
            "GETTER_EDGE": edge,
            "GETTER_EDGE_ROUTE": route,
        }
        if edge == "early_getter":
            project.write("sitecustomize.py", _EARLY_GETTER)
            # Startup runs this before either route imports the owning adapter.
            environment["PYTHONPATH"] = str(project.root)
        completed = run_parameter_suite(project, route, env=environment)
        output = completed.stdout + completed.stderr
        assert completed.returncode == 0, output
        assert "2 passed" in output, output
        observations[route] = [
            json.loads(path.read_text()) for path in sorted(project.root.glob("observed-*.json"))
        ]
        assert len(observations[route]) == 2
    assert observations["native"] == observations["pytest"]
