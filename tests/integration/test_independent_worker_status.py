"""Native process status retains session hook overrides for passed worker cases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("declared", [1, 42, 300, -1])
def test_session_policy_status_preserves_passed_worker_cases(tmp_path: Path, declared: int) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = {declared}\n",
    )
    for name in ("a", "b"):
        project.write(f"test_{name}.py", "def test_pass():\n    pass\n")
    oracle = project.run_python("-m", "pytest", "-q")

    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        "--native-result-json=record.json",
    )

    assert result.returncode == oracle.returncode, result.stdout + result.stderr
    record = json.loads((tmp_path / "record.json").read_text())
    assert record["exitstatus"] == declared
    assert record["counts"]["passed"] == 2
    assert record["counts"]["failed"] == record["counts"]["errors"] == 0
    assert record["execution_topology"]["active_workers"] == 2
