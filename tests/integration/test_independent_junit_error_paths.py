"""Independent JUnit documents for collection and usage errors match pytest's."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _document(path: Path) -> ET.Element:
    root = ET.parse(path).getroot()
    for element in root.iter():
        for attribute in ("time", "timestamp", "hostname"):
            element.attrib.pop(attribute, None)
    return root


@pytest.mark.parametrize("family", ["xunit1", "xunit2"])
def test_worker_collection_errors_match_pytest_junit(tmp_path: Path, family: str) -> None:
    project = VfyProject(tmp_path)
    project.write("test_raise.py", "raise RuntimeError('module boom sentinel')\n")
    project.write(
        "test_notrace.py",
        "import pytest\npytest.fail('collect boom sentinel', pytrace=False)\n",
    )
    project.write("test_ok.py", "def test_fine():\n    assert True\n")
    options = ("-q", "-o", f"junit_family={family}")
    stock = project.run_pytest(*options, "--junitxml=stock.xml", load_xdist=False)
    native = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        *options,
        "--junitxml=native.xml",
    )
    assert stock.returncode == native.returncode == 2, (
        stock.stdout + stock.stderr + native.stdout + native.stderr
    )
    assert ET.tostring(_document(tmp_path / "native.xml")) == ET.tostring(
        _document(tmp_path / "stock.xml")
    )


@pytest.mark.parametrize("workers", ["1", "2"])
@pytest.mark.parametrize("target", ["missing.py", "test_a.py::nope"])
def test_usage_error_writes_junit_document_like_pytest(
    tmp_path: Path, workers: str, target: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_a.py", "def test_a():\n    pass\n")
    options = ("-q", "-o", "junit_family=xunit2")
    stock = project.run_pytest(*options, "--junitxml=stock.xml", target, load_xdist=False)
    native = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        *options,
        "--junitxml=native.xml",
        target,
    )
    assert stock.returncode == native.returncode == 4, (
        stock.stdout + stock.stderr + native.stdout + native.stderr
    )
    assert ET.tostring(_document(tmp_path / "native.xml")) == ET.tostring(
        _document(tmp_path / "stock.xml")
    )
