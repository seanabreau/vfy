"""Unittest traceback entry styles match stock pytest reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_HELPERS = """import unittest
from pathlib import Path

class Argument:
    def __repr__(self):
        return '<stable-argument>'

class Base(unittest.TestCase):
    def __repr__(self):
        return '<stable-case>'
    def helper(self, value):
        self.assertEqual(3, 4)
    def hidden(self, value):
        __tracebackhide__ = True
        self.helper(value)
    def test_inherited(self):
        self.hidden(Argument())

def ordinary(value):
    raise ValueError('ordinary failure')
"""

_CASES = """import helpers

class Inherited(helpers.Base):
    pass

def test_ordinary():
    helpers.ordinary(helpers.Argument())
"""

_OBSERVER = """import json
from pathlib import Path

def pytest_runtest_logreport(report):
    if report.when == 'call':
        graph = report.longrepr
        value = {
            'nodeid': report.nodeid,
            'outcome': report.outcome,
            'text': report.longreprtext,
            'entries': [[entry.style, entry.reprfileloc.path, entry.reprfileloc.lineno,
                         entry.reprfileloc.message, entry.reprfuncargs is not None]
                        for entry in graph.reprtraceback.reprentries],
        }
        with Path('reports.jsonl').open('a') as output:
            output.write(json.dumps(value) + '\\n')
"""


@pytest.mark.parametrize("style", ["auto", "long", "short"])
def test_unittest_traceback_entry_styles_match_stock(tmp_path: Path, style: str) -> None:
    project = VfyProject(tmp_path)
    project.write("helpers.py", _HELPERS)
    project.write("test_helpers.py", _CASES)
    project.write("conftest.py", _OBSERVER)
    oracle = project.run_python("-m", "pytest", "-q", f"--tb={style}")
    expected = (tmp_path / "reports.jsonl").read_text()
    (tmp_path / "reports.jsonl").unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q", f"--tb={style}")
    diagnostic = oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    assert oracle.returncode == candidate.returncode == 1, diagnostic
    assert (tmp_path / "reports.jsonl").read_text() == expected, diagnostic
    reports = [json.loads(line) for line in expected.splitlines()]
    inherited = next(report for report in reports if "::Inherited::" in report["nodeid"])
    ordinary = next(report for report in reports if report["nodeid"].endswith("::test_ordinary"))
    assert [entry[0] for entry in inherited["entries"]] == (
        ["long", "short"] if style == "auto" else [style, style]
    )
    assert [entry[0] for entry in ordinary["entries"]] == (
        ["long", "long"] if style == "auto" else [style, style]
    )
