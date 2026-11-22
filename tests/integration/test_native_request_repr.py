"""Compare native request representations and their callbacks with pytest."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

pytestmark = requires_native_pytest


def test_native_request_repr_matches_live_and_retained_stock_requests(tmp_path: Path) -> None:
    source = """
import atexit, json
from pathlib import Path
import pytest

observations = {'cleanup': {}}
saved = []
quoted_name = "quoted'\\\\name Ω"

def publish():
    observations['after_runtime'] = [repr(request) for request in saved]
    Path('repr.json').write_text(json.dumps(observations))

atexit.register(publish)

@pytest.fixture
def plain():
    return 31

@pytest.fixture(name=quoted_name)
def quoted(request):
    yield request
    observations['cleanup']['quoted'] = repr(request)

@pytest.fixture(scope='module')
def module_request(request):
    yield request
    observations['cleanup']['module'] = repr(request)

@pytest.fixture(scope='session')
def session_request(request):
    yield request
    observations['cleanup']['session'] = repr(request)

def test_case(request, module_request, session_request):
    requests = [request, request.getfixturevalue(quoted_name), module_request, session_request]
    saved.extend(requests)
    observations['normal'] = [repr(value) for value in requests]
    original = pytest.Function.__repr__
    mode = {'value': 'reentrant'}
    calls = []
    def item_repr(item):
        calls.append(item.nodeid)
        if mode['value'] == 'error':
            raise ValueError('item repr sentinel')
        if mode['value'] == 'reentrant':
            assert request.getfixturevalue('plain') == 31
        if mode['value'] == 'surrogate':
            return '<LiveItem \\ud800>'
        return f'<LiveItem {item.name}>'
    pytest.Function.__repr__ = item_repr
    try:
        observations['reentrant'] = [repr(value) for value in requests]
        assert calls == [request.node.nodeid] * 4
        observations['errors'] = []
        mode['value'] = 'error'
        for value in requests:
            with pytest.raises(ValueError, match='item repr sentinel') as error:
                repr(value)
            observations['errors'].append([type(error.value).__name__, str(error.value)])
        mode['value'] = 'surrogate'
        observations['surrogate'] = [repr(value) for value in requests]
    finally:
        pytest.Function.__repr__ = original
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_case.py", source)
        completed = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        completed.expect(0)
        observed[engine] = json.loads((project.root / "repr.json").read_text())
    assert observed["pytest-native"] == observed["pytest"]
    stock = observed["pytest"]
    assert stock["normal"][0] == "<FixtureRequest for <Function test_case>>"
    assert stock["normal"][2:] == [
        "<SubRequest 'module_request' for <Function test_case>>",
        "<SubRequest 'session_request' for <Function test_case>>",
    ]
    assert stock["after_runtime"] == stock["normal"]
    assert set(stock["cleanup"]) == {"quoted", "module", "session"}
    assert stock["errors"] == [["ValueError", "item repr sentinel"]] * 4
    assert all("\ud800" in value for value in stock["surrogate"])
