"""Report event storage preserves public JSON and worker transport."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
@pytest.mark.parametrize("workers", [1, 2])
def test_phase_event_order_and_late_report_edits_survive_transport_and_replay(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """\
import pytest

reports = []

def pytest_runtest_logreport(report):
    if report.when == 'call':
        reports.append(report)
        report.duration = 2.5

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish():
    for report in reports:
        report.duration = 4.25
""",
    )
    for name in ("first", "second"):
        project.write(f"test_{name}.py", f"def test_{name}(): pass\n")
    result = project.run_vfy(
        "test",
        "--engine=native",
        f"--workers={workers}",
        "--no-replay",
        "--no-cov",
        "-q",
        "--native-result-json=native.json",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "VFY_WORKER_START": "exec"},
    )
    result.expect(0)
    record = json.loads((tmp_path / "native.json").read_text())
    assert len(record["cases"]) == 2
    for case in record["cases"]:
        assert [phase["phase"] for phase in case["phases"]] == ["setup", "call", "teardown"]
        assert case["report_events"] == [
            {"kind": "phase", "index": 0},
            {"kind": "phase", "index": 1},
            {"kind": "phase", "index": 2},
        ]
        assert case["phases"][1]["duration_ms"] == 4250
    replay = project.run_python(
        "-c",
        "from pathlib import Path\nfrom vfy.native import _pytest\n"
        "result = _pytest.NativeRunResult.from_independent_json(Path('native.json').read_text())\n"
        "result.write_json('replayed.json')\n",
    )
    replay.expect(0)
    assert json.loads((tmp_path / "replayed.json").read_text()) == record
