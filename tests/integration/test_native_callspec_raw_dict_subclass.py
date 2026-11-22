"""Real pytest CallSpec dictionary subclasses establish the public oracle."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.native import run_parameter_suite

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or pytest.__version__ != "9.1.1",
    reason="native callspec route requires Unix and pinned pytest",
)

_SOURCE = """\
import gc
import json
import os
from pathlib import Path
import pytest
from _pytest.python import CallSpec2

events = []
header = None

class Header(dict):
    def __contains__(self, key):
        events.append(['contains', key])
        return False
    def __getitem__(self, key):
        events.append(['getitem', key])
        if key == 'params':
            return {'value': 999}
        return dict.__getitem__(self, key)
    def __setitem__(self, key, value):
        events.append(['setitem', key])
        dict.__setitem__(self, key, {'value': 999} if key == 'params' else value)
    def __delitem__(self, key):
        events.append(['delitem', key])
        dict.__delitem__(self, key)
    def get(self, key, default=None):
        events.append(['get', key])
        return {'value': 999} if key == 'params' else dict.get(self, key, default)

def replace_collection_header(items):
    global header
    assert len(items) == 1
    spec = items[0].callspec
    assert type(spec) is CallSpec2
    # Read each public field first so the replacement is a complete user-owned
    # mapping rather than a copied private native anchor.
    fields = ('params', 'indices', '_arg2scope', '_idlist', 'marks')
    header = Header({name: getattr(spec, name) for name in fields})
    object.__setattr__(spec, '__dict__', header)
    events.clear()

@pytest.fixture
def value(request):
    assert gc.isenabled()
    return request.param

@pytest.mark.parametrize('value', [17], indirect=True)
def test_raw_header(value, request):
    assert value == 17, 'raw dictionary subclass altered ordinary fixture value'
    spec = request.node.callspec
    operation = os.environ['HEADER_OPERATION']
    if operation == 'read':
        result = [
            spec.params['value'], spec.indices['value'],
            spec._arg2scope['value'].value, spec.id, len(spec.marks),
        ]
    elif operation == 'set':
        replacement = {'value': 42}
        object.__setattr__(spec, 'params', replacement)
        result = [spec.params['value'], spec.params is replacement,
                  dict.__getitem__(header, 'params') is replacement]
    elif operation == 'dictionary':
        exposed = vars(spec)
        direct = spec.__dict__
        result = [exposed is header, direct is header, type(exposed).__name__,
                  list(dict.keys(exposed))]
    else:
        saved = spec.params
        object.__delattr__(spec, 'params')
        result = [not dict.__contains__(header, 'params')]
        if operation in ('missing_delete', 'missing_get'):
            try:
                if operation == 'missing_get':
                    getattr(spec, 'params')
                else:
                    object.__delattr__(spec, 'params')
            except (AttributeError, KeyError) as error:
                result.extend([type(error).__name__, error.args,
                               getattr(error, 'name', None), getattr(error, 'obj', None) is spec])
            else:
                raise AssertionError('missing attribute deletion succeeded')
        captured = list(events)
        object.__setattr__(spec, 'params', saved)
        Path('observed.json').write_text(json.dumps([result, captured]))
        return
    Path('observed.json').write_text(json.dumps([result, events]))
"""


@pytest.mark.parametrize(
    "operation", ["read", "set", "dictionary", "delete", "missing_delete", "missing_get"]
)
def test_public_callspec_raw_dictionary_subclass_matches_stock(
    tmp_path: Path, operation: str
) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_header.py", _SOURCE)
        project.write(
            "conftest.py",
            "def pytest_collection_modifyitems(items):\n"
            "    import test_header\n"
            "    test_header.replace_collection_header(items)\n",
        )
        environment = {
            "HEADER_OPERATION": operation,
        }
        result = run_parameter_suite(project, route, env=environment)
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "1 passed" in output, output
        observations[route] = json.loads((project.root / "observed.json").read_text())
    assert observations["native"] == observations["pytest"], observations
