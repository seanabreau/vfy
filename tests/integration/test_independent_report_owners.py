"""Worker terminal snapshots retain the stock JUnit property and attribute homes."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CONFIG = """[pytest]
junit_family = legacy
junit_logging = all
"""

_CASE = """import sys
from xml.etree import ElementTree

def test_value(record_property, record_xml_attribute):
    ElementTree.register_namespace('owned', 'urn:report-owner')
    record_property('case', LABEL)
    record_property('duplicate', LABEL + '-first')
    record_property('duplicate', LABEL + '-second')
    record_xml_attribute('owner', LABEL)
    record_xml_attribute('{urn:report-owner}key', LABEL + '-namespaced')
    print('stdout-' + LABEL)
    print('stderr-' + LABEL, file=sys.stderr)
"""


def _cases(path: Path) -> dict[str, tuple[dict[str, str], list[tuple[str, str]], str]]:
    result = {}
    for case in ET.parse(path).findall("./testsuite/testcase"):
        label = case.attrib["owner"]
        assert label not in result, f"duplicate JUnit owner: {label}"
        attributes = {
            key: value
            for key, value in case.attrib.items()
            if key in {"name", "classname", "owner"} or key.startswith("{urn:report-owner}")
        }
        properties = [
            (value.attrib["name"], value.attrib["value"])
            for value in case.findall("./properties/property")
        ]
        captured = "\n".join(
            value.text or ""
            for value in case.findall("./system-out") + case.findall("./system-err")
        )
        result[label] = attributes, properties, captured
    return result


@pytest.mark.parametrize("workers", [1, 2])
def test_terminal_materialization_preserves_junit_homes_and_captured_output(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", _CONFIG)
    for label in ("first", "second"):
        project.write(f"test_{label}.py", f"LABEL = {label!r}\n" + _CASE)
    oracle = project.run_python("-m", "pytest", "-q", "--junitxml=stock.xml")
    expected = _cases(tmp_path / "stock.xml")
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "-q",
        "-rP",
        "--junitxml=result.xml",
        env={"VFY_WORKER_START": "exec"},
    )
    diagnostic = oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    assert oracle.returncode == candidate.returncode == 0, diagnostic
    actual = _cases(tmp_path / "result.xml")
    assert set(actual) == set(expected) == {"first", "second"}, diagnostic
    for label in expected:
        assert actual[label][:2] == expected[label][:2], diagnostic
        for payload in (f"stdout-{label}", f"stderr-{label}"):
            assert payload in actual[label][2], diagnostic
            assert payload in expected[label][2], diagnostic
