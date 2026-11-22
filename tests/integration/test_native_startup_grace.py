"""Prove finite demand startup grace through public project runs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.integration.test_native_collected_demand import _DEMAND_ENV
from tests.integration.test_native_collected_fork import _case_records
from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="collected fork requires Linux")


def _run(project: VfyProject, grace: str, *extra: str) -> CommandResult:
    return project.run_vfy(
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
        *extra,
        env={
            **_DEMAND_ENV,
            "VFY_NATIVE_STARTUP_GRACE_MS": grace,
            "PYTEST_NATIVE_RESULT": str(project.root / "native.json"),
        },
    )


def test_startup_grace_expires_for_first_body_rendezvous(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_cases.py",
        """
import os, time
from pathlib import Path
import pytest

@pytest.mark.parametrize('value', range(64))
def test_case(value):
    if value in (0, 16):
        Path(f'lease.{value}').write_text(str(os.getpid()))
        deadline = time.monotonic() + 5
        while not Path(f'lease.{16 if value == 0 else 0}').exists():
            assert time.monotonic() < deadline, 'finite startup grace did not expire'
            time.sleep(.005)
    Path(f'body.{value}').touch()
""",
    )
    completed = _run(project, "100")
    completed.expect(0)
    assert (tmp_path / "lease.0").read_text() != (tmp_path / "lease.16").read_text()
    assert len(list(tmp_path.glob("body.*"))) == 64
    assert len(_case_records(tmp_path / "results.xml")) == 64
    result = json.loads((tmp_path / "native.json").read_text())
    assert result["counts"]["passed"] == 64
    assert [case["nodeid"] for case in result["cases"]] == [
        f"tests/test_cases.py::test_case[{value}]" for value in range(64)
    ]
    assert (tmp_path / "history.json").is_file()


def test_startup_grace_maxfail_keeps_followers_idle_and_closes_requests(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_cases.py",
        """
from pathlib import Path
import pytest

@pytest.fixture
def resource(request):
    request.addfinalizer(lambda: Path('finalized').touch())

@pytest.mark.parametrize('value', range(64))
def test_case(value, resource):
    Path(f'body.{value}').touch()
    if value == 0:
        pytest.fail('startup leader failure')
""",
    )
    completed = _run(project, "5000", "--maxfail=1")
    completed.expect(1)
    assert (tmp_path / "finalized").exists()
    assert [path.name for path in tmp_path.glob("body.*")] == ["body.0"]
    assert len(_case_records(tmp_path / "results.xml")) == 1
    result = json.loads((tmp_path / "native.json").read_text())
    assert result["collected"] == 64
    assert result["counts"]["failed"] == 1
    assert len(result["cases"]) == 1
    assert (tmp_path / "history.json").is_file()


@pytest.mark.parametrize(
    ("grace", "workers", "scheduling", "start"),
    [
        ("-1", "2", "demand", "fork"),
        ("nan", "2", "demand", "fork"),
        ("1", "1", "demand", "fork"),
        ("1", "2", "cases", "fork"),
        ("1", "2", "demand", "exec"),
    ],
)
def test_startup_grace_rejects_invalid_policy_before_bodies(
    tmp_path: Path, grace: str, workers: str, scheduling: str, start: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_case.py",
        "from pathlib import Path\ndef test_case():\n    Path('body').touch()\n",
    )
    completed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        f"--workers={workers}",
        "--no-replay",
        "--no-cov",
        "--capture=sys",
        "-q",
        env={
            **_DEMAND_ENV,
            "VFY_NATIVE_STARTUP_GRACE_MS": grace,
            "VFY_NATIVE_SCHEDULING": scheduling,
            "VFY_WORKER_START": start,
        },
    )
    completed.expect(4)
    text = completed.stdout + completed.stderr
    assert "startup grace" in text or "demand scheduling requires" in text
    assert not (tmp_path / "body").exists()
