"""Native workers preserve already converted property and attribute semantics."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_SOURCE = """import json
from pathlib import Path
import pytest

def event(label, value):
    with Path('events-' + label + '.jsonl').open('a') as output:
        output.write(json.dumps(value) + '\\n')

class Name:
    def __init__(self, label, kind):
        self.label, self.kind = label, kind
    def __str__(self):
        event(self.label, self.kind + '-name')
        return self.kind + '-name'

class Value:
    def __init__(self, label, kind):
        self.label, self.kind = label, kind
    def __str__(self):
        event(self.label, self.kind + '-value')
        return self.label + '<&' + self.kind

class SuiteValue:
    def __init__(self, label, recorder):
        self.label, self.recorder = label, recorder
    def __str__(self):
        event(self.label, 'suite-value')
        self.recorder('inner', self.label)
        return self.label

@pytest.fixture
def records(request, record_property, record_xml_attribute, record_testsuite_property):
    label = request.node.name[-1]
    event(label, 'fixture')
    record_testsuite_property('outer', SuiteValue(label, record_testsuite_property))
    record_property(Name(label, 'property'), Value(label, 'property'))
    record_xml_attribute(Name(label, 'attribute'), Value(label, 'attribute'))
    event(label, 'yield')
    yield label
    event(label, 'finalizer')
    record_xml_attribute('late', label + '<&late')
    event(label, 'finalizer-done')
"""
_TEST = """from conftest import event

def test_LABEL(records):
    event(records, 'body')
    assert not BROKEN
"""
_EXPECTED_EVENTS = [
    "fixture",
    "suite-value",
    "attribute-value",
    "attribute-name",
    "yield",
    "body",
    "finalizer",
    "finalizer-done",
    "property-value",
    "property-name",
]


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("broken", [False, True])
def test_worker_semantic_names_values_and_globals_keep_actual_callback_boundaries(
    tmp_path: Path, engine: str, broken: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _SOURCE)
    project.write("pytest.ini", "[pytest]\n")
    for label in ("a", "b"):
        project.write(
            f"test_{label}.py",
            _TEST.replace("LABEL", label).replace("BROKEN", repr(broken and label == "b")),
        )
    arguments = ("-q", "--junitxml=results.xml", "-o", "junit_family=legacy")
    result = (
        project.run_python("-m", "pytest", *arguments)
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", "-j", "2", *arguments)
    )
    project.write("stdout", result.stdout)
    project.write("stderr", result.stderr)
    assert result.returncode == int(broken), result.stdout + result.stderr
    suite = ET.parse(tmp_path / "results.xml").find("testsuite")
    assert suite is not None
    assert [
        (node.attrib["name"], node.attrib["value"])
        for node in suite.findall("./properties/property")
    ] == [
        ("inner", "a"),
        ("outer", "a"),
        ("inner", "b"),
        ("outer", "b"),
    ]
    cases = suite.findall("testcase")
    assert len(cases) == 2
    assert sum(case.find("failure") is not None for case in cases) == int(broken)
    for label, case in zip(("a", "b"), cases, strict=True):
        assert case.attrib["name"] == "test_" + label
        assert case.attrib["attribute-name"] == label + "<&attribute"
        assert case.attrib["late"] == label + "<&late"
        assert [
            (node.attrib["name"], node.attrib["value"])
            for node in case.findall("./properties/property")
        ] == [
            ("property-name", label + "<&property"),
        ]
        events = [
            json.loads(line)
            for line in (tmp_path / f"events-{label}.jsonl").read_text().splitlines()
        ]
        assert events == _EXPECTED_EVENTS
