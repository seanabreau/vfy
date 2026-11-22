"""File-tracer callbacks can wait while another thread is collecting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject


def test_file_tracer_wait_does_not_block_another_collecting_thread(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "def run():\n    return 1\n")
    project.write("other.py", "def run():\n    return 2\n")
    project.write(
        "collect.py",
        """import importlib
import json
import sys
import threading
from pathlib import Path
import subject, other

tool = sys.argv[1]
Coverage = importlib.import_module(tool).Coverage
CoveragePlugin = importlib.import_module(tool + ".plugin").CoveragePlugin
ready, done = threading.Event(), threading.Event()
results = []

class Plugin(CoveragePlugin):
    def file_tracer(self, filename):
        if (filename.endswith("subject.py")
                and threading.current_thread() is threading.main_thread()):
            ready.set()
            if not done.wait(10):
                raise AssertionError("worker did not finish while file_tracer waited")
        return None

def initialize(registration):
    registration.add_file_tracer(Plugin())

def worker():
    try:
        if ready.wait(10):
            results.append(other.run())
    finally:
        done.set()

thread = threading.Thread(target=worker, daemon=True)
cov = Coverage(data_file=None, config_file=False, plugins=[initialize])
try:
    cov.start()
    thread.start()
    assert subject.run() == 1
    thread.join(10)
    assert not thread.is_alive()
    assert results == [2], results
finally:
    ready.set()
    done.set()
    if thread.ident is not None:
        thread.join(10)
    cov.stop()
data = cov.get_data()
print(json.dumps([data.lines(str(Path(name).resolve())) for name in ("subject.py", "other.py")]))
""",
    )
    results = {}
    for tool in ("coverage", "vfy"):
        result = project.run_python("collect.py", tool, env={"COVERAGE_CORE": "ctrace"})
        result.expect(0)
        results[tool] = json.loads(result.stdout)
    assert results["coverage"] == [[2], [2]]
    assert results["vfy"] == results["coverage"]


@pytest.mark.parametrize(
    "stage",
    ["file_tracer", "has_dynamic_source_filename", "dynamic_source_filename", "line_number_range"],
)
def test_dynamic_file_tracer_interleavings(tmp_path: Path, stage: str) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "def run():\n    return 1\n")
    project.write("other.py", "def run():\n    return 2\n")
    project.write(
        "dynamic.py",
        """import importlib
import json
import sys
import threading
from pathlib import Path
import subject, other

tool, stage = sys.argv[1:]
Coverage = importlib.import_module(tool).Coverage
plugin = importlib.import_module(tool + ".plugin")
ready, done = threading.Event(), threading.Event()
results = []
blocked = False

def pause(point):
    global blocked
    if point == stage and not blocked and threading.current_thread() is threading.main_thread():
        blocked = True
        ready.set()
        if not done.wait(10):
            raise AssertionError("worker did not finish while " + stage + " waited")

class Tracer(plugin.FileTracer):
    def has_dynamic_source_filename(self):
        pause("has_dynamic_source_filename")
        return True

    def dynamic_source_filename(self, filename, frame):
        pause("dynamic_source_filename")
        return str(Path("subject.py").resolve())

    def line_number_range(self, frame):
        pause("line_number_range")
        return frame.f_lineno, frame.f_lineno

class Plugin(plugin.CoveragePlugin):
    def file_tracer(self, filename):
        if filename.endswith("subject.py"):
            pause("file_tracer")
            return Tracer()
        return None

def initialize(registration):
    registration.add_file_tracer(Plugin())

def worker():
    try:
        if ready.wait(10):
            results.append(other.run())
    finally:
        done.set()

thread = threading.Thread(target=worker, daemon=True)
cov = Coverage(data_file=None, config_file=False, plugins=[initialize], branch=True)
try:
    cov.start()
    thread.start()
    assert subject.run() == 1
    thread.join(10)
    assert not thread.is_alive()
    assert blocked and results == [2], results
finally:
    ready.set()
    done.set()
    if thread.ident is not None:
        thread.join(10)
    cov.stop()
data = cov.get_data()
records = []
for name in ("subject.py", "other.py"):
    path = str(Path(name).resolve())
    records.append([data.lines(path), sorted(data.arcs(path) or [])])
print(json.dumps(records))
""",
    )
    results = {}
    for tool in ("coverage", "vfy"):
        result = project.run_python("dynamic.py", tool, stage, env={"COVERAGE_CORE": "ctrace"})
        assert result.returncode == 0, (stage, tool, result.stderr)
        results[tool] = json.loads(result.stdout)
    assert results["coverage"][0][0] == [2]
    assert results["coverage"][1][0] == [2]
    assert results["vfy"] == results["coverage"]


def test_concurrent_dynamic_filenames_are_attributed_to_each_event(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "def run(target):\n    return target\n")
    project.write(
        "attribution.py",
        """import importlib
import json
import sys
import threading
from pathlib import Path
import subject

tool = sys.argv[1]
Coverage = importlib.import_module(tool).Coverage
plugin = importlib.import_module(tool + ".plugin")
go_a, go_b, entered, release, done_b = (threading.Event() for _ in range(5))
results = []
blocked = False

class Tracer(plugin.FileTracer):
    def has_dynamic_source_filename(self):
        return True

    def dynamic_source_filename(self, filename, frame):
        global blocked
        target = frame.f_locals["target"]
        if target == "a" and not blocked:
            blocked = True
            entered.set()
            if not release.wait(10):
                raise AssertionError("first dynamic filename did not resume")
        return str(Path(target + ".tpl").resolve())

class Plugin(plugin.CoveragePlugin):
    def file_tracer(self, filename):
        return Tracer() if filename.endswith("subject.py") else None

def initialize(registration):
    registration.add_file_tracer(Plugin())

def worker(target, go):
    if go.wait(10):
        results.append(subject.run(target))
        if target == "b":
            done_b.set()

threads = [threading.Thread(target=worker, args=("a", go_a), daemon=True),
           threading.Thread(target=worker, args=("b", go_b), daemon=True)]
cov = Coverage(data_file=None, config_file=False, plugins=[initialize], branch=True)
try:
    cov.start()
    threads[0].start()
    go_a.set()
    assert entered.wait(10)
    threads[1].start()
    go_b.set()
    assert done_b.wait(10), "second thread did not finish while first tracer waited"
finally:
    release.set()
    go_a.set()
    go_b.set()
    for thread in threads:
        if thread.ident is not None:
            thread.join(10)
    cov.stop()
assert all(not thread.is_alive() for thread in threads)
assert sorted(results) == ["a", "b"], results
data = cov.get_data()
records = []
for name in ("a", "b"):
    path = str(Path(name + ".tpl").resolve())
    records.append([data.lines(path), sorted(data.arcs(path) or [])])
print(json.dumps(records))
""",
    )
    results = {}
    for tool in ("coverage", "vfy"):
        result = project.run_python("attribution.py", tool, env={"COVERAGE_CORE": "ctrace"})
        assert result.returncode == 0, (tool, result.stderr)
        results[tool] = json.loads(result.stdout)
    assert results["coverage"] == [[[2], [[-1, 2], [2, -1]]]] * 2
    assert results["vfy"] == results["coverage"]
