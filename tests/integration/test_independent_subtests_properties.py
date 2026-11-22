"""Subtest publication preserves the containing test's JUnit properties."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_PROPERTY_TRACE = """import json
from pathlib import Path
import pytest

events = []

class Value:
    def __init__(self, label):
        self.label = label
    def __str__(self):
        events.append(['value_str', self.label])
        return self.label

class Properties:
    def __init__(self, label):
        self.label = label
    def __iter__(self):
        events.append(['properties_iter', self.label])
        yield ('seen', Value(self.label))

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report):
    label = ('child:' + report.context.msg
             if isinstance(report, pytest.SubtestReport) else 'parent') + ':' + report.when
    events.append(['logreport', label, report.outcome])
    report.user_properties = Properties(label)

def pytest_unconfigure(config):
    Path('events.json').write_text(json.dumps(events))
"""


def _run_property_trace(
    tmp_path: Path, engine: str, source: str, expected_status: int
) -> tuple[list[tuple[str, str]], list[list[tuple[str, str]]]]:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\njunit_family = legacy\n")
    project.write("conftest.py", _PROPERTY_TRACE)
    project.write("test_properties.py", source)
    arguments = ["-q", "--junitxml=result.xml"]
    result = (
        project.run_python("-m", "pytest", *arguments)
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    )
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    project.write(f"{engine}.status", str(result.returncode))
    assert result.returncode == expected_status, result.stdout + result.stderr
    observations = json.loads((project.root / "events.json").read_text())
    assert isinstance(observations, list)
    events: list[tuple[str, str]] = []
    for event in observations:
        assert isinstance(event, list)
        kind, label = event[:2]
        assert isinstance(kind, str)
        assert isinstance(label, str)
        events.append((kind, label))
    owners = [
        [
            (element.attrib["name"], element.attrib["value"])
            for element in case.findall("./properties/property")
        ]
        for case in ET.parse(project.root / "result.xml").findall("./testsuite/testcase")
    ]
    return events, owners


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
def test_subtest_publication_keeps_parent_property_history(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\njunit_family = legacy\n")
    project.write(
        "test_properties.py",
        """def test_properties(subtests, record_property):
    record_property('before', 11)
    with subtests.test('child'):
        record_property('during', 29)
    record_property('after', 37)
""",
    )
    arguments = ["-q", "--junitxml=result.xml"]
    result = (
        project.run_python("-m", "pytest", *arguments)
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    )
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    project.write(f"{engine}.status", str(result.returncode))
    assert result.returncode == 0, result.stdout + result.stderr
    cases = ET.parse(project.root / "result.xml").findall("./testsuite/testcase")
    assert len(cases) == 1
    assert [
        (element.attrib["name"], element.attrib["value"])
        for element in cases[0].findall("./properties/property")
    ] == [("before", "11"), ("during", "29"), ("after", "37")]


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
def test_failed_teardown_finalizes_first_actual_pending_child(tmp_path: Path, engine: str) -> None:
    events, owners = _run_property_trace(
        tmp_path,
        engine,
        """import pytest

@pytest.fixture
def cleanup():
    yield
    raise RuntimeError('teardown')

def test_properties(subtests, cleanup):
    with subtests.test('first'):
        raise RuntimeError('first')
    with subtests.test('second'):
        raise RuntimeError('second')
    with subtests.test('third'):
        pass
""",
        1,
    )
    expected = ["child:first:call", "parent:teardown"]
    assert [event[1] for event in events if event[0] == "properties_iter"] == expected
    assert [event[1] for event in events if event[0] == "value_str"] == expected
    assert owners == [[("seen", owner)] for owner in expected]
