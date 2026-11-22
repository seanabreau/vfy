"""Traceback arguments retain ordinary Python representations of bound methods."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("subject_text", ["stable-subject", "prefix-" + "x" * 300 + "-suffix"])
@pytest.mark.parametrize("verbosity", ["quiet", "verbose"])
def test_traceback_method_argument_representation_matches_stock(
    tmp_path: Path, subject_text: str, verbosity: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_value.py",
        """import builtins
import json
from pathlib import Path
import weakref

watched = None

def record(value):
    with Path('events.jsonl').open('a') as stream:
        stream.write(json.dumps(value) + '\\n')

class Subject:
    def __init__(self):
        global watched
        watched = weakref.ref(self)
    def __repr__(self):
        return 'stable-subject'
    def __del__(self):
        record('subject.destroyed')
    def method(self):
        pass

def sink(callback):
    raise ValueError('argument representation witness')

def test_value():
    sink(Subject().method)
""".replace("'stable-subject'", repr(subject_text)),
    )
    project.write(
        "conftest.py",
        """import gc
import json
import sys
from pathlib import Path

def pytest_runtest_logreport(report):
    if report.when == 'call':
        Path('diagnostic.json').write_text(json.dumps({
            'outcome': report.outcome, 'diagnostic': report.longreprtext,
        }))

def pytest_unconfigure(config):
    for name in ('last_type', 'last_value', 'last_traceback', 'last_exc'):
        if hasattr(sys, name):
            delattr(sys, name)
    gc.collect()
    import test_value
    test_value.record(['after run', test_value.watched() is None])
""",
    )
    observed: dict[str, dict[str, object]] = {}
    for label in ("stock", "candidate"):
        result = project.run_engine(
            "stock" if label == "stock" else "independent",
            "-vvv" if verbosity == "verbose" else "-q",
            "-p",
            "no:cacheprovider",
        )
        project.write(f"{label}.stdout", result.stdout)
        project.write(f"{label}.stderr", result.stderr)
        result.expect(1)
        events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
        report = json.loads((tmp_path / "diagnostic.json").read_text())
        observed[label] = {"events": events, "report": report}
        project.write(f"{label}-observed.json", json.dumps(observed[label], indent=2))
        (tmp_path / "events.jsonl").unlink()
    stock_report = cast("dict[str, str]", observed["stock"]["report"])
    assert "callback = <bound method Subject.method of " in stock_report["diagnostic"]
    assert observed["stock"]["events"] == ["subject.destroyed", ["after run", True]]
    assert observed["candidate"] == observed["stock"]
