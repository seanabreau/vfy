"""Public differential for the native getter's binding and callback order."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.native import run_parameter_suite

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or pytest.__version__ != "9.1.1",
    reason="deferred callspec adapter requires Unix and pinned pytest 9.1.1",
)

_ATTRIBUTE_NAMES = """\
import json
from pathlib import Path
import pytest

class Name(str):
    __hash__ = str.__hash__
    def __eq__(self, other):
        assert other != '__dict__', 'attribute dispatch called rich equality'
        return True

class InvalidName:
    def __eq__(self, other):
        raise AssertionError('invalid attribute name called rich equality')

class DictionaryName(str):
    __hash__ = str.__hash__
    def __eq__(self, other):
        self.events.append(other)
        if self.mode == 'raises':
            raise RuntimeError('dictionary name equality')
        return self.mode == 'equal'

@pytest.mark.parametrize('value', [1, 2])
def test_attribute_names(value, request):
    callspec = request.node.callspec
    params = callspec.__getattribute__(Name('params'))
    assert params == {'value': value}
    assert params is callspec.params
    dictionary_lookups = []
    for mode in ('unequal', 'raises', 'equal'):
        def lookup(getter):
            name = DictionaryName('__dict__')
            name.mode, name.events = mode, []
            try:
                raw = getter(name)
            except AttributeError as error:
                assert mode != 'equal'
                return name.events, [type(error).__name__, str(error)], None
            assert mode == 'equal', 'dictionary lookup ignored name equality'
            return name.events, ['dictionary'], raw

        # Hash probes can revisit a class key. Use the same object and table
        # layout for the exact CPython callback-order oracle.
        getter = callspec.__getattribute__
        expected_events, expected_outcome, expected_raw = lookup(
            lambda name: object.__getattribute__(callspec, name)
        )
        events, outcome, raw = lookup(getter)
        assert events == expected_events
        assert outcome == expected_outcome
        if mode == 'equal':
            assert raw is expected_raw is vars(callspec) and raw['params'] is params
            assert set(raw) == {'params', 'indices', '_arg2scope', '_idlist', 'marks'}
        dictionary_lookups.append([mode, outcome])
    failures = []
    for name in (InvalidName(), 17, b'params', '\\ud800'):
        try:
            callspec.__getattribute__(name)
        except (TypeError, AttributeError) as error:
            failures.append([type(error).__name__, str(error)])
        else:
            raise AssertionError('invalid or missing attribute accepted')
    Path(f'observed-{value}.json').write_text(json.dumps([params, dictionary_lookups, failures]))
"""

_ATTRIBUTE_OBSERVER = """\
import gc
import json
import os
from pathlib import Path
from _pytest.python import CallSpec2

def pytest_collection():
    gc.disable()

def pytest_collection_finish(session):
    gc.enable()
"""


def test_callspec_attribute_names_match_stock_pytest(tmp_path: Path) -> None:
    for seed in ("0", "23", "26"):
        observations = {}
        for route in ("pytest", "native"):
            project = VfyProject(tmp_path / seed / route)
            project.write("test_attribute_names.py", _ATTRIBUTE_NAMES)
            project.write("conftest.py", _ATTRIBUTE_OBSERVER)
            result = run_parameter_suite(
                project,
                route,
                env={
                    "GETTER_ROUTE": route,
                    "VFY_WORKER_START": "exec",
                    "PYTHONHASHSEED": seed,
                },
            )
            output = result.stdout + result.stderr
            assert result.returncode == 0, output
            assert "2 passed" in output, output
            observations[route] = [
                json.loads((project.root / f"observed-{value}.json").read_text())
                for value in (1, 2)
            ]
        assert observations["native"] == observations["pytest"]


_AUDIT_SOURCE = """\
import gc
import json
import os
import sys
from pathlib import Path
import pytest
from _pytest.python import CallSpec2

state = {'armed': False, 'getter': None}

def audit(event, args):
    if (state['armed'] and event == 'object.__getattr__'
        and args[0] is state['getter'] and args[1] == '__code__'):
        raise PermissionError('fixture audit policy forbids inspecting its getter code')

sys.addaudithook(audit)

@pytest.fixture(autouse=True)
def getter_audit_policy(request):
    assert gc.isenabled()
    spec = request.node.callspec
    assert type(spec) is CallSpec2
    raw = CallSpec2.__dict__['__dict__'].__get__(spec)
    state.update(armed=True, getter=CallSpec2.__dict__.get('__getattribute__'), raw=raw)
    try:
        yield
    finally:
        state['armed'] = False

@pytest.fixture
def value(request):
    assert gc.isenabled()
    result = [request.param, request.scope]
    Path('observed.json').write_text(json.dumps(result))
    return result

@pytest.mark.parametrize('value', [17], indirect=True)
def test_fixture_with_restrictive_getter_audit(value):
    assert value == [17, 'function']
"""


def test_dense_fixture_read_respects_restrictive_getter_audit_policy(tmp_path: Path) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_audit.py", _AUDIT_SOURCE)
        project.write(
            "conftest.py",
            "import gc\n"
            "def pytest_collection():\n    gc.disable()\n"
            "def pytest_collection_finish(session):\n    gc.enable()\n",
        )
        environment = {
            "GETTER_ROUTE": route,
        }
        result = run_parameter_suite(project, route, env=environment)
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "1 passed" in output, output
        observations[route] = json.loads((project.root / "observed.json").read_text())
    assert observations["native"] == observations["pytest"] == [17, "function"]
