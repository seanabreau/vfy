"""A failed first finalization cannot invent an unobserved teardown reporter."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = """import pytest
class Name:
 def __str__(self):raise RuntimeError('actual first reporter property conversion')
@pytest.fixture
def broken():
 yield
 raise RuntimeError('actual teardown error')
"""
_TEST = """from conftest import Name
def test_case(broken,record_property,record_xml_attribute):
 record_xml_attribute('custom','actual first reporter')
 record_property(Name(),'value')
 assert False,'actual call failure'
"""


def test_failed_pending_capture_keeps_only_actual_writer_observations(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _SOURCE)
    project.write("test_case.py", _TEST)
    observations = []
    for engine in ["pytest", "vfy.independent.entry"]:
        result = project.run_python(
            "-m",
            engine,
            "-q",
            "--junitxml=results.xml",
            "--junitprefix=review.prefix",
            "-o",
            "junit_family=legacy",
        )
        result.expect(3)
        root = ET.parse(tmp_path / "results.xml")
        cases = root.findall(".//testcase")
        assert len(cases) == 2
        first, internal = cases
        assert first.attrib["classname"] == "review.prefix.test_case"
        assert first.attrib["custom"] == "actual first reporter"
        assert first.find("failure") is not None
        assert first.find("error") is None
        assert internal.attrib["name"] == "internal"
        assert internal.find("error") is not None
        suite = root.find(".//testsuite")
        assert suite is not None
        counts = {key: suite.attrib[key] for key in ["tests", "errors", "failures", "skipped"]}
        observations.append((counts, [[child.tag for child in case] for case in cases]))
    assert observations[0] == observations[1]
