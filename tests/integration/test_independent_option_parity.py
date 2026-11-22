"""Options that stock pytest accepts and the independent engine honours."""

from __future__ import annotations

import re
from importlib.metadata import version
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

VFY_VERSION = version("vfy")

_FAILING_PROJECT = """import pytest


def test_fails():
    assert 1 == 2


def test_passes():
    pass


@pytest.mark.skip(reason="not today")
def test_skipped():
    pass
"""


def _lines(text: str) -> list[str]:
    return [re.sub(r" in [0-9.]+s\b", " in Xs", line) for line in text.splitlines()]


def _assert_matches_stock(stock: CommandResult, independent: CommandResult) -> None:
    assert independent.returncode == stock.returncode, independent.stderr
    assert _lines(independent.stdout) == _lines(stock.stdout)
    assert _lines(independent.stderr) == _lines(stock.stderr)


def _differential(project: VfyProject, *arguments: str) -> CommandResult:
    stock = project.run_engine("stock", *arguments, stock=("-p", "no:cacheprovider"))
    independent = project.run_engine("native", *arguments)
    _assert_matches_stock(stock, independent)
    return independent


@requires_native_pytest
@pytest.mark.parametrize("flag", ["--version", "-V"])
@pytest.mark.parametrize("engine", [(), ("--engine=native",)])
def test_owned_version_exits_before_collection_and_plugin_startup(
    tmp_path: Path, flag: str, engine: tuple[str, ...]
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_version.py", "raise AssertionError('version collected tests')\n")
    project.write("conftest.py", "raise AssertionError('version imported conftest')\n")
    project.write("version_plugin.py", "raise AssertionError('version imported plugin')\n")

    result = project.run_vfy("test", *engine, "--no-cov", "-p", "version_plugin", flag)

    result.expect(0)
    assert result.stdout == f"vfy {VFY_VERSION}\n"
    assert result.stderr == ""


@pytest.mark.parametrize("flag", ["--version", "-V"])
@pytest.mark.parametrize(
    "engine", ["pytest", pytest.param("pytest-native", marks=requires_native_pytest)]
)
def test_installed_engine_version_keeps_the_pytest_contract(
    tmp_path: Path, flag: str, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_version.py", "raise AssertionError('version collected tests')\n")
    stock = project.run_pytest(flag, load_xdist=False)
    result = project.run_vfy("test", f"--engine={engine}", "--no-cov", flag)

    assert result.returncode == stock.returncode == 0, result.stdout + result.stderr
    assert result.stdout == stock.stdout == f"pytest {pytest.__version__}\n"
    assert result.stderr == stock.stderr == ""


@requires_native_pytest
def test_repeated_version_flag_is_refused_without_running_tests(tmp_path: Path) -> None:
    # Stock prints pytest's verbose version report here, which vfy does not reproduce.
    project = VfyProject(tmp_path)
    project.write("test_version.py", "def test_unused():\n    raise AssertionError\n")

    result = project.run_engine("native", "-VV")

    assert result.returncode == 4
    assert "may be given once" in result.stderr
    assert "passed" not in result.stdout
    assert "failed" not in result.stdout


@requires_native_pytest
def test_no_summary_suppresses_summary_like_stock(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_summary.py", _FAILING_PROJECT)

    result = _differential(project, "--no-summary", "--no-header")

    assert result.returncode == 1
    assert "FAILURES" not in result.stdout
    assert "short test summary info" not in result.stdout


@requires_native_pytest
@pytest.mark.parametrize("value", ["yes", "no"])
def test_code_highlight_option_is_accepted_without_color(tmp_path: Path, value: str) -> None:
    project = VfyProject(tmp_path)
    project.write("test_highlight.py", _FAILING_PROJECT)

    _differential(project, "--color=no", f"--code-highlight={value}", "--no-header")


@requires_native_pytest
@pytest.mark.parametrize("arguments", [("--verbosity=2",), ("--verbosity=-1",)])
def test_verbosity_option_matches_stock_levels(tmp_path: Path, arguments: tuple[str, ...]) -> None:
    project = VfyProject(tmp_path)
    project.write("test_levels.py", _FAILING_PROJECT)

    _differential(project, *arguments, "--no-header")


@requires_native_pytest
@pytest.mark.parametrize(
    "flag",
    [
        "--show-capture=no",
        "--force-short-summary",
        "--no-fold-skipped",
        "--xfail-tb",
        "--max-warnings=1",
        "--pdb",
    ],
)
def test_unimplemented_stock_options_stay_refused(tmp_path: Path, flag: str) -> None:
    # Accepting these would silently drop behaviour that vfy does not implement.
    project = VfyProject(tmp_path)
    project.write("test_refused.py", _FAILING_PROJECT)

    result = project.run_engine("native", flag)

    assert result.returncode == 4
    assert "unrecognized arguments" in result.stderr
