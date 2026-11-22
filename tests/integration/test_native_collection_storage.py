"""Preserve ordinary Function storage and customized collection readers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or sys.implementation.name != "cpython",
    reason="native collection storage requires Unix CPython",
)

_CASES = """\
import pytest

@pytest.mark.parametrize('value', [1, 2])
def test_parameter(value):
    assert value in (1, 2)

def test_plain():
    pass
"""

_STORAGE_OBSERVER = """\
import gc
import json
from collections import Counter
from pathlib import Path
from _pytest.fixtures import TopRequest
from _pytest.python import Function

def shape(value):
    # Reading fields or vars first would hide inline-storage promotion.
    return sorted(Counter(type(ref).__qualname__ for ref in gc.get_referents(value)).items())

def pytest_collection_finish(session):
    observations = []
    for item in session.items:
        assert type(item) is Function
        before = shape(item)
        request = item._request
        request_before = shape(request)
        assert type(request) is TopRequest
        assert request._pyfuncitem is item
        assert request._arg2fixturedefs is item._fixtureinfo.name2fixturedefs
        assert item._store is item.stash
        assert item.keywords.node is item and item.keywords.parent is item.parent
        raw = vars(item)
        assert raw is vars(item) and raw is item.__dict__
        assert raw['_request'] is request
        observations.append([item.nodeid, before, shape(item), request_before, list(raw)])
    Path('storage.json').write_text(json.dumps(observations))
"""

_READER_PREFIX = """\
import os
from _pytest.python import Function

armed = False
reads = []
mode = os.environ['COLLECTION_READER_MODE']
original_getattribute = Function.__getattribute__

if mode == 'getattribute':
    def read(self, name):
        if armed and name == 'callspec':
            reads.append(self.nodeid)
        return original_getattribute(self, name)
    Function.__getattribute__ = read
elif mode == 'getattr':
    def missing(self, name):
        if armed and name == 'callspec':
            reads.append(self.nodeid)
        raise AttributeError(name)
    Function.__getattr__ = missing
elif mode == 'descriptor':
    class Callspec:
        def __get__(self, instance, owner=None):
            if instance is None:
                return self
            if armed:
                reads.append(instance.nodeid)
            return object.__getattribute__(instance, '_stored_callspec')
        def __set__(self, instance, value):
            object.__setattr__(instance, '_stored_callspec', value)
    Function.callspec = Callspec()
else:
    raise AssertionError(mode)
"""

_READER_OBSERVER = """\
import json
from pathlib import Path
import pytest

@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    module = items[0].module
    module.reads.clear()
    module.armed = True

def pytest_collection_finish(session):
    module = session.items[0].module
    module.armed = False
    Path('readers.json').write_text(json.dumps([
        [item.nodeid for item in session.items], module.reads,
    ]))
"""

_CALLSPEC_READER_PREFIX = """\
import os
from _pytest.python import CallSpec2

armed = False
reads = []
mode = os.environ['COLLECTION_READER_MODE']
original_getattribute = CallSpec2.__getattribute__

if mode == 'callspec_getter':
    def read(self, name):
        if armed and name == 'indices':
            reads.append(name)
        return original_getattribute(self, name)
    CallSpec2.__getattribute__ = read
else:
    field = mode
    assert field in ('indices', '_arg2scope')
    class ScopeField:
        def __get__(self, instance, owner=None):
            if instance is None:
                return self
            if armed:
                reads.append(field)
            return original_getattribute(instance, '_stored_' + field)
        def __set__(self, instance, value):
            object.__setattr__(instance, '_stored_' + field, value)
    setattr(CallSpec2, field, ScopeField())
"""


def _run(project: VfyProject, route: str, environment: dict[str, str | None]) -> None:
    native_result = project.root / "native.json"
    environment = {
        **environment,
    }
    if route == "pytest":
        result = project.run_pytest("-q", load_xdist=False, env=environment)
    else:
        result = project.run_vfy(
            "test",
            "--engine=pytest-native",
            "--no-cov",
            "-q",
            env={**environment, "PYTEST_NATIVE_RESULT": str(native_result)},
        )
    result.expect(0)
    assert "3 passed" in result.stdout, result.stdout + result.stderr
    if route == "native":
        assert json.loads(native_result.read_text())["engine"] == "pytest-runner-native-v1"


@pytest.mark.skipif(
    pytest.__version__ != "9.1.1",
    reason="inline storage proof requires the pinned pytest constructor and request shape",
)
def test_default_collection_preserves_inline_storage_until_vars(tmp_path: Path) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_cases.py", _CASES)
        project.write("conftest.py", _STORAGE_OBSERVER)
        _run(project, route, {})
        observations[route] = json.loads((project.root / "storage.json").read_text())
    assert observations["native"] == observations["pytest"]


@pytest.mark.parametrize("mode", ["getattribute", "getattr", "descriptor"])
def test_custom_collection_readers_use_stock_sorter(tmp_path: Path, mode: str) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_cases.py", _READER_PREFIX + _CASES)
        project.write("conftest.py", _READER_OBSERVER)
        _run(project, route, {"COLLECTION_READER_MODE": mode})
        observations[route] = json.loads((project.root / "readers.json").read_text())
    assert observations["pytest"][1], observations
    assert observations["native"] == observations["pytest"]


@pytest.mark.parametrize("mode", ["callspec_getter", "indices", "_arg2scope"])
def test_callspec_scope_readers_keep_stock_sorter_callback_count(tmp_path: Path, mode: str) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_cases.py", _CALLSPEC_READER_PREFIX + _CASES)
        project.write("conftest.py", _READER_OBSERVER)
        _run(project, route, {"COLLECTION_READER_MODE": mode})
        observations[route] = json.loads((project.root / "readers.json").read_text())
    field = "indices" if mode == "callspec_getter" else mode
    # Two parameterized items, with one scope-field read in each of pytest's
    # four high-scope sorting passes. Admission must contribute no extra reads.
    # Pytest 9.2 reads scope directly and bypasses the indices mapping.
    reads = 0 if field == "indices" and tuple(map(int, pytest.version_tuple[:2])) >= (9, 2) else 8
    assert observations["pytest"][1] == [field] * reads, observations
    assert observations["native"] == observations["pytest"]
