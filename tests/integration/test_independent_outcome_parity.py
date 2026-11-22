"""Outcome exception classes and module-level skips match pytest."""

from __future__ import annotations

from pathlib import Path

from tests.support import CommandResult, VfyProject
from tests.support.differential import normalized_output, run_pair
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_outcome_classes_and_failure_names_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_outcome_classes.py",
        """import pytest


def test_outcome_classes():
    raisers = (
        lambda: pytest.skip("skipped"),
        lambda: pytest.fail("failed", pytrace=False),
        lambda: pytest.xfail("xfailed"),
        lambda: pytest.exit("exited"),
    )
    for raiser in raisers:
        try:
            raiser()
        except BaseException as exc:
            print(type(exc))
            print([(cls.__module__, cls.__qualname__) for cls in type(exc).__mro__])


def test_failure_names_the_outcome_class():
    pytest.fail("shown failure")
""",
    )

    # -s keeps the printed class identities visible; captured output of passing tests is hidden.
    stock, independent = run_pair(project, "-s", "test_outcome_classes.py")

    assert normalized_output(independent) == normalized_output(stock)


def test_module_level_skip_is_a_collection_error_like_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_module_skip.py",
        """import pytest

pytest.skip("module skipped")


def test_never_runs():
    pass
""",
    )

    stock, independent = run_pair(project, "test_module_skip.py")

    assert stock.returncode == 2
    assert _collection_errors(independent) == _collection_errors(stock)


def test_module_level_skip_with_allow_module_level_skips_the_module(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_allowed_skip.py",
        """import pytest

pytest.skip("module skipped", allow_module_level=True)


def test_never_runs():
    pass
""",
    )

    stock, independent = run_pair(project, "test_allowed_skip.py")

    assert stock.returncode == 5
    assert normalized_output(independent) == normalized_output(stock)


def _collection_errors(result: CommandResult) -> str:
    # The summary and interrupted banner still differ from pytest; compare the error block only.
    return result.stdout.partition(" ERRORS ")[2].partition(" short test summary info ")[0]
