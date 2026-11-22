"""Requested mutation baseline coverage retains its collection mode and sources."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


@pytest.mark.skipif(sys.platform != "linux", reason="independent mutations require Linux")
@pytest.mark.parametrize("mode", ["branch", "decisions"])
@pytest.mark.parametrize("configured", [False, True], ids=["flag", "project-default"])
def test_independent_mutation_baseline_preserves_requested_coverage_mode(
    tmp_path: Path, mode: str, configured: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "app.py",
        "def value(flag=True):\n    if flag:\n        return 1\n    return 2\n",
    )
    project.write(
        "test_app.py", "from app import value\ndef test_value():\n    assert value() == 1\n"
    )
    options = ["--cov=app", "--cov-report=json:coverage.json"]
    if configured:
        configuration = (
            "[tool.coverage.run]\nbranch = true\n"
            if mode == "branch"
            else "[tool.coverage.run]\nbranch = false\n[tool.vfy.run]\ndecisions = true\n"
        )
        project.write("pyproject.toml", configuration)
        options.append("--cov-config=.coveragerc")
    else:
        options.append(f"--cov-{mode}")
    if mode == "decisions":
        options.append("--cov-decision-report=json:decisions.json")
        reference = project.run_vfy(
            "test", "--engine=native", "--all", "--source=app.py", *options, "-q"
        )
    else:
        reference = project.run_pytest(*options, "-q", "-p", "pytest_cov", load_xdist=False)
    reference.expect(0)
    expected = json.loads((tmp_path / "coverage.json").read_text())
    assert expected["meta"]["branch_coverage"] is True
    expected_decisions = (
        json.loads((tmp_path / "decisions.json").read_text())["totals"]
        if mode == "decisions"
        else None
    )
    (tmp_path / "coverage.json").unlink()
    if mode == "decisions":
        (tmp_path / "decisions.json").unlink()

    completed = project.run_vfy(
        "mutate",
        "--engine=native",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--max-mutations=1",
        "--data-file=.coverage-mutation",
        "--no-cache",
        "--format=json",
        *options,
        "-q",
    )
    completed.expect(0)
    actual = json.loads((tmp_path / "coverage.json").read_text())
    assert actual["meta"]["branch_coverage"] is True
    assert actual["files"]["app.py"]["summary"] == expected["files"]["app.py"]["summary"]
    if mode == "decisions":
        assert json.loads((tmp_path / "decisions.json").read_text())["totals"] == expected_decisions


@pytest.mark.parametrize(
    "engine",
    [
        pytest.param(
            "native",
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="independent mutations require Linux"
            ),
        ),
        pytest.param("pytest-native", marks=requires_native_pytest),
    ],
)
def test_mutation_baseline_keeps_requested_coverage_sources_and_tests_unmeasured_targets(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write("other.py", "def other_value():\n    return 7\n")
    project.write(
        "test_app.py", "from app import value\ndef test_value():\n    assert value() == 1\n"
    )
    project.write(
        "test_other.py",
        "from other import other_value\ndef test_other_value():\n    assert other_value() == 7\n",
    )
    options = ["--cov=other", "--cov-report=json:coverage.json", "-q"]
    reference = project.run_pytest(*options, "-p", "pytest_cov", load_xdist=False)
    reference.expect(0)
    expected = json.loads((tmp_path / "coverage.json").read_text())["files"]
    assert set(expected) == {"other.py"}
    (tmp_path / "coverage.json").unlink()

    completed = project.run_vfy(
        "mutate",
        f"--engine={engine}",
        "--workers=1",
        "--test-isolation=serial",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--max-mutations=1",
        "--data-file=.coverage-mutation",
        "--no-cache",
        "--format=json",
        *options,
    )
    completed.expect(0)
    actual = json.loads((tmp_path / "coverage.json").read_text())["files"]
    assert actual == expected
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}, report
    assert report["mutations"][0]["killer"] == "test_app.py::test_value", report
