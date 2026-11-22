"""Original traceback metadata and hide callbacks survive native formatting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CASES = {
    "all_hidden": (
        "def test_value():\n"
        "    __tracebackhide__ = lambda info: info.type is ValueError\n"
        "    raise ValueError('hidden failure')\n"
    ),
    "middle_frames": (
        "def inner(value):\n    raise ValueError(value)\n"
        "def middle(value):\n    inner(value)\n"
        "def test_value():\n    middle('failure')\n"
    ),
    "negative_lasti": (
        "import sys, types\n"
        "def test_value():\n"
        "    frame = sys._getframe()\n"
        "    traceback = types.TracebackType(None, frame, -1, frame.f_lineno)\n"
        "    raise ValueError('manual traceback').with_traceback(traceback)\n"
    ),
    "cause_mutation": (
        "def hide(info):\n"
        "    info.value.__cause__ = RuntimeError('new cause')\n"
        "    return False\n"
        "def test_value():\n"
        "    __tracebackhide__ = hide\n"
        "    error = ValueError('outer')\n"
        "    error.__cause__ = TypeError('old cause')\n"
        "    raise error\n"
    ),
    "short_no_repr": (
        "from pathlib import Path\n"
        "class Value:\n"
        "    def __repr__(self):\n"
        "        Path('repr-called').write_text('called')\n"
        "        raise KeyboardInterrupt('short frame must not represent arguments')\n"
        "def inner():\n    raise ValueError('body failure')\n"
        "def middle(value):\n    inner()\n"
        "def test_value():\n    middle(Value())\n"
    ),
}


def _diagnostic(project: VfyProject, *, candidate: bool) -> str:
    result = project.run_engine("native" if candidate else "stock", "-q")
    label = "candidate" if candidate else "stock"
    project.write(f"{label}.stdout", result.stdout)
    project.write(f"{label}.stderr", result.stderr)
    result.expect(1)
    path = project.root / "diagnostic.json"
    text = json.loads(path.read_text())["text"]
    assert isinstance(text, str)
    path.unlink()
    return text


@pytest.mark.parametrize("case", _CASES)
def test_traceback_edges_preserve_stock_failure_and_diagnostic(tmp_path: Path, case: str) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_value.py", _CASES[case])
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

def pytest_runtest_logreport(report):
    if report.failed:
        Path('diagnostic.json').write_text(json.dumps({'text': report.longreprtext}))
""",
    )
    expected = _diagnostic(project, candidate=False)
    if case == "all_hidden":
        assert "All traceback entries are hidden." in expected
    elif case == "middle_frames":
        assert "in middle" in expected
    elif case == "cause_mutation":
        assert "RuntimeError: new cause" in expected
        assert "TypeError: old cause" not in expected
    elif case == "short_no_repr":
        assert not (tmp_path / "repr-called").exists()
    project.write("stock-diagnostic.json", json.dumps(expected))
    assert _diagnostic(project, candidate=True) == expected
    if case == "short_no_repr":
        assert not (tmp_path / "repr-called").exists()
