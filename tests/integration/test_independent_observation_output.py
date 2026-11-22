"""Detailed result observations are captured for consumers that request them."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_cli_observes_late_markers_only_for_requested_native_output(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nmarkers = late_snapshot: added at session finish\n")
    project.write("test_body.py", "def test_body(): pass\n")
    project.write(
        "conftest.py",
        """import atexit
import json
from pathlib import Path
import pytest

reads = []
atexit.register(lambda: Path('late-reads.json').write_text(json.dumps(reads)))

def pytest_sessionfinish(session):
    item = session.items[0]
    item.add_marker(pytest.mark.late_snapshot)
    original = item.iter_markers
    def tracked(name=None):
        reads.append(name)
        return original(name)
    item.iter_markers = tracked
""",
    )
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(0)
    expected = json.loads((project.root / "late-reads.json").read_text())
    assert expected == []
    ordinary = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    ordinary.expect(0)
    assert json.loads((project.root / "late-reads.json").read_text()) == expected
    detailed = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-q", "--native-result-json=result.json"
    )
    detailed.expect(0)
    record = json.loads((project.root / "result.json").read_text())
    assert record["case_markers"] == [
        {"nodeid": "test_body.py::test_body", "names": ["late_snapshot"]}
    ]
    assert json.loads((project.root / "late-reads.json").read_text()) == [None]
