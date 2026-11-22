"""Completed pytest items release request state at the protocol boundary."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        ("pass", 0),
        ("skip", 0),
        ("setup_error", 1),
        ("call_error", 1),
        ("maxfail", 1),
        ("interrupt_setup", 2),
        ("interrupt_call", 2),
        ("interrupt_teardown", 2),
    ],
)
def test_completed_items_clear_request_and_funcargs(tmp_path: Path, mode: str, status: int) -> None:
    conftest = """import json
from pathlib import Path
import pytest

events = []
saved = []

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session):
    Path('state.json').write_text(json.dumps({
        'items': [(item._request is False, item.funcargs is None) for item in session.items],
        'saved': [(request.node._request is False, request.node.funcargs is None)
                  for request in saved],
        'events': events,
    }))
"""
    source = f"""import pytest
from conftest import events, saved

mode = {mode!r}

@pytest.fixture
def value(request):
    saved.append(request)
    request.node.addfinalizer(lambda: events.append('node-finalizer'))
    if mode == 'interrupt_setup':
        raise KeyboardInterrupt()
    value = object()
    yield value
    assert request.getfixturevalue('value') is value
    events.append('fixture-finalizer')
    if mode == 'interrupt_teardown':
        raise KeyboardInterrupt()

@pytest.fixture
def checked(value):
    if mode == 'setup_error':
        raise RuntimeError('setup sentinel')
    return value

@pytest.mark.skipif(mode == 'skip', reason='skip sentinel')
def test_first(checked):
    if mode in ('call_error', 'maxfail'):
        raise RuntimeError('call sentinel')
    if mode == 'interrupt_call':
        raise KeyboardInterrupt()

def test_second(request):
    previous = request.session.items[0]
    assert previous._request is False
    assert previous.funcargs is None
    if saved:
        assert saved[0].node is previous
    events.append('second')
"""
    observations: dict[str, object] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", conftest)
        project.write("test_cleanup.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            *(["--maxfail=1"] if mode == "maxfail" else []),
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        project.write("stdout.txt", result.stdout)
        project.write("stderr.txt", result.stderr)
        assert result.returncode == status, result.stdout + result.stderr
        observation = json.loads((project.root / "state.json").read_text())
        observations[engine] = observation
        assert observation["items"][0] == [True, True]
        assert all(state == [True, True] for state in observation["saved"])
        expected_events = (
            []
            if mode == "skip"
            else (
                ["node-finalizer"]
                if mode == "interrupt_setup"
                else (
                    ["fixture-finalizer"]
                    if mode == "interrupt_teardown"
                    else ["fixture-finalizer", "node-finalizer"]
                )
            )
        )
        if mode not in {"maxfail", "interrupt_setup", "interrupt_call", "interrupt_teardown"}:
            expected_events.append("second")
        assert observation["events"] == expected_events
    assert observations["pytest-native"] == observations["pytest"]


def test_request_release_precedes_funcargs_and_protocol_cleanup(tmp_path: Path) -> None:
    conftest = """import json
from pathlib import Path
import pytest

events = []

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session):
    Path('events.json').write_text(json.dumps(events))
"""
    source = """import sys
import warnings
import pytest
from _pytest.assertion import util
from conftest import events

class Release:
    def __init__(self, node):
        self.node = node

    def __del__(self):
        events.append(('released', self.node._request is False,
                       self.node.funcargs is not None, util._reprcompare is not None))
        print('request-release-output')
        print('request-release-error', file=sys.stderr)
        warnings.warn('request-release-warning', UserWarning)

@pytest.fixture
def value(request):
    request.node._request = Release(request.node)
    yield
    events.append(('finalized', request.node._request is not False))

def test_first(value):
    pass

def test_second():
    events.append(('second',))
"""
    observations: dict[str, object] = {}
    captured: dict[str, list[tuple[str, str | None]]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", conftest)
        project.write("test_release.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--junitxml=result.xml",
            "-o",
            "junit_logging=all",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        project.write("stdout.txt", result.stdout)
        project.write("stderr.txt", result.stderr)
        result.expect(0)
        observations[engine] = json.loads((project.root / "events.json").read_text())
        assert result.stdout.count("request-release-output") == 1, result.stdout
        assert ".request-release-output" in result.stdout, result.stdout
        assert result.stderr.count("request-release-error") == 1, result.stderr
        assert "test_release.py::test_first" in result.stdout
        assert "UserWarning: request-release-warning" in result.stdout
        captured[engine] = [
            (child.tag, child.text)
            for case in ET.parse(project.root / "result.xml").iter("testcase")
            for child in case
            if child.tag in {"system-out", "system-err"}
        ]
    expected = [["finalized", True], ["released", True, True, True], ["second"]]
    assert observations["pytest"] == expected
    assert observations["pytest-native"] == expected
    assert captured["pytest-native"] == captured["pytest"]
    assert all("request-release-" not in (text or "") for _, text in captured["pytest"])


@pytest.mark.parametrize("interrupt", [False, True])
def test_failed_request_clear_is_not_retried(tmp_path: Path, interrupt: bool) -> None:
    conftest = """import json
from pathlib import Path
import pytest

events = []

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session):
    Path('events.json').write_text(json.dumps(events))
"""
    source = f"""from conftest import events

def test_cleanup(request):
    item_type = type(request.node)
    original = item_type.__setattr__
    def set_attribute(item, name, value):
        if name == '_request' and value is False:
            events.append('clear-request')
            raise RuntimeError('request-clear sentinel')
        original(item, name, value)
    item_type.__setattr__ = set_attribute
    if {interrupt!r}:
        raise KeyboardInterrupt()
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", conftest)
        project.write("test_error.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        project.write("stdout.txt", result.stdout)
        project.write("stderr.txt", result.stderr)
        result.expect(3)
        assert "request-clear sentinel" in result.stdout + result.stderr
        assert json.loads((project.root / "events.json").read_text()) == ["clear-request"]
