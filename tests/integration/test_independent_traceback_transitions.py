"""Automatic tracebacks retain separators at long and short frame transitions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _diagnostic(project: VfyProject, *, candidate: bool) -> str:
    result = project.run_engine("native" if candidate else "stock", "-q")
    label = "candidate" if candidate else "stock"
    project.write(f"{label}.stdout", result.stdout)
    project.write(f"{label}.stderr", result.stderr)
    result.expect(1)
    path = project.root / "diagnostic.json"
    observed = json.loads(path.read_text())
    assert isinstance(observed, str)
    path.unlink()
    project.write(f"{label}-diagnostic.json", json.dumps(observed))
    return observed


@pytest.mark.parametrize("hidden_middle", [0, 1, 2])
def test_automatic_traceback_frame_transitions_match_stock(
    tmp_path: Path, hidden_middle: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_value.py",
        "def fail(value):\n"
        "    raise ValueError(value)\n\n"
        "def middle_two(value):\n"
        f"    __tracebackhide__ = {hidden_middle >= 2!r}\n"
        "    fail(value)\n\n"
        "def middle_one(value):\n"
        f"    __tracebackhide__ = {hidden_middle >= 1!r}\n"
        "    middle_two(value)\n\n"
        "def test_value():\n"
        "    middle_one('failure')\n",
    )
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

def pytest_runtest_logreport(report):
    if report.failed:
        Path('diagnostic.json').write_text(json.dumps(report.longreprtext))
""",
    )
    expected = _diagnostic(project, candidate=False)
    assert ("in middle_one" in expected) is (hidden_middle == 0)
    assert ("in middle_two" in expected) is (hidden_middle < 2)
    assert _diagnostic(project, candidate=True) == expected
