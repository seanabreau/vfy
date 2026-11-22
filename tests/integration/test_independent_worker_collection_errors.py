"""Worker collection errors obey the requested continuation policy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize(
    ("continue_errors", "collect_only"), [(False, False), (True, False), (True, True)]
)
@pytest.mark.parametrize("workers", [1, 2])
def test_collection_error_continuation_matches_pytest(
    tmp_path: Path, continue_errors: bool, collect_only: bool, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_bad.py", "raise ValueError('actual collection failure')\n")
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            f"from pathlib import Path\ndef test_pass():\n    Path('ran-{name}').touch()\n",
        )
    arguments = ["--continue-on-collection-errors"] if continue_errors else []
    if collect_only:
        arguments.append("--collect-only")
    oracle = project.run_python("-m", "pytest", "-q", *arguments)
    assert oracle.returncode == (1 if continue_errors else 2), oracle.stdout + oracle.stderr
    expected = sorted(path.name for path in tmp_path.glob("ran-*"))
    assert expected == (["ran-a", "ran-b"] if continue_errors and not collect_only else [])
    for path in tmp_path.glob("ran-*"):
        path.unlink()

    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "--native-result-json=result.json",
        *arguments,
    )

    assert result.returncode == oracle.returncode, result.stdout + result.stderr
    assert "actual collection failure" in result.stdout
    assert sorted(path.name for path in tmp_path.glob("ran-*")) == expected
    record = json.loads((tmp_path / "result.json").read_text())
    assert record["exitstatus"] == oracle.returncode
    assert record["counts"]["errors"] == 1
    assert [case["nodeid"] for case in record["cases"]] == (
        ["test_a.py::test_pass", "test_b.py::test_pass"]
        if continue_errors and not collect_only
        else []
    )
    if continue_errors:
        assert record["execution_topology"]["active_workers"] == workers


def test_continuing_collection_without_valid_tests_completes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path, timeout=15)
    project.write("test_bad.py", "raise ValueError('actual empty collection failure')\n")
    oracle = project.run_python("-m", "pytest", "-q", "--continue-on-collection-errors")
    oracle.expect(1)

    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        "--continue-on-collection-errors",
        "--native-result-json=result.json",
    )

    assert result.returncode == oracle.returncode, result.stdout + result.stderr
    assert "actual empty collection failure" in result.stdout
    record = json.loads((tmp_path / "result.json").read_text())
    assert record["exitstatus"] == oracle.returncode
    assert (record["collected"], record["counts"]["errors"], record["cases"]) == (0, 1, [])
    assert record["execution_topology"]["active_workers"] == 1


def test_continuing_workers_reject_different_collection_outcomes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_bad.py",
        "import os\n"
        "if os.environ.get('VFY_NATIVE_PARALLEL_INDEX') == '1':\n"
        "    raise ValueError('actual second worker collection failure')\n",
    )
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            "from pathlib import Path\ndef test_pass():\n    Path('body-ran').touch()\n",
        )

    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        "--continue-on-collection-errors",
        "--native-result-json=result.json",
    )

    result.expect(3)
    assert "different collection outcomes" in result.stdout
    assert not (tmp_path / "body-ran").exists()
    assert not (tmp_path / "result.json").exists()
