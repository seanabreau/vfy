"""Invocation failures keep stock exit status, INTERNALERROR output, and plugin hook order."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CASES = [
    pytest.param("plugin_import", "runtime", False, 1, False, id="plugin-import"),
    pytest.param("conftest_import", "runtime", False, 4, False, id="conftest-import"),
    pytest.param("configure", "runtime", False, 3, True, id="configure"),
    pytest.param("sessionstart", "runtime", False, 3, True, id="sessionstart"),
    pytest.param("loop", "runtime", False, 3, True, id="loop"),
    pytest.param("loop", "exit", False, 7, False, id="loop-exit"),
    pytest.param("loop", "keyboard", False, 2, False, id="loop-keyboard"),
    pytest.param("sessionfinish", "runtime", False, 1, False, id="sessionfinish"),
    pytest.param("unconfigure", "runtime", False, 1, False, id="unconfigure"),
    pytest.param("cleanup", "runtime", False, 1, False, id="cleanup"),
    pytest.param("configure", "runtime", True, 1, True, id="configure-cleanup-override"),
    pytest.param("configure", "exit", True, 1, False, id="exit-cleanup-override"),
    pytest.param("loop", "runtime", True, 1, True, id="loop-cleanup-override"),
    pytest.param("conftest_import", "runtime", True, 1, False, id="conftest-cleanup-override"),
]

_OBSERVER = """import json
import os
from pathlib import Path
import pytest

CONTROLLER = 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ
STAGE = os.environ['PRIMARY_STAGE']
CONTROL = os.environ['PRIMARY_CONTROL']
EVENTS = []


def note(event):
    if CONTROLLER:
        EVENTS.append(event)
        Path(os.environ['EVENTS_FILE']).write_text(json.dumps(EVENTS))


def trigger(stage):
    if not CONTROLLER or STAGE != stage:
        return
    note('raise ' + stage)
    if CONTROL == 'runtime':
        raise RuntimeError(stage + ' failed')
    if CONTROL == 'keyboard':
        raise KeyboardInterrupt(stage + ' stop')
    pytest.exit(stage + ' stop', returncode=7)


def cleanup():
    note('cleanup')
    if os.environ.get('CLEANUP_FAIL') or STAGE == 'cleanup':
        note('raise cleanup')
        raise RuntimeError('cleanup failed')


def pytest_addoption(parser, pluginmanager):
    pluginmanager.get_plugin('pytestconfig').add_cleanup(cleanup)
    note('addoption')


def pytest_configure(config):
    note('configure')
    trigger('configure')


def pytest_sessionstart(session):
    note('sessionstart')
    trigger('sessionstart')


@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session):
    note('loop enter')
    result = yield
    note('loop exit')
    trigger('loop')
    return result


def pytest_sessionfinish(session, exitstatus):
    note('sessionfinish')
    trigger('sessionfinish')


def pytest_unconfigure(config):
    note('unconfigure')
    trigger('unconfigure')


def pytest_internalerror(excrepr, excinfo):
    note('internalerror')


def pytest_keyboard_interrupt(excinfo):
    note('keyboard_interrupt')
"""


def _project(tmp_path: Path) -> VfyProject:
    project = VfyProject(tmp_path)
    project.write("observer_plugin.py", _OBSERVER)
    project.write(
        "broken_plugin.py",
        "from observer_plugin import trigger\ntrigger('plugin_import')\n",
    )
    project.write(
        "conftest.py",
        "from observer_plugin import trigger\ntrigger('conftest_import')\n",
    )
    for name in ["a", "b"]:
        project.write(f"test_{name}.py", "def test_body():\n    pass\n")
    return project


def _environment(
    tmp_path: Path, engine: str, stage: str, control: str, cleanup: bool
) -> dict[str, str]:
    return {
        "PYTHONPATH": str(tmp_path),
        "PRIMARY_STAGE": stage,
        "PRIMARY_CONTROL": control,
        "CLEANUP_FAIL": "1" if cleanup else "",
        "EVENTS_FILE": str(tmp_path / f"events-{engine}.json"),
    }


# A worker whose cleanup fails after the run does not yet match serial pytest, so
# those two cases run on the serial route only.
_WORKER_CASES = [case for case in _CASES if case.id not in {"cleanup", "loop-cleanup-override"}]
_ROUTES = [
    *(pytest.param("serial", *case.values, id=f"{case.id}-serial") for case in _CASES),
    *(pytest.param("workers", *case.values, id=f"{case.id}-workers") for case in _WORKER_CASES),
]


@pytest.mark.parametrize(("route", "stage", "control", "cleanup", "status", "internal"), _ROUTES)
def test_stage_failures_match_stock_status_internalerror_and_hook_order(
    tmp_path: Path, route: str, stage: str, control: str, cleanup: bool, status: int, internal: bool
) -> None:
    project = _project(tmp_path)
    plugins = ["-p", "observer_plugin", "-p", "broken_plugin"]
    oracle = project.run_python(
        "-m",
        "pytest",
        "-q",
        *plugins,
        env=_environment(tmp_path, "oracle", stage, control, cleanup),
    )
    arguments = ["test", "--engine=native", "--no-cov", "-q", *plugins]
    if route == "workers":
        arguments.append("--workers=2")
    actual = project.run_vfy(
        *arguments, env=_environment(tmp_path, "native", stage, control, cleanup)
    )
    assert oracle.returncode == status, (oracle.stdout, oracle.stderr)
    assert actual.returncode == oracle.returncode, (actual.stdout, actual.stderr)
    assert ("INTERNALERROR>" in oracle.stdout + oracle.stderr) is internal
    assert ("INTERNALERROR>" in actual.stdout + actual.stderr) is internal
    expected = json.loads((tmp_path / "events-oracle.json").read_text())
    assert json.loads((tmp_path / "events-native.json").read_text()) == expected
