"""The public independent CLI forwards configured coverage and disabling options."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("disabled", [False, True])
def test_independent_cli_configured_coverage_matches_stock(tmp_path: Path, disabled: bool) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini",
        "[pytest]\naddopts = --cov=product --cov-context=test --cov-report=json:report.json\n",
    )
    project.write("product.py", "def value():\n    return 7\n")
    project.write("conftest.py", "from product import value\n")
    project.write(
        "test_product.py",
        "from product import value\n"
        "def test_value(cov):\n"
        f"    assert (cov is None) is {disabled!r}\n"
        "    assert value() == 7\n",
    )
    observations = []
    for route in ("oracle", "candidate"):
        arguments = ["--no-cov"] if disabled else []
        environment = {"COVERAGE_FILE": str(project.root / f".coverage.{route}")}
        result = (
            project.run_python(
                "-m", "pytest", "-p", "pytest_cov.plugin", *arguments, env=environment
            )
            if route == "oracle"
            else project.run_vfy(
                "test", "--engine=native", "--workers=1", *arguments, env=environment
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        report = project.root / "report.json"
        if disabled:
            assert not report.exists()
            assert not (project.root / f".coverage.{route}").exists()
        else:
            data = project.data(f".coverage.{route}")
            source = str(project.root / "product.py")
            observations.append(
                (
                    data.lines(source),
                    data.contexts_by_lineno(source),
                    json.loads(report.read_text())["files"]["product.py"],
                )
            )
            report.unlink()
    if not disabled:
        assert observations[1] == observations[0]
        assert observations[0][0] == [1, 2]
