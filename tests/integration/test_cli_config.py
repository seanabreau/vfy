"""vfy configuration tables, `vfy config show`, and nearest-project discovery."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject


@pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="decision collection requires Linux or macOS",
)
@pytest.mark.parametrize(("threshold", "status"), [(40, 0), (60, 2)])
def test_semantic_check_reads_the_documented_vfy_config_table(
    tmp_path: Path, threshold: int, status: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("pyproject.toml", f"[tool.vfy.check]\nfail_under_mcdc = {threshold}\n")
    project.write(
        "bounds.py",
        "def in_bounds(x):\n    if x >= 0 and x < 10:\n        return True\n    return False\n",
    )
    project.write(
        "test_bounds.py",
        "from bounds import in_bounds\n\n"
        "def test_below():\n    assert in_bounds(-1) is False\n\n"
        "def test_inside():\n    assert in_bounds(5) is True\n",
    )
    collected = project.run_python(
        "-m",
        "pytest",
        "-p",
        "no:pytest_cov",
        "-p",
        "vfy.pytest_plugin",
        "--cov=bounds.py",
        "--cov-decisions",
        "test_bounds.py",
    )
    collected.expect(0)

    result = project.run_vfy("coverage", "check")

    assert result.returncode == status, result.stdout + result.stderr


def test_configured_selection_policy_is_reported_by_config_show(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pyproject.toml", '[tool.vfy.test]\nselection = "balanced"\n')

    result = project.run_vfy("config", "show")

    result.expect(0)
    assert json.loads(result.stdout)["settings"]["vfy"]["test"]["selection"] == "balanced"


def test_rust_configuration_uses_the_nearest_vfy_project(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    nested = parent / "package" / "tests"
    nested.mkdir(parents=True)
    (parent / "pyproject.toml").write_text(
        '[tool.vfy.test]\nhistory = "parent-history.json"\n', encoding="utf-8"
    )
    child = parent / "package" / "pyproject.toml"
    child.write_text('[tool.vfy.test]\nhistory = "child-history.json"\n', encoding="utf-8")

    result = VfyProject(nested).run_vfy("config", "show")

    result.expect(0)
    assert json.loads(result.stdout)["settings"]["vfy"] == {
        "test": {"history": "child-history.json"}
    }
