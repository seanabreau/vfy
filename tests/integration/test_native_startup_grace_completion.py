"""Release demand startup followers when the first lease completes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.integration.test_native_collected_fork import _case_records
from tests.integration.test_native_startup_grace import _run
from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="collected fork requires Linux")


def test_startup_grace_completion_releases_followers_before_deadline(tmp_path: Path) -> None:
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
        Path('first-lease-finished').touch()

@pytest.mark.parametrize('value', range(64))
def test_case(value, phases):
    if value in (16, 32):
        assert Path('first-lease-finished').exists()
        Path(f'lease.{value}').write_text(str(os.getpid()))
        deadline = time.monotonic() + 2
        while not Path(f'lease.{48-value}').exists():
            assert time.monotonic() < deadline, 'first lease did not release its followers'
            time.sleep(.005)
    Path(f'body.{value}').touch()
""",
    )
    completed = _run(project, "5000")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (tmp_path / "lease.16").read_text() != (tmp_path / "lease.32").read_text()
    assert len(list(tmp_path.glob("body.*"))) == 64
    assert len(_case_records(tmp_path / "results.xml")) == 64
    result = json.loads((tmp_path / "native.json").read_text())
    assert result["counts"]["passed"] == 64
    assert [case["nodeid"] for case in result["cases"]] == [
        f"tests/test_cases.py::test_case[{value}]" for value in range(64)
    ]
    assert (tmp_path / "history.json").is_file()
