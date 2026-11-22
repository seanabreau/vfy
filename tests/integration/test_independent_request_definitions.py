"""Active request definitions retain successful fixture bindings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_RECORDER = """import gc
import json
from pathlib import Path
import pytest

pytest_plugins = ['provider']
events = []
mappings = []
definitions = {}
latest = {}
requests = []

def observe(label, request):
    mapping = request._fixture_defs
    mappings.append(mapping)
    requests.append(request)
    assert mapping is request._fixture_defs
    assert mapping is request._pyfuncitem._request._fixture_defs
    assert list(mapping) == list(mapping.keys())
    assert len(mapping) == len(list(mapping))
    assert 'request' not in mapping
    assert mapping.get('not_requested') is None
    sentinel = object()
    assert mapping.get('not_requested', sentinel) is sentinel
    try:
        mapping['not_requested']
    except KeyError as error:
        assert error.args == ('not_requested',)
    else:
        raise AssertionError('missing definition did not raise KeyError')
    for name, definition in mapping.items():
        assert name in mapping
        assert mapping[name] is definition
        assert mapping.get(name) is definition
        assert definition in mapping.values()
        assert sum(value is definition for value in gc.get_referents(mapping)) == 1
    events.append([label, list(mapping),
                   [[name, definition.scope]
                    for name, definition in mapping.items()]])

@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    definitions[(fixturedef.argname, fixturedef.func.__module__)] = fixturedef
    latest[fixturedef.argname] = fixturedef
    observe('before:' + fixturedef.argname, request)
    try:
        result = yield
    except BaseException:
        observe('error:' + fixturedef.argname, request)
        raise
    observe('after:' + fixturedef.argname, request)
    return result

@pytest.fixture
def value(value, request):
    observe('override-body', request)
    assert request._fixture_defs['value'] is definitions[('value', 'provider')]
    assert value == 17
    return value + 1

@pytest.fixture
def ordered(ordered, request):
    assert list(request._fixture_defs) == ['ordered']
    assert request.getfixturevalue('late') == 29
    assert list(request._fixture_defs) == ['ordered', 'late']
    assert request._fixture_defs['ordered'] is definitions[('ordered', 'provider')]
    return ordered + 1

@pytest.fixture
def late(request):
    observe('late-body', request)
    assert 'late' not in request._fixture_defs
    request.addfinalizer(lambda: observe('late-finalizer', request))
    return 29

@pytest.fixture
def broken(request):
    observe('broken-body', request)
    raise ValueError('real setup failure')

def pytest_sessionfinish(session, exitstatus):
    assert mappings and requests
    assert exitstatus == 0
    for request, mapping in zip(requests, mappings):
        assert request._fixture_defs is mapping
        assert all(definition.cached_result is None for definition in mapping.values())
        for name, definition in mapping.items():
            assert mapping[name] is definition
            assert definition in gc.get_referents(mapping)
    events.append(['finish', int(exitstatus), [list(mapping) for mapping in mappings]])
    Path('observations.json').write_text(json.dumps(events))
"""
_PROVIDER = """import pytest
@pytest.fixture
def value(request):
    import conftest
    conftest.observe('base-body', request)
    assert 'value' not in request._fixture_defs
    return 17

@pytest.fixture
def ordered(): return 23
"""
_CASES = {
    "override_order": """import conftest

def test_body(ordered, request):
    assert ordered == 24
    mapping = request._fixture_defs
    assert list(mapping) == ['ordered', 'late']
    assert mapping['ordered'] is conftest.definitions[('ordered', 'conftest')]
    assert mapping['late'] is conftest.definitions[('late', 'conftest')]
    conftest.observe('test-order', request)
""",
    "override_dynamic": """import conftest

def test_body(value, request):
    mapping = request._fixture_defs
    assert mapping['value'] is conftest.definitions[('value', 'conftest')]
    assert list(mapping) == ['value']
    conftest.observe('test-before-dynamic', request)
    assert request.getfixturevalue('late') == 29
    assert request._fixture_defs is mapping
    assert list(mapping) == ['value', 'late']
    assert mapping['late'] is conftest.definitions[('late', 'conftest')]
    assert request.getfixturevalue('value') == 18
    assert list(mapping) == ['value', 'late']
    conftest.observe('test-after-dynamic', request)
""",
    "failed_dynamic": """import pytest
import conftest

def test_body(request):
    conftest.observe('test-before-failure', request)
    mapping = request._fixture_defs
    with pytest.raises(ValueError, match='real setup failure'):
        request.getfixturevalue('broken')
    assert 'broken' not in mapping
    assert list(mapping) == []
    conftest.observe('test-after-failure', request)
""",
    "direct_parameter": """import pytest
import conftest

@pytest.mark.parametrize('direct', [41])
def test_body(direct, request):
    assert direct == 41
    mapping = request._fixture_defs
    assert list(mapping) == ['direct']
    assert mapping['direct'] is conftest.latest['direct']
    conftest.observe('test-direct', request)
""",
    "distinct_requests": """import conftest

def test_first(request):
    assert list(request._fixture_defs) == []
    conftest.observe('first-before', request)
    assert request.getfixturevalue('late') == 29
    conftest.observe('first-after', request)

def test_second(request):
    previous = conftest.requests[0]._fixture_defs
    assert list(previous) == ['late']
    assert all(value.cached_result is None for value in previous.values())
    assert request._fixture_defs is not previous
    assert list(request._fixture_defs) == []
    conftest.observe('second-before', request)
    assert request.getfixturevalue('late') == 29
    conftest.observe('second-after', request)
""",
}


def _project(path: Path, scenario: str, *, interpreter: Path | None = None) -> VfyProject:
    project = VfyProject(path, interpreter=interpreter)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", _RECORDER)
    project.write("provider.py", _PROVIDER)
    project.write("test_case.py", _CASES[scenario])
    return project


@pytest.mark.parametrize("scenario", _CASES)
def test_active_fixture_definitions(tmp_path: Path, scenario: str) -> None:
    project = _project(tmp_path, scenario)
    observation = tmp_path / "observations.json"
    stock = project.run_python("-m", "pytest", "-q", "-p", "pytest_cov")
    project.write("stock.stdout", stock.stdout)
    project.write("stock.stderr", stock.stderr)
    stock.expect(0)
    assert observation.exists(), stock.stdout + stock.stderr
    expected = json.loads(observation.read_text())
    assert expected
    assert expected[-1][0:2] == ["finish", 0]
    project.write("stock-observations.json", json.dumps(expected, indent=2))
    observation.unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    project.write("candidate.stdout", candidate.stdout)
    project.write("candidate.stderr", candidate.stderr)
    project.write("statuses.json", json.dumps([stock.returncode, candidate.returncode]))
    candidate.expect(0)
    assert observation.exists(), candidate.stdout + candidate.stderr
    actual = json.loads(observation.read_text())
    project.write("candidate-observations.json", json.dumps(actual, indent=2))
    assert actual == expected


_RELEASE_CONFTEST = """import pytest
import holder

@pytest.fixture
def value(request):
    holder.request = request
    holder.mapping = request._fixture_defs
    return 31
"""
_RELEASE_RUNNER = """import gc
import json
from pathlib import Path
import runpy
import sys
import weakref
import holder

engine = sys.argv[1]
if engine == 'stock':
    sys.argv = ['pytest', '-q', '-p', 'pytest_cov']
    module = 'pytest'
else:
    sys.argv = ['vfy.independent.entry', '-q']
    module = 'vfy.independent.entry'
try:
    runpy.run_module(module, run_name='__main__', alter_sys=True)
except SystemExit as error:
    status = error.code
else:
    raise AssertionError('CLI did not exit')
assert status == 0
mapping = holder.mapping
assert holder.request._fixture_defs is mapping
assert list(mapping) == ['value']
binding = mapping['value']
assert binding.cached_result is None
assert mapping.get('value') is binding
assert list(mapping.items()) == [('value', binding)]
assert binding in gc.get_referents(mapping)

events = []
class CallableOwner:
    def __init__(self, owner): self.owner = owner
    def __call__(self): return 31
    def __del__(self):
        assert list(self.owner) == ['value']
        assert self.owner['value'].cached_result is None
        events.append('released-readable')
original = binding.func
callable_owner = CallableOwner(mapping)
reference = weakref.ref(callable_owner)
binding.func = callable_owner
del callable_owner
holder.request = holder.mapping = None
gc.collect()
assert reference() is not None
assert reference().owner is mapping
assert binding.func is reference()
binding.func = original
assert reference() is None
assert events == ['released-readable']
Path('released.json').write_text(json.dumps({'status': status, 'events': events}))
"""


def _release_project(path: Path, *, interpreter: Path | None = None) -> VfyProject:
    project = VfyProject(path, interpreter=interpreter)
    project.write("pytest.ini", "[pytest]\n")
    project.write("holder.py", "request = mapping = None\n")
    project.write("conftest.py", _RELEASE_CONFTEST)
    project.write("test_case.py", "def test_body(value): assert value == 31\n")
    project.write("runner.py", _RELEASE_RUNNER)
    return project


def test_completed_fixture_registry_follows_retained_requests(tmp_path: Path) -> None:
    observations = {}
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """import gc
import json
from pathlib import Path
import pytest

pending = None
saved = scoped = saved_value = None
events = []

@pytest.fixture
def disposable():
    return 17

@pytest.fixture(scope='session')
def shared():
    return object()

@pytest.fixture(scope='session')
def scope_owner(request):
    return request

@pytest.fixture(scope='session')
def late():
    events.append('late setup')
    yield 29
    events.append('late teardown')

def remember_disposable(request):
    global pending
    mapping = request._fixture_defs
    assert gc.is_tracked(mapping)
    pending = id(mapping), type(mapping), mapping['disposable']

def pytest_runtest_logfinish(nodeid):
    if not nodeid.endswith('::test_disposable'):
        return
    identity, mapping_type, binding = pending
    gc.collect()
    # Match the binding too: an unrelated mapping can reuse a released address.
    assert not any(id(value) == identity and type(value) is mapping_type
                   and value.get('disposable') is binding
                   for value in gc.get_objects())
    events.append('disposable registry released')

def pytest_sessionfinish():
    assert 'late' in saved.fixturenames
    assert 'late' in scoped.fixturenames
    Path('lifetime.json').write_text(json.dumps(events))
""",
        )
        project.write(
            "test_case.py",
            """import conftest

def test_disposable(disposable, request):
    assert disposable == 17
    conftest.remember_disposable(request)

def test_retain(scope_owner, request):
    conftest.saved = request
    conftest.scoped = scope_owner
    conftest.saved_value = request.getfixturevalue('shared')
    assert request._fixture_defs is scope_owner._fixture_defs

def test_saved():
    saved, scoped = conftest.saved, conftest.scoped
    assert saved._fixture_defs is scoped._fixture_defs
    assert saved.getfixturevalue('shared') is conftest.saved_value
    conftest.events.append('saved cached lookup')
    # A live session request can still create a fixture after its test ends.
    assert scoped.getfixturevalue('late') == 29
    assert saved._fixture_defs['late'] is scoped._fixture_defs['late']
    assert 'late' in saved.fixturenames
    conftest.events.append('saved dynamic lookup')
""",
        )
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        if engine == "stock":
            result = project.run_python("-m", "pytest", "-q", env=env)
        else:
            result = project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=env)
        result.expect(0)
        observations[engine] = json.loads((project.root / "lifetime.json").read_text())
    assert (
        observations["native"]
        == observations["stock"]
        == [
            "disposable registry released",
            "saved cached lookup",
            "late setup",
            "saved dynamic lookup",
            "late teardown",
        ]
    )
