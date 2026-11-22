"""Keep demand startup followers behind the first lease's teardown."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from tests.integration.test_native_collected_fork import _case_records
from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="collected fork requires Linux")


def test_demand_startup_grace_waits_for_first_lease_teardown(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_cases.py",
        """
import os, time
from pathlib import Path
import pytest

@pytest.fixture
def phases(request):
    yield
    if request.node.callspec.params['value'] == 15:
        Path('teardown-entered').touch()
        deadline = time.monotonic() + 20
        while not Path('release-teardown').exists():
            assert time.monotonic() < deadline, 'parent did not release the first lease teardown'
            time.sleep(.005)
        Path('teardown-finished').touch()

@pytest.mark.parametrize('value', range(64))
def test_case(value, phases):
    if value >= 16:
        assert Path('teardown-finished').exists(), 'later lease overtook first lease teardown'
        Path('later-body').touch()
    Path(f'case.{value}').write_text(str(os.getpid()))
""",
    )
    command = project.start_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--history",
        "history.json",
        "--no-replay",
        "--no-cov",
        "--capture=sys",
        "--junitxml=results.xml",
        "-q",
        env={
            "VFY_WORKER_START": "fork",
            "VFY_NATIVE_SCHEDULING": "demand",
            "VFY_WORKER_COLLECTION": "0",
            "VFY_NATIVE_STARTUP_GRACE_MS": "5000",
            "PYTEST_NATIVE_RESULT": str(project.root / "native.json"),
        },
    )
    try:
        project.wait_for_path("teardown-entered")
        # Give waiting workers several polls while teardown remains blocked.
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            assert command.poll() is None, command.text("stdout") + command.text("stderr")
            assert not (tmp_path / "later-body").exists()
            time.sleep(0.01)
        (tmp_path / "release-teardown").touch()
        completed = command.wait(timeout=30)
    finally:
        if command.poll() is None:
            command.kill()
            command.wait(timeout=10)
    completed.expect(0)
    assert "64 passed" in completed.stdout
    assert (tmp_path / "teardown-finished").exists()
    assert (tmp_path / "later-body").exists()
    assert len(list(tmp_path.glob("case.*"))) == 64
    assert len(_case_records(tmp_path / "results.xml")) == 64
    result = json.loads((tmp_path / "native.json").read_text())
    assert result["collected"] == 64
    assert len(result["cases"]) == 64
    assert len({case["nodeid"] for case in result["cases"]}) == 64
    assert (tmp_path / "history.json").is_file()
