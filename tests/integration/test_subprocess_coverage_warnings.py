"""Automatic subprocess collectors stay quiet about conditions only the parent can judge."""

from __future__ import annotations

from pathlib import Path

from tests.support.vfy_project import VfyProject

_WARNINGS = ("never imported", "No data was collected")


def _project(root: Path) -> VfyProject:
    """The parent measures `pkg`; its child never imports it, so a child warning is wrong."""
    project = VfyProject(root)
    project.write(".coveragerc", "[run]\npatch = subprocess\nsource_pkgs = pkg\n")
    project.write("pkg/__init__.py", "VALUE = 1\n")
    project.write(
        "parent.py",
        "import subprocess\nimport sys\n\nimport pkg\n\n"
        "subprocess.run([sys.executable, '-c', 'pass'], check=True)\n",
    )
    return project


def test_subprocess_collector_stays_quiet_about_what_only_the_parent_judges(
    tmp_path: Path,
) -> None:
    reference = _project(tmp_path / "reference").run_python("-m", "coverage", "run", "parent.py")
    candidate = _project(tmp_path / "vfy").run_vfy("coverage", "run", "parent.py")

    assert reference.returncode == candidate.returncode == 0, candidate.stderr
    for result in (reference, candidate):
        for warning in _WARNINGS:
            assert warning not in result.stderr, result.stderr
