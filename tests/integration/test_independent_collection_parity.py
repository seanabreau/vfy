"""Collection rules that pytest applies before and during test collection."""

from __future__ import annotations

from pathlib import Path

from tests.support import CommandResult, VfyProject
from tests.support.differential import run_matching
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_empty_parameter_set_has_one_notset_id_for_several_names(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_empty.py",
        """import pytest


@pytest.mark.parametrize(("left", "right"), [])
def test_pair(left, right):
    raise AssertionError("must not execute")


@pytest.mark.parametrize("value", [1])
def test_regular(value):
    assert value == 1
""",
    )

    result = run_matching(project, "--collect-only")

    assert result.returncode == 0
    assert "test_empty.py::test_pair[NOTSET]" in result.stdout


def test_class_with_constructor_is_not_collected_and_warns(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_init.py",
        """class TestInit:
    def __init__(self, value):
        self.value = value

    def test_never_runs(self):
        raise AssertionError("must not execute")


class TestNew:
    def __new__(cls, *args):
        return super().__new__(cls)

    def test_never_runs(self):
        raise AssertionError("must not execute")


def test_plain():
    assert True
""",
    )

    result = run_matching(project)

    assert result.returncode == 0
    assert "1 passed, 2 warnings" in result.stdout
    assert (
        "cannot collect test class 'TestInit' because it has a __init__ constructor "
        "(from: test_init.py)"
    ) in result.stdout


def test_no_python_plugin_collects_no_test_modules(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_plain.py", "def test_plain():\n    assert True\n")

    result = run_matching(project, "-p", "no:python")

    assert result.returncode == 5
    assert "no tests ran" in result.stdout


def test_continue_on_collection_errors_runs_collected_tests(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_good.py", "def test_good():\n    assert True\n")
    project.write(
        "test_bad.py",
        "raise ValueError('bad module')\n\ndef test_bad():\n    assert True\n",
    )

    result = run_matching(project, "--continue-on-collection-errors")

    assert result.returncode == 1
    assert "1 passed, 1 error" in result.stdout


def test_import_error_in_test_module_reports_stock_collect_error(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_bad.py", "import vfy_missing_module\n\n\ndef test_bad():\n    assert True\n"
    )
    project.write("test_good.py", "def test_good():\n    assert True\n")

    result = run_matching(project, "--continue-on-collection-errors")

    assert result.returncode == 1
    assert "1 passed, 1 error" in result.stdout
    assert "ImportError while importing test module" in result.stdout


def test_import_error_in_test_module_interrupts_collection_like_stock(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_bad.py", "import vfy_missing_module\n\n\ndef test_bad():\n    assert True\n"
    )
    project.write("test_good.py", "def test_good():\n    assert True\n")

    result = run_matching(project)

    assert result.returncode == 2
    assert "Interrupted: 1 error during collection" in result.stdout


def _syntax_error_lines(result: CommandResult) -> list[str]:
    # The frames above the exception differ: pytest shows its own import machinery and vfy shows
    # its own, so only the header and the exception lines are compared with pytest.
    return [
        line
        for line in result.stdout.splitlines()
        if "ERROR collecting" in line or line.startswith("E   ")
    ]


def test_syntax_error_module_reports_pytest_exception_lines(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_good.py", "def test_good():\n    assert True\n")
    project.write("test_syntax.py", "def test_syntax(:\n    pass\n")

    stock = project.run_pytest(
        "-q", "-p", "no:cacheprovider", "--continue-on-collection-errors", load_xdist=False
    )
    independent = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-q", "--continue-on-collection-errors"
    )

    assert independent.returncode == stock.returncode == 1
    assert _syntax_error_lines(independent) == _syntax_error_lines(stock)
    assert "E   SyntaxError: invalid syntax" in independent.stdout
