from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

_LINUX_OR_MACOS = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="native test execution requires Linux or macOS",
)


def _selection_project(tmp_path: Path) -> VfyProject:
    project = VfyProject(tmp_path)
    project.write("src/calculator.py", "def double(value):\n    return value * 2\n")
    project.write("lib/__init__.py", "")
    project.write(
        "test_calculator.py",
        "from src.calculator import double\n\n"
        "def test_smoke():\n    assert double(2) == 4\n\n"
        "def test_other():\n    pass\n",
    )
    return project


@_LINUX_OR_MACOS
@pytest.mark.parametrize(
    "pytest_args",
    [
        ["--cov=src", "--cov-branch", "--cov-report=json:coverage.json", "-k", "smoke"],
        ["--cov", "src", "--cov-report", "json:coverage.json", "-k", "smoke"],
    ],
)
def test_test_accepts_pytest_options_without_a_separator(
    tmp_path: Path, pytest_args: list[str]
) -> None:
    project = _selection_project(tmp_path)

    result = project.run_vfy("test", *pytest_args)

    result.expect(0)
    assert (tmp_path / "coverage.json").is_file()


@_LINUX_OR_MACOS
def test_test_no_cov_option_disables_coverage_data(tmp_path: Path) -> None:
    project = _selection_project(tmp_path)

    result = project.run_vfy("test", "--engine=native", "--no-cov", "-k", "smoke")

    result.expect(0)
    assert not (tmp_path / ".coverage").exists()


@_LINUX_OR_MACOS
@pytest.mark.parametrize(
    "argv",
    [
        ["--cov=src", "--all", "--source", "lib", "--cov-branch"],
        ["--all", "--source", "lib", "--cov=src", "--cov-branch"],
        ["--cov=src", "--source", "lib", "--cov-branch", "--all"],
    ],
)
def test_test_options_behave_the_same_after_pytest_options(tmp_path: Path, argv: list[str]) -> None:
    project = _selection_project(tmp_path)

    result = project.run_vfy("test", *argv)

    result.expect(0)
    assert project.data().lines(str(tmp_path / "src/calculator.py")) == [1, 2]


@pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="native test execution requires Linux or macOS",
)
def test_test_runs_vfy_options_after_coverage_options(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    source = project.write("src/calculator.py", "def double(value):\n    return value * 2\n")
    project.write(
        "test_calculator.py",
        "from src.calculator import double\n\ndef test_double():\n    assert double(2) == 4\n",
    )

    result = project.run_vfy("test", "--engine=native", "--cov=src", "--all")

    result.expect(0)
    assert project.data().lines(str(source)) == [1, 2]


@pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="native test execution requires Linux or macOS",
)
def test_test_help_example_collects_branch_coverage(tmp_path: Path) -> None:
    """Help output does not depend on the engine, so no engine is selected."""
    project = VfyProject(tmp_path)
    source = project.write(
        "src/calculator.py",
        "def classify(value):\n    if value:\n        return 1\n    return 0\n",
    )
    project.write(
        "test_calculator.py",
        "from src.calculator import classify\n\n"
        "def test_positive():\n    assert classify(True) == 1\n",
    )

    help_result = project.run_vfy("test", "--help")

    assert (help_result.returncode, help_result.stderr) == (0, "")
    example = next(
        line.strip()
        for line in help_result.stdout.splitlines()
        if line.strip().startswith("vfy test ")
    )
    result = project.run_vfy(*shlex.split(example)[1:])

    result.expect(0)
    data = project.data()
    assert data.has_arcs()
    assert data.measured_files() == {str(source)}
    assert data.lines(str(source)) == [1, 2, 3]
    assert "Missing" in result.stdout


def test_test_help_discovers_pytest_and_coverage_options(tmp_path: Path) -> None:
    """Help output does not depend on the engine, so no engine is selected."""
    project = VfyProject(tmp_path)
    help_result = project.run_vfy("test", "--help")
    help_result.expect(0)
    command = next(
        line.strip()
        for line in help_result.stdout.splitlines()
        if line.strip().startswith("python -m pytest ")
    )

    result = project.run_python(*shlex.split(command)[1:])

    assert (result.returncode, result.stderr) == (0, "")
    assert "--maxfail=" in result.stdout
    assert "--cov-fail-under=MIN" in result.stdout
    summarized = [
        line.split()[0].split("=")[0]
        for line in help_result.stdout.splitlines()
        if line.startswith(("  --cov", "  --no-cov"))
    ]
    assert summarized
    for option in summarized:
        assert re.search(rf"(?<![\w-]){re.escape(option)}(?![\w-])", result.stdout), option


@_LINUX_OR_MACOS
def test_test_keeps_targets_before_direct_pytest_options(tmp_path: Path) -> None:
    project = _selection_project(tmp_path)
    project.write("test_failing.py", "def test_never_selected():\n    raise AssertionError\n")

    result = project.run_vfy(
        "test", "--engine=native", "test_calculator.py", "--cov=src", "-k", "smoke"
    )

    result.expect(0)
    assert project.data().lines(str(tmp_path / "src/calculator.py")) == [1, 2]


def test_test_rejects_the_removed_separator(tmp_path: Path) -> None:
    result = VfyProject(tmp_path).run_vfy("test", "--engine=native", "--all", "--", "--cov=src")

    assert result.returncode == 1
    assert "remove the `--` separator" in result.stderr


@_LINUX_OR_MACOS
@requires_native_pytest
def test_test_ignores_selection_variables_of_an_enclosing_run(tmp_path: Path) -> None:
    project = _selection_project(tmp_path)
    selected = project.write("outer-selection.json", '["test_calculator.py::test_smoke"]')

    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--no-cov",
        "-v",
        env={"VFY_SELECTED_TESTS": str(selected), "VFY_CONFIRM_FAILURES": str(selected)},
    )

    result.expect(0)
    assert "test_other PASSED" in result.stdout
