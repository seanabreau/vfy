"""Native benchmark samples retain complete execution and failure evidence."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from benchmarks.benchmark_edit_loop import assert_test_failure, executed_outcomes

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")), reason="native execution requires Unix"
)


def test_native_outcome_gate_accepts_real_failure_and_rejects_missing_teardown(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_value():\n    assert False, 'breaking edit'\n")
    report = tmp_path / "outcomes.json"
    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        "--no-cov",
        "-q",
        env={"PYTEST_NATIVE_RESULT": str(report)},
    )
    result.expect(1)
    assert executed_outcomes(report) == {"test_value.py::test_value": "failed"}
    completed = subprocess.CompletedProcess(["vfy", "test"], result.returncode)
    assert_test_failure(completed, report)

    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["cases"][0]["phases"] = [
        phase for phase in payload["cases"][0]["phases"] if phase["phase"] != "teardown"
    ]
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="incomplete pytest protocol"):
        assert_test_failure(completed, report)
