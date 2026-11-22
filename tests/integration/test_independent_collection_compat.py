"""Module and class collection details that match pytest's Python collector."""

from __future__ import annotations

import getpass
import re
import shutil
from pathlib import Path

from tests.support import ENGINE_NAMES, CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _run(project: VfyProject, engine: str, *arguments: str) -> CommandResult:
    return project.run_engine(engine, "-q", *arguments, stock=("-p", "no:cacheprovider"))


def _differential(project: VfyProject, *arguments: str) -> CommandResult:
    stock, independent = (_run(project, engine, *arguments) for engine in ENGINE_NAMES)
    assert independent.returncode == stock.returncode, independent.stdout + independent.stderr
    clean = [re.sub(r" in [0-9.]+s$", "", line) for line in stock.stdout.splitlines()]
    assert [re.sub(r" in [0-9.]+s$", "", line) for line in independent.stdout.splitlines()] == clean
    return independent


def test_xunit_setup_and_teardown_functions_run_in_pytest_order(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_xunit.py",
        """events = []


def setup_module(module):
    events.append("setup_module")


def teardown_module():
    events.append("teardown_module")
    print(events)


def setup_function(function):
    events.append(f"setup_function {function.__name__}")


def teardown_function(function):
    events.append(f"teardown_function {function.__name__}")


def test_function():
    assert events[-1] == "setup_function test_function"


class TestMethods:
    @classmethod
    def setup_class(cls):
        events.append("setup_class")
        cls.shared = 1

    @classmethod
    def teardown_class(cls):
        events.append("teardown_class")

    def setup_method(self, method):
        events.append(f"setup_method {method.__name__}")
        self.value = 2

    def teardown_method(self):
        events.append("teardown_method")

    def test_method(self):
        assert (self.shared, self.value) == (1, 2)
        assert "setup_function test_method" not in events
""",
    )

    result = _differential(project, "-s")

    assert result.returncode == 0
    assert "'teardown_method', 'teardown_class', 'teardown_module']" in result.stdout


def test_single_name_sequence_unpacks_each_parameter_value(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_values.py",
        """import pytest


@pytest.mark.parametrize(("value",), [(1,), (2,)])
def test_sequence(value):
    assert isinstance(value, int)


@pytest.mark.parametrize("value", [(1,), (2,)])
def test_string(value):
    assert isinstance(value, tuple)
""",
    )

    assert _differential(project).returncode == 0


def test_module_globals_that_raise_on_lookup_do_not_stop_collection(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_proxy.py",
        """class Unbound:
    def __getattr__(self, name):
        raise RuntimeError("working outside of a context")


current = Unbound()


def test_body():
    pass
""",
    )

    assert _differential(project).returncode == 0


def test_mock_patch_arguments_are_not_requested_as_fixtures(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_patched.py",
        """import os
from unittest import mock

import pytest


@pytest.fixture
def value():
    return 3


@mock.patch("os.getcwd")
@mock.patch("os.getpid", return_value=7)
def test_patched(getpid, getcwd, value):
    getcwd.return_value = "/patched"
    assert (os.getcwd(), os.getpid(), value) == ("/patched", 7, 3)
""",
    )

    assert _differential(project).returncode == 0


def test_assertion_assignment_expressions_stay_in_their_function(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_walrus.py",
        """def read(value):
    return value


def test_plain():
    owner = read(1)
    assert owner is not None


def test_walrus():
    lock = 2
    assert (owner := read(lock)) is not None
    assert owner == 2
""",
    )

    assert _differential(project).returncode == 0


def test_marks_copied_by_wrapping_decorators_parametrize_the_wrapper(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_wrapped.py",
        """import functools

import pytest


def copy_marks(function):
    @functools.wraps(function, assigned=("__module__", "__name__", "__qualname__"), updated=())
    def wrapper(*args, **kwargs):
        return function(*args, **kwargs)

    wrapper.pytestmark = function.pytestmark
    return wrapper


@copy_marks
@pytest.mark.parametrize("value", [1, 2])
def test_wrapped(value):
    assert value in (1, 2)


@pytest.mark.parametrize("left,right,", [(1, 1), (2, 2)])
def test_trailing_comma(left, right):
    assert left == right
""",
    )

    assert _differential(project).returncode == 0


def test_class_like_proxies_are_not_test_cases(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_proxy_class.py",
        """import unittest


class Proxy:
    def __init__(self, wrapped):
        self.__wrapped__ = wrapped

    @property
    def __class__(self):
        return type


class Base:
    pass


proxied = Proxy(Base)


class TestReal(unittest.TestCase):
    def test_body(self):
        pass
""",
    )

    assert _differential(project).returncode == 0


def test_default_basetemp_uses_pytest_numbered_layout(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    temproot = tmp_path / "temproot"
    temproot.mkdir()
    project.write(
        "test_layout.py",
        """import getpass


def test_layout(tmp_path):
    numbered = tmp_path.parent
    root = numbered.parent
    assert root.name == f"pytest-of-{getpass.getuser()}"
    assert numbered.name.startswith("pytest-")
    assert (root / "pytest-current").resolve() == numbered
""",
    )
    environment = {"PYTEST_DEBUG_TEMPROOT": str(temproot)}

    root = temproot / f"pytest-of-{getpass.getuser()}"

    for _ in range(5):
        stock = project.run_pytest(
            "-q", "-p", "no:cacheprovider", env=environment, load_xdist=False
        )
        stock.expect(0)
    stock_dirs = sorted(path.name for path in root.iterdir())
    shutil.rmtree(root)
    for _ in range(5):
        vfy = project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=environment)
        vfy.expect(0)
    vfy_dirs = sorted(path.name for path in root.iterdir())

    assert vfy_dirs == stock_dirs == ["pytest-2", "pytest-3", "pytest-4", "pytest-current"]
