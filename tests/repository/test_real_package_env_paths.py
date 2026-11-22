from pathlib import Path

import pytest
from validation.pytest_runner.real_packages import benchmark, corpus


def test_env_paths_share_resolution_and_provenance(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    entry = {"env_paths": {"PYTHONTZPATH": ".venv/tzdata/usr/share/zoneinfo"}}
    env = {}
    corpus._configure_path_environment(entry, project, env)
    expected = str(project / ".venv/tzdata/usr/share/zoneinfo")
    assert env["PYTHONTZPATH"] == expected
    assert corpus._recorded_environment(entry, env)["PYTHONTZPATH"] == expected
    assert benchmark._environment(entry, project / ".venv")["PYTHONTZPATH"] == expected


@pytest.mark.parametrize("path", ["/etc", "../outside", "link/outside"])
def test_env_paths_reject_escape(tmp_path: Path, path: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="inside the checkout"):
        corpus._configure_path_environment({"env_paths": {"PYTHONTZPATH": path}}, project, {})


def test_env_paths_reject_duplicate_configuration(tmp_path: Path) -> None:
    entry = {"env": {"PYTHONTZPATH": "other"}, "env_paths": {"PYTHONTZPATH": "tzdata"}}
    with pytest.raises(ValueError, match="both env and env_paths"):
        corpus._configure_path_environment(entry, tmp_path, {})
