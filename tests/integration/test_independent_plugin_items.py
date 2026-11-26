"""Plug-in collectors and items match pytest through the public protocol."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.support import ENGINE_NAMES, CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _run(project: VfyProject, engine: str, *arguments: str) -> CommandResult:
    return project.run_engine(engine, "-q", *arguments, stock=("-p", "no:cacheprovider"))


def _output(result: CommandResult) -> list[str]:
    return [re.sub(r" in [0-9.]+s$", "", line) for line in result.stdout.splitlines()]


def _differential(project: VfyProject, *arguments: str) -> CommandResult:
    stock, independent = (_run(project, engine, *arguments) for engine in ENGINE_NAMES)
    assert independent.returncode == stock.returncode, independent.stdout + independent.stderr
    assert _output(independent) == _output(stock)
    return independent


def test_collect_file_items_run_and_report_like_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest


class UsecaseError(Exception):
    pass


def pytest_collect_file(parent, file_path):
    if file_path.suffix == ".case" and file_path.name.startswith("test"):
        return CaseFile.from_parent(parent, path=file_path)


class CaseFile(pytest.File):
    def collect(self):
        for line in self.path.read_text().splitlines():
            name, _, expected = line.partition("=")
            yield CaseItem.from_parent(self, name=name, expected=expected)


class CaseItem(pytest.Item):
    def __init__(self, *, expected, **kwargs):
        super().__init__(**kwargs)
        self.expected = expected

    def runtest(self):
        if self.expected != "ok":
            raise UsecaseError(self, self.expected)

    def repr_failure(self, excinfo):
        if isinstance(excinfo.value, UsecaseError):
            return f"usecase execution failed: {excinfo.value.args[1]}"
        return super().repr_failure(excinfo)

    def reportinfo(self):
        return self.path, 0, f"usecase: {self.name}"
""",
    )
    project.write("test_cases.case", "first=ok\nsecond=broken\nthird=ok\n")
    project.write("test_plain.py", "def test_plain():\n    pass\n")

    result = _differential(project)

    assert result.returncode == 1
    assert "_ usecase: second _" in result.stdout


def test_item_repr_failure_filters_its_traceback_like_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest

hide_arguments = []


def hide_when_called(excinfo):
    hide_arguments.append(type(excinfo).__name__)
    return True


def hidden():
    __tracebackhide__ = True
    conditional()


def conditional():
    __tracebackhide__ = hide_when_called
    visible()


def visible():
    raise ValueError("boom")


def pytest_collect_file(parent, file_path):
    if file_path.suffix == ".case":
        return CaseFile.from_parent(parent, path=file_path)


class CaseFile(pytest.File):
    def collect(self):
        yield CaseItem.from_parent(self, name="case")


class CaseItem(pytest.Item):
    def runtest(self):
        hidden()

    def repr_failure(self, excinfo):
        def names(traceback):
            return [entry.name for entry in traceback if entry.path.name == "conftest.py"]

        lines = [
            f"all: {names(excinfo.traceback)}",
            f"excinfo: {names(excinfo.traceback.filter(excinfo))}",
            f"hide_arguments: {hide_arguments}",
            f"callable: {names(excinfo.traceback.filter(lambda entry: entry.name != 'visible'))}",
        ]
        try:
            excinfo.traceback.filter()
        except TypeError:
            lines.append("no filter: TypeError")
        return "\\n".join(lines)

    def reportinfo(self):
        return self.path, 0, f"case: {self.name}"
""",
    )
    project.write("test_items.case", "")

    result = _differential(project)

    assert result.returncode == 1
    assert "excinfo: ['runtest', 'visible']" in result.stdout
    assert "hide_arguments: ['ExceptionInfo']" in result.stdout


def test_doctest_modules_failure_matches_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pkg/mod.py",
        '''def add(a, b):
    """
    >>> add(1, 2)
    3
    >>> add(2, 2)
    5
    """
    return a + b


def sub(a, b):
    """
    >>> sub(3, 1)
    2
    """
    return a - b
''',
    )
    project.write("pkg/__init__.py", "")

    result = _differential(project, "--doctest-modules", "pkg")

    assert result.returncode == 1
    assert "[doctest] pkg.mod.add" in result.stdout


@pytest.mark.parametrize("hook_result", ["None", "[]"])
def test_pycollect_makeitem_marks_reach_native_items(tmp_path: Path, hook_result: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"""import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makeitem(collector, name, obj):
    if collector.istestfunction(obj, name) and name.endswith("_slow"):
        pytest.mark.skip(reason="slow")(obj)
    return {hook_result} if name == "helper_value" else None
""",
    )
    project.write(
        "test_marks.py",
        "helper_value = 1\n\n\ndef test_fast():\n    pass\n\n\ndef test_case_slow():\n    pass\n",
    )

    result = _differential(project, "-rs")

    assert result.returncode == 0


def test_conftest_import_error_is_reported_like_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("tests/conftest.py", "import helper_that_does_not_exist\n")
    project.write("tests/test_body.py", "def test_body():\n    pass\n")

    stock, independent = (_run(project, engine) for engine in ENGINE_NAMES)

    assert independent.returncode == stock.returncode == 4
    assert independent.stderr == stock.stderr


def test_package_initializer_imports_as_its_package(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pkg/__init__.py",
        'def engine():\n    """\n    >>> engine()\n    1\n    """\n    return 1\n',
    )
    project.write(
        "tests/test_pkg.py", "import pkg\n\n\ndef test_engine():\n    assert pkg.engine() == 1\n"
    )

    result = _differential(project, "--doctest-modules", "--import-mode=importlib")

    assert result.returncode == 0
