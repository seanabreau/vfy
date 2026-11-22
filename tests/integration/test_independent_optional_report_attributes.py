"""Native report attribute checks retain Python's builtin lookup semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("replacement_timing", ["before_import", "after_import"])
def test_replaced_getattr_does_not_intercept_native_report_lookup(
    tmp_path: Path, replacement_timing: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_case.py", "def test_pass(): pass\n")
    project.write(
        "conftest.py",
        f"""import builtins
import json
from pathlib import Path

original = builtins.getattr
calls = []

def replacement(value, name, *default):
    if name == "wasxfail" and hasattr(type(value), "when"):
        calls.append(name)
    return original(value, name, *default)

def install():
    builtins.getattr = replacement

if {replacement_timing!r} == "before_import":
    install()

def pytest_configure():
    if {replacement_timing!r} == "after_import":
        install()

def pytest_unconfigure():
    builtins.getattr = original
    Path("calls.json").write_text(json.dumps(calls))
""",
    )
    result = project.run_engine("native", "-q", "test_case.py")
    result.expect(0)
    assert "1 passed" in result.stdout
    assert json.loads((tmp_path / "calls.json").read_text()) == []
