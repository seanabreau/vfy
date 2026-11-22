"""The native session restores its invocation directory at pytest's shutdown boundary."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_BRIDGE = """import atexit, json, os
from pathlib import Path

base = Path(__file__).parent
start = base / 'invocation'
changed = base / 'changed'
hook_directory = base / 'hook-directory'
cleanup_directory = base / 'cleanup-directory'
events = []
class HookError(Exception): pass
class CleanupError(Exception): pass
hook_error = HookError('hook failed')
cleanup_error = CleanupError('cleanup failed')

def label(path):
    path = Path(path)
    try: return path.relative_to(base).as_posix()
    except ValueError: return str(path)

def observe(event): events.append([event, label(Path.cwd())])

def _write():
    # Runs once the command's main function has returned or raised.
    observe('returned')
    (base / 'observations.json').write_text(json.dumps(events))

atexit.register(_write)
"""

_PLUGIN = """import os, sys
import bridge
import pytest

def pytest_configure(config):
    bridge.events.append(['rootpath', bridge.label(config.rootpath)])
    def cleanup():
        bridge.observe('cleanup')
        if bridge.mode in ['cleanup_error', 'restore_cleanup_error',
                           'configure_restore_cleanup_error']:
            raise bridge.cleanup_error
    config.add_cleanup(cleanup)
    if bridge.mode in ['configure_error', 'configure_restore_error',
                       'configure_restore_cleanup_error']:
        os.chdir(bridge.changed)
        if bridge.mode != 'configure_error': bridge.start.rmdir()
        bridge.observe('configure_error')
        raise bridge.hook_error

def pytest_sessionstart(session):
    if bridge.mode == 'startpath':
        bridge.events.append(['startpath', bridge.label(session.startpath)])
        try: session.startpath = bridge.changed
        except AttributeError: bridge.events.append(['startpath_readonly', True])
        else: bridge.events.append(['startpath_readonly', False])
    if bridge.mode == 'sessionstart_error':
        os.chdir(bridge.changed)
        bridge.observe('sessionstart_error')
        raise bridge.hook_error

@pytest.fixture
def resource(request):
    bridge.observe('fixture_setup')
    request.addfinalizer(lambda: bridge.observe('fixture_finalizer'))
    return 1

def pytest_sessionfinish(session, exitstatus):
    bridge.observe('sessionfinish')
    bridge.events.append(['finish_status', int(exitstatus)])
    if bridge.mode in ['finish_change', 'finish_error', 'cleanup_error']:
        os.chdir(bridge.hook_directory)
        bridge.observe('finish_changed')
        if bridge.mode != 'finish_change': raise bridge.hook_error

def pytest_unconfigure():
    bridge.observe('unconfigure')
    active = sys.exception()
    bridge.events.append(['unconfigure_error', type(active).__name__ if active else None])
    if bridge.mode == 'finish_change':
        os.chdir(bridge.cleanup_directory)
        bridge.observe('unconfigure_changed')

def pytest_internalerror(excinfo):
    bridge.events.append(['internalerror', type(excinfo.value).__name__])
    return True
"""

_BODY = """import os
import bridge
import pytest

def test_body(resource):
    bridge.observe('body_before')
    os.chdir(bridge.changed)
    bridge.observe('body_changed')
    if bridge.mode in ['restore_error', 'restore_cleanup_error']:
        bridge.start.rmdir()
    if bridge.mode in ['fail', 'maxfail']: assert False, 'body failed'
    if bridge.mode == 'exit': pytest.exit('cwd-exit', returncode=7)
    if bridge.mode == 'keyboard': raise KeyboardInterrupt
"""

_LAUNCHER = """import os, runpy, sys

os.chdir(sys.argv[1])
module = sys.argv[2]
sys.argv = [module, *sys.argv[3:]]
runpy.run_module(module, run_name='__main__', alter_sys=True)
"""


def _project(tmp_path: Path, mode: str) -> VfyProject:
    project = VfyProject(tmp_path)
    project.write("bridge.py", f"mode = {mode!r}\n" + _BRIDGE)
    project.write("suite/pytest.ini", "[pytest]\n")
    plugin = _PLUGIN
    if mode == "conftest_error":
        plugin = (
            "import os, bridge\nos.chdir(bridge.changed)\n"
            "bridge.observe('conftest_error')\nraise bridge.hook_error\n"
        )
    project.write("suite/conftest.py", plugin)
    body = _BODY
    if mode == "collection_error":
        body = (
            "import os, bridge\nos.chdir(bridge.changed)\n"
            "bridge.observe('collection_error')\nraise ValueError('collection failed')\n"
        )
    if mode == "maxfail":
        body += "\ndef test_later(): bridge.observe('later_body')\n"
    project.write("suite/test_body.py", body)
    project.write("launch.py", _LAUNCHER)
    for name in ("invocation", "changed", "hook-directory", "cleanup-directory"):
        (project.root / name).mkdir()
    return project


def _observe(project: VfyProject, *, native: bool, mode: str) -> dict[str, Any]:
    for name in ("invocation", "changed", "hook-directory", "cleanup-directory"):
        (project.root / name).mkdir(exist_ok=True)
    arguments = [
        "-p",
        "no:terminal",
        "-c",
        str(project.root / "suite/pytest.ini"),
        str(project.root / "suite/test_body.py"),
        *(["-x"] if mode == "maxfail" else []),
    ]
    command = ["-m", "vfy", "test", "--engine=native", "--no-cov"] if native else ["-m", "pytest"]
    module = "vfy" if native else "pytest"
    result = project.run_python(
        str(project.root / "launch.py"),
        str(project.root / "invocation"),
        module,
        *command[2:],
        *arguments,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONPATH": str(project.root)},
    )
    path = project.root / "observations.json"
    assert path.is_file(), result.stdout + result.stderr
    observed: dict[str, Any] = {"status": result.returncode, "events": json.loads(path.read_text())}
    path.unlink()
    traceback = re.findall(r"^([\w.]+)(?:: .*)?$", result.stderr, flags=re.MULTILINE)
    if result.returncode == 1 and "Traceback" in result.stderr:
        observed["raised"] = traceback[-1].rsplit(".", 1)[-1]
    return observed


@pytest.mark.parametrize(
    "mode",
    [
        "pass",
        "fail",
        "maxfail",
        "exit",
        "keyboard",
        "collection_error",
        "configure_error",
        "sessionstart_error",
        "finish_change",
        "finish_error",
        "cleanup_error",
        "restore_error",
        "restore_cleanup_error",
        "conftest_error",
        "startpath",
        "configure_restore_error",
        "configure_restore_cleanup_error",
    ],
)
def test_session_directory_matches_reference_shutdown_boundary(tmp_path: Path, mode: str) -> None:
    project = _project(tmp_path, mode)
    expected = _observe(project, native=False, mode=mode)
    actual = _observe(project, native=True, mode=mode)
    assert actual == expected
    events = expected["events"]
    assert ["rootpath", "suite"] in events or mode == "conftest_error"
    if mode in {"pass", "fail", "maxfail", "finish_change", "finish_error", "cleanup_error"}:
        assert ["fixture_finalizer", "changed"] in events
    if mode in {"exit", "keyboard"}:
        assert ["fixture_finalizer", "invocation"] in events
    restore_failures = {
        "restore_error",
        "restore_cleanup_error",
        "configure_restore_error",
        "configure_restore_cleanup_error",
    }
    if mode not in {"conftest_error", *restore_failures}:
        assert ["unconfigure", "invocation"] in events or mode in {
            "finish_change",
            "finish_error",
            "cleanup_error",
        }
    if mode in {*restore_failures, "configure_error", "sessionstart_error"}:
        assert not any(event[0] == "sessionfinish" for event in events)

    statuses = {
        "pass": 0,
        "fail": 1,
        "maxfail": 1,
        "exit": 7,
        "keyboard": 2,
        "collection_error": 2,
        "configure_error": 3,
        "sessionstart_error": 3,
        "finish_change": 0,
        "conftest_error": 4,
        "startpath": 0,
    }
    if mode in statuses:
        assert expected["status"] == statuses[mode]
    else:
        assert expected["raised"] == (
            "HookError"
            if mode == "finish_error"
            else "FileNotFoundError"
            if mode in {"restore_error", "configure_restore_error"}
            else "CleanupError"
        )
    if mode in {"finish_change", "finish_error", "cleanup_error"}:
        assert ["unconfigure", "hook-directory"] in events
    if mode == "finish_change":
        assert events[-1] == ["returned", "cleanup-directory"]
    if mode in restore_failures:
        assert ["unconfigure_error", "FileNotFoundError"] in events
    if mode == "maxfail":
        assert not any(event[0] == "later_body" for event in events)
    if mode == "startpath":
        assert ["startpath", "invocation"] in events
        assert ["startpath_readonly", True] in events
