"""``vfy config show`` reports the configuration the commands resolve."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject


def test_config_show_prefers_the_nearest_vfy_project_over_coverage_files(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.vfy.test]\nworkers = "logical"\nsource = ["application"]\n'
        "[tool.coverage.report]\nprecision = 3\nfail_under = 99.5\n"
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".coveragerc").write_text("[run]\nbranch = yes\n")

    result = VfyProject(nested).run_vfy("config", "show")

    result.expect(0)
    shown = json.loads(result.stdout)
    assert shown["source"] == str(tmp_path / "pyproject.toml")
    assert shown["settings"]["vfy"]["test"] == {"source": ["application"], "workers": "logical"}
    assert shown["settings"]["report"] == {"fail_under": 99.5, "precision": 3}


def test_config_show_expands_environment_in_coverage_py_files(tmp_path: Path) -> None:
    (tmp_path / ".coveragerc").write_text(
        "[run]\nbranch = yes\ndata_file = ${REPORT_ROOT}/coverage$$data\n"
    )

    result = VfyProject(tmp_path).run_vfy("config", "show", env={"REPORT_ROOT": "résultats"})

    result.expect(0)
    shown = json.loads(result.stdout)
    assert shown["provenance"]["discovery"] == "Coverage.py fallback"
    assert shown["settings"]["run"] == {"branch": True, "data_file": "résultats/coverage$data"}


def test_config_change_between_runs_is_read_fresh_and_invalid_values_fail(tmp_path: Path) -> None:
    config = tmp_path / "pyproject.toml"
    project = VfyProject(tmp_path)
    config.write_text("[tool.vfy.test]\nworkers = 3\n")
    assert _workers(project) == "3"

    config.write_text("[tool.vfy.test]\nworkers = 0\n")
    failed = project.run_vfy("config", "show")
    assert failed.returncode == 1
    assert "invalid value for [tool.vfy.test] workers: 0" in failed.stderr

    config.write_text("[tool.vfy.test]\nworkers = 4\n")
    assert _workers(project) == "4"


def _workers(project: VfyProject) -> str:
    result = project.run_vfy("config", "show")
    result.expect(0)
    return str(json.loads(result.stdout)["settings"]["vfy"]["test"]["workers"])
