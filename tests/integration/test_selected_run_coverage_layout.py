"""``vfy test`` prints the coverage section once, as pytest-cov does, in every route."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject

_BASELINE = "def first():\n    return 1\n"
_EDITED = "def first(flag=False):\n    if flag:\n        return 0\n    return 1\n"
_ARGUMENTS = ("--cov=one", "--cov=two", "--cov-report=term-missing", "--cov-context=test")


def _section(result: CommandResult) -> tuple[list[str], int]:
    """Return the coverage section lines and how many headings pytest printed."""
    lines = result.stdout.splitlines()
    headings = [index for index, line in enumerate(lines) if "tests coverage" in line]
    assert headings, result.stdout + result.stderr
    summary = [index for index, line in enumerate(lines) if " in " in line and "passed" in line]
    assert summary, result.stdout
    assert summary[-1] > headings[0], result.stdout
    return lines[headings[0] : summary[-1]], len(headings)


def _oracle(project: VfyProject, threshold: str) -> CommandResult:
    return project.run_python(
        "-m", "pytest", "-p", "pytest_cov.plugin", *_ARGUMENTS, f"--cov-fail-under={threshold}"
    )


@pytest.mark.skipif(os.name != "posix", reason="independent execution requires POSIX")
@pytest.mark.parametrize("engine", ["default", "pytest-native", "pytest"])
@pytest.mark.parametrize(
    ("threshold", "status"),
    [pytest.param("80", 0, id="reached-by-complete-data"), pytest.param("90", 1, id="missed")],
)
def test_full_and_selected_runs_print_pytest_cov_layout(
    tmp_path: Path, engine: str, threshold: str, status: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("one.py", _BASELINE)
    project.write("two.py", "def second():\n    return 2\n")
    project.write("test_one.py", "from one import first\ndef test_one(): assert first() == 1\n")
    project.write("test_two.py", "from two import second\ndef test_two(): assert second() == 2\n")
    project.git_init()
    command = ["test", *(() if engine == "default" else (f"--engine={engine}",))]
    command += ["--workers=1", *_ARGUMENTS, f"--cov-fail-under={threshold}"]
    baseline = project.run_vfy(*command, "--all")
    baseline.expect(0)

    project.write("one.py", _EDITED)
    expected = _oracle(project, threshold)
    selected = project.run_vfy(*command)
    full = project.run_vfy(*command, "--all")

    assert expected.returncode == selected.returncode == full.returncode == status, (
        selected.stdout + selected.stderr
    )
    assert "Reused 1 tests; complete coverage reconstructed" in selected.stderr, selected.stderr
    expected_section, _count = _section(expected)
    assert _section(full) == (expected_section, 1)
    assert _section(selected) == (expected_section, 1)
    assert "Reused" not in selected.stdout
