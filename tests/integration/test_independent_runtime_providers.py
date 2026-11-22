"""Owned thread and fault-handler resources survive failure, reuse, and exclusions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize(
    ("mode", "initially_enabled"),
    [("repeat", False), ("repeat", True), ("disabled", False), ("configure_failure", False)],
)
def test_runtime_provider_resources_match_pytest_after_repeated_commands(
    tmp_path: Path, mode: str, initially_enabled: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "observer.py",
        """import atexit
import faulthandler
import json
import os
import threading
from pathlib import Path

events = []
acquired = []
mode = os.environ['PROVIDER_MODE']
original_enable = faulthandler.enable
failed_once = False

def previous(args):
    events.append(['previous', args.thread.name, str(args.exc_value)])

def enable(*args, **kwargs):
    global failed_once
    if 'file' in kwargs:
        acquired.append(kwargs['file'])
        if mode == 'configure_failure' and not failed_once:
            failed_once = True
            raise RuntimeError('FAULT_HANDLER_ACQUISITION_FAILED')
    return original_enable(*args, **kwargs)

threading.excepthook = previous
faulthandler.enable = enable

def pytest_sessionstart(session):
    manager = session.config.pluginmanager
    events.append(['providers',
        manager.getplugin('threadexception') is not None,
        manager.getplugin('faulthandler') is not None])

def record():
    if not acquired and not events:
        return
    closed = []
    for descriptor in acquired:
        try:
            os.fstat(descriptor)
        except OSError:
            closed.append(True)
        else:
            closed.append(False)
    faulthandler.enable = original_enable
    Path(f'observations-{os.getpid()}.json').write_text(json.dumps(
        [*events, ['exit', threading.excepthook is previous, faulthandler.is_enabled(), closed]]))

atexit.register(record)
""",
    )
    project.write(
        "test_case.py",
        """import threading

def explode():
    raise ValueError('owned thread failure')

def test_case():
    thread = threading.Thread(target=explode, name='owned-thread')
    thread.start()
    thread.join()
""",
    )
    observed = []
    arguments = [
        "-q",
        "-p",
        "observer",
        "-W",
        "ignore::pytest.PytestUnhandledThreadExceptionWarning",
        "test_case.py",
    ]
    if mode == "disabled":
        arguments += ["-p", "no:threadexception", "-p", "no:faulthandler"]
    environment = {
        "PROVIDER_MODE": mode,
        "PYTHONFAULTHANDLER": "1" if initially_enabled else None,
        "PYTHONPATH": str(tmp_path),
    }
    for engine in ("stock", "native"):
        for stale in tmp_path.glob("observations-*.json"):
            stale.unlink()
        result = project.run_engine(engine, *arguments, env=environment)
        files = sorted(tmp_path.glob("observations-*.json"))
        assert len(files) == 1, result.stdout + result.stderr
        events = json.loads(files[0].read_text())
        assert events
        observed.append((result.returncode, events))
    assert observed[1] == observed[0]
    status, events = observed[0]
    assert status == (3 if mode == "configure_failure" else 0)
    final = events[-1]
    assert final[1] is True
    assert final[2] is initially_enabled
    assert all(final[3])
    assert any(event[0] == "previous" for event in events) is (mode == "disabled")
