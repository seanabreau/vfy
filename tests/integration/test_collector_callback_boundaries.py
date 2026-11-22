"""Live collector callbacks release their borrow before calling Python."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.parametrize("boundary", ["source-package-audit", "exception-attribute", "live-save"])
def test_python_callbacks_allow_a_peer_to_collect(tmp_path: Path, boundary: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "subject.py",
        "def peer():\n    return 2\n"
        "def run(error=None):\n    if error is not None:\n        raise error\n    return 1\n",
    )
    project.write(
        "observe.py",
        """import json
import sys
import threading
from pathlib import Path
from vfy import Coverage, CoverageData
from vfy.plugin import CoveragePlugin
import subject

boundary = sys.argv[1]
go, done = threading.Event(), threading.Event()
armed = blocked = False
values, failures = [], []

def pause(point):
    global armed, blocked
    if armed and point == boundary and threading.current_thread() is threading.main_thread():
        armed, blocked = False, True
        go.set()
        if not done.wait(5):
            raise AssertionError("peer did not finish during " + point)

def audit(event, args):
    if event == "sys._getframe":
        pause("source-package-audit")

class InspectedError(Exception):
    @property
    def exceptions(self):
        pause("exception-attribute")
        return ()

class Discovery(CoveragePlugin):
    def file_tracer(self, filename):
        return None
    def find_executable_files(self, directory):
        pause("live-save")
        return []

def initialize(registration):
    registration.add_file_tracer(Discovery())

def worker():
    try:
        if go.wait(5):
            values.append(subject.peer())
    except BaseException as error:
        failures.append([type(error).__name__, str(error)])
    finally:
        done.set()

options = {"config_file": False, "data_file": ".coverage"}
if boundary == "source-package-audit":
    options["source_pkgs"] = ["subject"]
    sys.addaudithook(audit)
else:
    options["source"] = ["."]
    if boundary == "exception-attribute":
        options["decisions"] = True
    else:
        options["plugins"] = [initialize]
coverage = Coverage(**options)
thread = threading.Thread(target=worker, daemon=True)
saved_peer = None
try:
    coverage.start()
    thread.start()
    armed = True
    if boundary == "live-save":
        assert subject.run() == 1
        coverage.save()
        saved = CoverageData(basename=coverage.get_data().data_filename())
        saved.read()
        saved_peer = 2 in (saved.lines(str(Path("subject.py").resolve())) or [])
    elif boundary == "exception-attribute":
        try:
            subject.run(InspectedError("expected"))
        except InspectedError:
            pass
    else:
        assert subject.run() == 1
finally:
    armed = False
    go.set()
    if thread.ident is not None:
        thread.join(5)
    coverage.stop()
assert not thread.is_alive(), "peer thread did not stop"
assert blocked, "the intended callback did not run"
assert not failures, failures
assert values == [2], values
lines = coverage.get_data().lines(str(Path("subject.py").resolve())) or []
print(json.dumps({"peer_measured": 2 in lines, "saved_peer": saved_peer}))
""",
    )
    result = project.run_python("observe.py", boundary)
    result.expect(0)
    assert json.loads(result.stdout) == {
        "peer_measured": True,
        "saved_peer": True if boundary == "live-save" else None,
    }
