"""Parallel test workers measure the same files as serial runs and pytest-cov."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _measured(project: VfyProject, *arguments: str, stock: bool = False) -> set[str]:
    report = project.root / "coverage.json"
    report.unlink(missing_ok=True)
    environment = {"PYTHONPATH": str(project.root / "startup")}
    if stock:
        result = project.run_pytest(
            "--cov", "--cov-report=json:coverage.json", "-q", *arguments, env=environment
        )
    else:
        result = project.run_vfy(
            "test",
            "--engine=native",
            "--cov-report=json:coverage.json",
            "-q",
            *arguments,
            env=environment,
        )
    result.expect(0)
    return {Path(name).name for name in json.loads(report.read_text())["files"]}


def test_workers_start_measuring_after_application_sitecustomize(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("startup/sitecustomize.py", "import os\nSTARTED = os.getpid()\n")
    project.write("src/calc.py", "def add(a, b):\n    return a + b\n")
    project.write(
        "tests/test_calc.py",
        "import pytest\nfrom src.calc import add\n"
        "@pytest.mark.parametrize('value', range(8))\n"
        "def test_add(value):\n    assert add(value, 1) == value + 1\n",
    )

    serial = _measured(project)
    parallel = _measured(project, "-n", "2")
    stock_parallel = _measured(project, "-n", "2", stock=True)

    assert "sitecustomize.py" not in serial
    assert parallel == serial == stock_parallel
