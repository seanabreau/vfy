"""Builtin markers are available to real try-last configure hooks."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_OBSERVER = """import json
from pathlib import Path

events = []

def note(config, phase):
    events.append([phase, list(config.getini('markers'))])
    Path('markers.json').write_text(json.dumps(events))
"""
_EARLY = """import pytest
from marker_observer import note

@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    note(config, 'first')
"""
_NORMAL = """from marker_observer import note

def pytest_configure(config):
    note(config, 'normal')
"""
_LATE = """import pytest
from marker_observer import note

@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    note(config, 'last')
    markers = config.getini('markers')
    markers[:] = [marker + ' [configured]' for marker in markers]

def pytest_sessionstart(session):
    note(session.config, 'sessionstart')
"""
_BODY = """from marker_observer import note

def test_markers(request):
    note(request.config, 'body')
"""
_FILES = {
    "pytest.ini": "[pytest]\nmarkers = custom: declared by the project\n",
    "marker_observer.py": _OBSERVER,
    "early_plugin.py": _EARLY,
    "normal_plugin.py": _NORMAL,
    "late_plugin.py": _LATE,
    "test_markers.py": _BODY,
}


def test_builtin_marker_registration_preserves_configure_hook_priority(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    for name, source in _FILES.items():
        project.write(name, source)
    arguments = ["-p", "early_plugin", "-p", "normal_plugin", "-p", "late_plugin"]
    oracle = project.run_pytest("-q", *arguments, load_xdist=False)
    oracle.expect(0)
    observation = tmp_path / "markers.json"
    expected = json.loads(observation.read_text())
    assert [phase for phase, _ in expected] == [
        "first",
        "normal",
        "last",
        "sessionstart",
        "body",
    ]
    first, normal, last, session, body = [markers for _, markers in expected]
    assert first == normal == ["custom: declared by the project"]
    assert {marker.split(":", 1)[0].split("(", 1)[0] for marker in last} == {
        "custom",
        "filterwarnings",
        "skip",
        "skipif",
        "xfail",
        "parametrize",
        "usefixtures",
        "tryfirst",
        "trylast",
    }
    assert session == body == [marker + " [configured]" for marker in last]
    observation.unlink()
    actual = project.run_python("-m", "vfy.independent.entry", "-q", *arguments)
    actual.expect(0)
    assert json.loads(observation.read_text()) == expected
