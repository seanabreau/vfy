"""Explicit file targets reach pytest-compatible file collectors."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("suffix", ["txt", "rst"])
def test_explicit_doctest_file_matches_pytest(tmp_path: Path, suffix: str) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        target = f"example.{suffix}"
        project.write(target, ">>> 6 * 7\n42\n")
        project.write(
            "conftest.py",
            "def pytest_ignore_collect(collection_path, config):\n"
            "    return collection_path.name.startswith('example.')\n",
        )
        project.write("test_other.py", "def test_other():\n    assert False\n")
        args = ("-q", target, "--junitxml=results.xml")
        result = (
            project.run_pytest(*args, load_xdist=False)
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", *args)
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        cases = [
            (case.get("classname"), case.get("name"), [child.tag for child in case])
            for case in ET.parse(project.root / "results.xml").iter("testcase")
        ]
        assert len(cases) == 1, output
        observations.append(cases)
    assert observations[0] == observations[1]


def test_explicit_custom_file_collector_matches_pytest(tmp_path: Path) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            """import pytest
class CaseFile(pytest.File):
    def collect(self):
        yield CaseItem.from_parent(self, name="case")
class CaseItem(pytest.Item):
    def runtest(self):
        assert self.path.read_text() == "payload"
def pytest_collect_file(file_path, parent):
    if file_path.suffix == ".case":
        return CaseFile.from_parent(parent, path=file_path)
""",
        )
        project.write("example.case", "payload")
        args = ("-q", "example.case", "--junitxml=results.xml")
        result = (
            project.run_pytest(*args, load_xdist=False)
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", *args)
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        cases = [
            (case.get("classname"), case.get("name"), [child.tag for child in case])
            for case in ET.parse(project.root / "results.xml").iter("testcase")
        ]
        assert len(cases) == 1, output
        observations.append(cases)
    assert observations[0] == observations[1]


def test_unhandled_explicit_file_is_a_usage_error(tmp_path: Path) -> None:
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("example.unknown", "payload")
        project.write("test_other.py", "def test_other():\n    assert False\n")
        args = ("-q", "example.unknown")
        result = (
            project.run_pytest(*args, load_xdist=False)
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", *args)
        )
        assert result.returncode == 4, result.stdout + result.stderr


def test_uncollected_explicit_doctest_is_a_usage_error(tmp_path: Path) -> None:
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("example.txt", ">>> 1 + 1\n2\n")
        args = ("-q", "-p", "no:doctest", "example.txt")
        result = (
            project.run_pytest(*args, load_xdist=False)
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", *args)
        )
        assert result.returncode == 4, result.stdout + result.stderr
