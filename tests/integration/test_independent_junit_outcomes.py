"""Independent JUnit output agrees with pytest's public XML diagnostics."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("family", ["xunit1", "xunit2"])
def test_junit_outcomes_and_diagnostics_match_pytest(tmp_path: Path, family: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_cases.py",
        """import pytest

@pytest.fixture
def broken():
    raise RuntimeError('setup error sentinel')

def test_pass(record_property, record_xml_attribute, record_testsuite_property):
    print('capture sentinel <&')
    record_property('owner', 'team <&')
    record_xml_attribute('custom', 'value <&')
    record_testsuite_property('build', 'nightly <&')

def test_failure():
    assert False, 'failure sentinel'

def test_error(broken):
    pass

def test_skip():
    pytest.skip('skip sentinel')

@pytest.mark.xfail(reason='xfail sentinel')
def test_xfail():
    assert False

@pytest.mark.xfail(reason='xpass sentinel')
def test_xpass():
    pass
""",
    )
    options = ("-q", "-o", f"junit_family={family}", "-o", "junit_logging=all")
    stock = project.run_pytest(*options, "--junitxml=stock.xml", load_xdist=False)
    native = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=1",
        *options,
        "--junitxml=native.xml",
    )
    assert stock.returncode == native.returncode == 1, (
        stock.stdout + stock.stderr + native.stdout + native.stderr
    )

    def document(path: Path) -> ET.Element:
        root = ET.parse(path).getroot()
        for element in root.iter():
            for attribute in ("time", "timestamp", "hostname"):
                element.attrib.pop(attribute, None)
        return root

    stock_xml = document(tmp_path / "stock.xml")
    native_xml = document(tmp_path / "native.xml")
    assert ET.tostring(native_xml) == ET.tostring(stock_xml)


@pytest.mark.parametrize("family", ["xunit1", "xunit2"])
def test_registered_namespace_attribute_matches_pytest(tmp_path: Path, family: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_namespace.py",
        """import xml.etree.ElementTree as ET

ET.register_namespace('current', 'urn:example:registered')

def test_namespaced(record_xml_attribute):
    record_xml_attribute('{urn:example:registered}key', 'registered value')
    record_xml_attribute('{urn:example:unregistered}key', 'unregistered value')
""",
    )
    options = ("-q", "-o", f"junit_family={family}")
    stock = project.run_pytest(*options, "--junitxml=stock.xml", load_xdist=False)
    native = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=1",
        *options,
        "--junitxml=native.xml",
    )
    assert stock.returncode == native.returncode == 0, (
        stock.stdout + stock.stderr + native.stdout + native.stderr
    )
    stock_text = (tmp_path / "stock.xml").read_text()
    native_text = (tmp_path / "native.xml").read_text()
    stock_case = ET.fromstring(stock_text).find("./testsuite/testcase")
    native_case = ET.fromstring(native_text).find("./testsuite/testcase")
    assert stock_case is not None
    assert native_case is not None
    stock_case.attrib.pop("time")
    native_case.attrib.pop("time")
    assert native_case.attrib == stock_case.attrib
    if family == "xunit1":
        assert 'xmlns:current="urn:example:registered"' in stock_text
        assert 'xmlns:current="urn:example:registered"' in native_text
        assert 'current:key="registered value"' in native_text
        assert 'ns1:key="unregistered value"' in native_text


def test_namespace_registered_during_test_matches_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_late_namespace.py",
        """import xml.etree.ElementTree as ET

def test_late_namespace(record_xml_attribute):
    ET.register_namespace('late', 'urn:example:late')
    record_xml_attribute('{urn:example:late}key', 'late value')
""",
    )
    options = ("-q", "-o", "junit_family=xunit1")
    stock = project.run_pytest(*options, "--junitxml=stock.xml", load_xdist=False)
    native = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=1",
        *options,
        "--junitxml=native.xml",
    )
    assert stock.returncode == native.returncode == 0, (
        stock.stdout + stock.stderr + native.stdout + native.stderr
    )
    for name in ("stock", "native"):
        text = (tmp_path / f"{name}.xml").read_text()
        assert 'xmlns:late="urn:example:late"' in text
        assert 'late:key="late value"' in text
