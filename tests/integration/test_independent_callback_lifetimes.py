"""Retained execution callbacks can be collected by another Python thread."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_STATE = """import gc
import os
import sys
import threading

cycles = []
finalized = []
thread_pairs = []
unraisables = []
worker_errors = []
closed_checks = 0
wrong_thread_checks = 0
native = os.environ.get('CALLBACK_ROUTE') == 'independent'

class Probe:
    def __init__(self, hook, nodeid):
        self.hook = hook
        self.nodeid = nodeid
    def __del__(self):
        finalized.append([self.hook, self.nodeid, threading.get_ident()])

def reap():
    global cycles, closed_checks
    retired, cycles = cycles, []
    expected = set()
    for entry in retired:
        if entry[2].endswith('::test_create'):
            expected.add((entry[1], entry[2]))
            if native:
                try:
                    entry[0]()
                except AssertionError as error:
                    assert 'no longer available' in str(error)
                    closed_checks += 1
                else:
                    raise AssertionError('closed callback accepted reentry')
    del entry
    assert len(expected) == 4
    owner_thread = threading.get_ident()
    offset = len(finalized)
    previous = sys.unraisablehook
    def observe(event):
        unraisables.append(str(event.exc_value))
        previous(event)
    def collect():
        global wrong_thread_checks
        try:
            thread_pairs.append([owner_thread, threading.get_ident()])
            assert threading.get_ident() != owner_thread
            for entry in retired:
                if native and entry[2].endswith('::test_create'):
                    try:
                        entry[0]()
                    except RuntimeError as error:
                        assert 'belongs to another thread' in str(error)
                        wrong_thread_checks += 1
                    else:
                        raise AssertionError('foreign thread invoked native callback')
            del entry
            retired.clear()
            gc.collect()
        except BaseException as error:
            worker_errors.append(repr(error))
    sys.unraisablehook = observe
    try:
        thread = threading.Thread(target=collect)
        thread.start()
        thread.join(10)
        assert not thread.is_alive()
    finally:
        sys.unraisablehook = previous
    assert worker_errors == []
    assert unraisables == []
    observed = {(hook, nodeid) for hook, nodeid, ident in finalized[offset:]
                if nodeid.endswith('::test_create') and ident != owner_thread}
    assert observed == expected
"""

_HOOKS = """import json
import os
from pathlib import Path
import observer

def before(name, methods, kwargs):
    if name not in {'pytest_runtest_protocol', 'pytest_runtest_setup',
                    'pytest_runtest_call', 'pytest_runtest_teardown'}:
        return
    callback = next(method.function for method in methods if method.plugin_name == 'runner')
    nodeid = kwargs['item'].nodeid
    cycle = [callback, name, nodeid, observer.Probe(name, nodeid)]
    cycle.append(cycle)
    observer.cycles.append(cycle)

def pytest_configure(config):
    config.pluginmanager.add_hookcall_monitoring(before, lambda *args: None)

def pytest_unconfigure():
    Path(f'observed-{os.getpid()}.json').write_text(json.dumps({
        'threads': observer.thread_pairs, 'finalized': observer.finalized,
        'unraisables': observer.unraisables, 'worker_errors': observer.worker_errors,
        'closed_checks': observer.closed_checks,
        'wrong_thread_checks': observer.wrong_thread_checks,
    }))
"""

_CASE = """import observer

def test_create():
    pass

def test_reap():
    observer.reap()
"""


@pytest.mark.parametrize("workers", [1, 2])
def test_retained_callbacks_release_on_background_thread_without_unraisables(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nfilterwarnings = error\n")
    project.write("observer.py", _STATE)
    project.write("conftest.py", _HOOKS)
    for name in ("first", "second"):
        project.write(f"test_{name}.py", _CASE)
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "--native-scheduling=file",
        "--native-result-json=native.json",
        "-q",
        env={"CALLBACK_ROUTE": "independent", "VFY_WORKER_START": "exec"},
    )
    diagnostic = result.stdout + result.stderr
    assert result.returncode == 0, diagnostic
    assert "Exception ignored" not in diagnostic, diagnostic
    native = json.loads((tmp_path / "native.json").read_text())
    assert native["counts"]["passed"] == native["collected"] == 4, diagnostic
    assert native["warnings"] == [], diagnostic


def test_phase_callback_runs_once_when_monitor_invokes_it_early(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """from pathlib import Path


def pytest_configure(config):
    def before(name, methods, kwargs):
        if name != 'pytest_runtest_call':
            return
        method = next(method for method in methods if method.plugin_name == 'runner')
        method.function()
        try:
            method.function()
        except AssertionError as error:
            assert 'no longer available' in str(error)
        else:
            raise AssertionError('completed phase callback ran twice')
        methods.remove(method)
        Path('callback-once').touch()

    config.pluginmanager.add_hookcall_monitoring(before, lambda *args: None)
""",
    )
    project.write(
        "test_body.py",
        """from pathlib import Path

def test_body():
    path = Path('body-count')
    path.write_text(str(int(path.read_text()) + 1) if path.exists() else '1')
""",
    )
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    result.expect(0)
    assert (tmp_path / "callback-once").is_file()
    assert (tmp_path / "body-count").read_text() == "1"


def test_phase_callback_remains_available_through_monitor_after(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """from pathlib import Path
callbacks = []

def pytest_configure(config):
    def before(name, methods, kwargs):
        if name != 'pytest_runtest_call':
            return
        method = next(method for method in methods if method.plugin_name == 'runner')
        callbacks.append(method.function)
        methods.remove(method)

    def after(outcome, name, methods, kwargs):
        if name != 'pytest_runtest_call':
            return
        callbacks.pop()()
        Path('after-called').touch()

    config.pluginmanager.add_hookcall_monitoring(before, after)
""",
    )
    project.write(
        "test_body.py", "from pathlib import Path\ndef test_body(): Path('body-ran').touch()\n"
    )
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    result.expect(0)
    assert (tmp_path / "after-called").is_file()
    assert (tmp_path / "body-ran").is_file()
