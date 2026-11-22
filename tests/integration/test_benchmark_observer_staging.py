"""Benchmark observers preserve project imports and Coverage.py file identity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from benchmarks.observer_plugins import OUTCOME_PLUGIN, stage_plugins

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("engine", ["stock", "native"])
def test_observer_handles_project_benchmarks_package(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path / "project")
    project.write("benchmarks/__init__.py", "ORIGIN = 'specimen'\n")
    source = project.write("src/pkg/__init__.py", "def answer():\n    return 42\n")
    project.write(
        "test_value.py",
        "import benchmarks\nimport pkg\n\ndef test_value():\n"
        "    assert benchmarks.ORIGIN == 'specimen'\n    assert pkg.answer() == 42\n",
    )
    project.write(
        "pyproject.toml",
        "[tool.pytest.ini_options]\npythonpath = ['src']\n"
        "[tool.coverage.run]\nsource = ['pkg']\nbranch = true\n"
        "[tool.coverage.paths]\nother = ['.', '*/pkg']\nsource = ['src', '**/src']\n",
    )
    outcomes = tmp_path / "outcomes.jsonl"
    import_root = stage_plugins(tmp_path / "observer-path")
    environment = {
        "PYTHONPATH": str(import_root),
        "VFY_BENCHMARK_OUTCOMES": str(outcomes),
    }
    arguments = [
        "-q",
        "-p",
        OUTCOME_PLUGIN,
        "--cov=pkg",
        "--cov-report=json:coverage.json",
        "test_value.py",
    ]
    if engine == "stock":
        result = project.run_python(
            "-m", "pytest", "-p", "pytest_cov.plugin", *arguments, env=environment
        )
    else:
        result = project.run_vfy(
            "test", "--engine=native", "--workers=1", *arguments, env=environment
        )
    result.expect(0)
    assert {
        tuple(record[key] for key in ("nodeid", "when", "outcome", "wasxfail"))
        for record in map(json.loads, outcomes.read_text().splitlines())
    } == {
        ("test_value.py::test_value", phase, "passed", None)
        for phase in ("setup", "call", "teardown")
    }
    report = json.loads((project.root / "coverage.json").read_text())
    assert set(report["files"]) == {"src/pkg/__init__.py"}
    data = project.data()
    try:
        assert data.measured_files() == {str(source)}
        assert data.lines(str(source)) == [1, 2]
    finally:
        data.close(force=True)
