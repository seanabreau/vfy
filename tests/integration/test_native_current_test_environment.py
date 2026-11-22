"""Compare pytest's current-test environment without project phase hooks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


_SOURCE = r"""import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

SCENARIO = SCENARIO_VALUE

def record(event, nodeid):
    with Path(EVENTS_VALUE).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps([event, nodeid, os.environ.get('PYTEST_CURRENT_TEST')]) + '\n')

@pytest.fixture
def value(request):
    nodeid = request.node.nodeid
    record('setup', nodeid)
    request.addfinalizer(lambda: record('finalizer', nodeid))
    if SCENARIO == 'setup':
        raise ValueError('current-test setup failure')
    yield nodeid
    record('teardown', nodeid)
    if SCENARIO == 'teardown':
        raise ValueError('current-test teardown failure')

def test_case(value):
    record('call', value)
    child = subprocess.check_output(
        [sys.executable, '-c', "import os; print(os.environ['PYTEST_CURRENT_TEST'])"],
        text=True,
    ).strip()
    assert child == value + ' (call)'
    record('child', value)
    if SCENARIO == 'call':
        raise ValueError('current-test call failure')
"""


type Event = tuple[str, str, str | None]


def _read_events(project: VfyProject, filename: str) -> list[Event]:
    events: list[Event] = []
    for line in (project.root / filename).read_text().splitlines():
        row = json.loads(line)
        events.append((row[0], row[1], row[2]))
    return events


@pytest.mark.parametrize("scenario", ["pass", "setup", "call", "teardown"])
def test_native_current_test_environment_matches_pytest_without_phase_hooks(
    tmp_path: Path, scenario: str
) -> None:
    observed: dict[str, tuple[int, list[Event], str | None, list[Event]]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            "import json, os\nfrom pathlib import Path\n"
            "def pytest_sessionfinish():\n"
            "    value = os.environ.get('PYTEST_CURRENT_TEST')\n"
            "    Path('finished.json').write_text(json.dumps(value))\n",
        )
        project.write(
            "test_case.py",
            _SOURCE.replace("SCENARIO_VALUE", repr(scenario)).replace(
                "EVENTS_VALUE", "'events.jsonl'"
            ),
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=native", "--workers=1", "--no-replay", "--no-cov", "-q"
            )
        )
        assert result.returncode == (0 if scenario == "pass" else 1), result.stdout + result.stderr
        events = _read_events(project, "events.jsonl")
        assert events, result.stdout + result.stderr
        finished = json.loads((project.root / "finished.json").read_text())
        # A later public invocation must use its own case and clear successful teardown.
        project.write(
            "test_case.py",
            _SOURCE.replace("SCENARIO_VALUE", "'pass'").replace("EVENTS_VALUE", "'recovery.jsonl'"),
        )
        recovered = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=native", "--workers=1", "--no-replay", "--no-cov", "-q"
            )
        )
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        assert json.loads((project.root / "finished.json").read_text()) is None
        observed[engine] = (
            result.returncode,
            events,
            finished,
            _read_events(project, "recovery.jsonl"),
        )

    assert observed["pytest-native"] == observed["pytest"]
    for event, nodeid, current in observed["pytest"][1]:
        phase = (
            "setup" if event == "setup" else "call" if event in {"call", "child"} else "teardown"
        )
        assert current == f"{nodeid} ({phase})"


def test_native_workers_inherit_current_test_environment_without_phase_hooks(
    tmp_path: Path,
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        for name in ("a", "b"):
            project.write(
                f"test_{name}.py",
                _SOURCE.replace("SCENARIO_VALUE", "'pass'").replace(
                    "EVENTS_VALUE", repr(f"{name}.jsonl")
                ),
            )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=native", "--workers=2", "--no-replay", "--no-cov", "-q"
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observed[engine] = [_read_events(project, f"{name}.jsonl") for name in ("a", "b")]
    assert observed["pytest-native"] == observed["pytest"]


def test_current_test_uses_imported_os_module_after_sys_modules_replacement(
    tmp_path: Path,
) -> None:
    source = """import os
import sys
import types
import pytest

@pytest.fixture
def replace_os():
    original = sys.modules['os']
    decoy = types.ModuleType('os')
    decoy.environ = {}
    yield decoy
    try:
        assert os.environ['PYTEST_CURRENT_TEST'].endswith(' (teardown)')
        assert not decoy.environ
    finally:
        sys.modules['os'] = original

def test_case(replace_os):
    sys.modules['os'] = replace_os
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_case.py", source)
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=pytest-native", "--workers=1", "--no-replay", "--no-cov", "-q"
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout, result.stdout + result.stderr
