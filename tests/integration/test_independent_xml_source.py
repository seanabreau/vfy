"""Coverage reports retain CLI source roots across the independent controller."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize(
    "sources",
    [
        ("--cov=package",),
        ("--cov", "package"),
        ("--cov=missing", "--cov-reset", "--cov=package"),
        ("--cov=package", "--cov-append"),
        ("--cov",),
    ],
)
def test_xml_uses_the_plain_package_source_root_from_cov(
    tmp_path: Path, sources: tuple[str, ...]
) -> None:
    project = VfyProject(tmp_path)
    if sources == ("--cov",):
        project.write(".coveragerc", "[run]\nsource = package\n")
    project.write("package/__init__.py", "def value():\n    return 1\n")
    project.write(
        "test_package.py",
        "from package import value\ndef test_value():\n    assert value() == 1\n",
    )
    reports = []
    for engine in ("stock", "native"):
        destination = tmp_path / f"{engine}.xml"
        result = project.run_engine(
            engine,
            "-q",
            *sources,
            f"--cov-report=xml:{destination}",
            stock=("-p", "pytest_cov.plugin"),
            independent=("--workers=1",),
            no_cov=False,
        )
        result.expect(0)
        if engine == "stock":
            root = ET.parse(destination).getroot()
            assert [node.text for node in root.findall("sources/source")] == [
                str(tmp_path / "package")
            ]
            assert [node.attrib["name"] for node in root.findall("packages/package")] == ["."]
            assert [
                node.attrib["filename"] for node in root.findall("packages/package/classes/class")
            ] == ["__init__.py"]
        reports.append(
            ET.canonicalize(
                from_file=destination, strip_text=True, exclude_attrs={"version", "timestamp"}
            )
        )
    assert reports[1] == reports[0]


def test_empty_cov_source_is_a_literal_package_with_no_xml(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".coveragerc", "[run]\nsource = package\n")
    project.write("package/__init__.py", "VALUE = 1\n")
    project.write(
        "test_package.py", "from package import VALUE\ndef test_value():\n    assert VALUE == 1\n"
    )
    for engine in ("stock", "native"):
        destination = tmp_path / f"{engine}.xml"
        result = project.run_engine(
            engine,
            "-q",
            "--cov=",
            f"--cov-report=xml:{destination}",
            stock=("-p", "pytest_cov.plugin"),
            independent=("--workers=1",),
            no_cov=False,
            env={"COVERAGE_FILE": str(tmp_path / f".coverage.{engine}")},
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "Module  was never imported" in output, output
        assert "No data was collected" in output, output
        assert not destination.exists(), output
        data = project.data(f".coverage.{engine}")
        try:
            assert not data.measured_files()
        finally:
            data.close(force=True)


@pytest.mark.parametrize("addopts", ["ini", "environment"])
def test_xml_sources_from_addopts_survive_parent_reporting(tmp_path: Path, addopts: str) -> None:
    project = VfyProject(tmp_path)
    project.write("package/__init__.py", "VALUE = 1\n")
    project.write(
        "test_package.py", "from package import VALUE\ndef test_value():\n    assert VALUE == 1\n"
    )
    if addopts == "ini":
        project.write("pytest.ini", "[pytest]\naddopts = --cov=package\n")
    reports = []
    for engine in ("stock", "native"):
        destination = tmp_path / f"{engine}.xml"
        result = project.run_engine(
            engine,
            "-q",
            f"--cov-report=xml:{destination}",
            stock=("-p", "pytest_cov.plugin"),
            independent=("--workers=1",),
            no_cov=False,
            env={"PYTEST_ADDOPTS": "--cov=package" if addopts == "environment" else None},
        )
        result.expect(0)
        reports.append(
            ET.canonicalize(
                from_file=destination, strip_text=True, exclude_attrs={"version", "timestamp"}
            )
        )
    assert reports[1] == reports[0]


def test_xml_rates_match_stock_fractional_line_and_branch_coverage(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "package/__init__.py",
        "def choose(value):\n    if value:\n        return 1\n    return 2\n"
        "def unused():\n    return 3\n",
    )
    project.write(
        "test_package.py",
        "from package import choose\ndef test_value():\n    assert choose(True) == 1\n",
    )
    reports = []
    for engine in ("stock", "native"):
        destination = tmp_path / f"{engine}.xml"
        result = project.run_engine(
            engine,
            "-q",
            "--cov=package",
            "--cov-branch",
            f"--cov-report=xml:{destination}",
            stock=("-p", "pytest_cov.plugin"),
            independent=("--workers=1",),
            no_cov=False,
        )
        result.expect(0)
        if engine == "stock":
            attributes = ET.parse(destination).getroot().attrib
            assert (attributes["line-rate"], attributes["branch-rate"]) == ("0.6667", "0.5")
        reports.append(
            ET.canonicalize(
                from_file=destination, strip_text=True, exclude_attrs={"version", "timestamp"}
            )
        )
    assert reports[1] == reports[0]
