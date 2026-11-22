"""vfy test coverage refuses to start inside another vfy coverage session."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _project(root: Path) -> VfyProject:
    project = VfyProject(root)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "test_app.py",
        "from pathlib import Path\nfrom app import value\n"
        "def test_value():\n    Path('executed').touch()\n    assert value() == 1\n",
    )
    return project


@pytest.mark.parametrize("engine", ["pytest-native", "native"])
def test_measured_run_inside_coverage_run_reports_the_enclosing_session(
    tmp_path: Path, engine: str
) -> None:
    project = _project(tmp_path)

    result = project.run_vfy(
        "coverage",
        "run",
        "--data-file=enclosing.coverage",
        "-m",
        "vfy",
        "test",
        f"--engine={engine}",
        "--no-replay",
        "--cov=app",
        "-q",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 4, output
    assert "does not support an enclosing active Coverage session" in output
    assert "Traceback" not in output
    assert "DataError" not in output
    assert not (tmp_path / "executed").exists()


@pytest.mark.parametrize("engine", ["pytest-native", "native"])
def test_unmeasured_run_inside_coverage_run_still_executes(tmp_path: Path, engine: str) -> None:
    project = _project(tmp_path)

    result = project.run_vfy(
        "coverage",
        "run",
        "--data-file=enclosing.coverage",
        "-m",
        "vfy",
        "test",
        f"--engine={engine}",
        "--no-replay",
        "--no-cov",
        "-q",
    )

    result.expect(0)
    assert (tmp_path / "executed").is_file()
