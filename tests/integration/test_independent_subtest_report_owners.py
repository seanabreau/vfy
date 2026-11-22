"""Unittest child snapshots retain canonical worker JUnit attachments."""

from __future__ import annotations

import json
import re
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

_HOOKS = """import json
import os
from pathlib import Path
from xml.etree import ElementTree
import pytest

reports = []

@pytest.fixture(autouse=True)
def metadata(request, record_property, record_xml_attribute):
    label = request.node.nodeid
    ElementTree.register_namespace('owned', 'urn:subtest-owner')
    record_property('owner', label)
    record_property('duplicate', label + '-first')
    record_property('duplicate', label + '-second')
    record_xml_attribute('owner', label)
    record_xml_attribute('{urn:subtest-owner}key', label + '-namespaced')

def pytest_runtest_logreport(report):
    context = getattr(report, 'context', None)
    reports.append({
        'nodeid': report.nodeid, 'when': report.when, 'outcome': report.outcome,
        'context': None if context is None else [
            [key, str(value)] for key, value in context.kwargs.items()],
        'stdout': report.capstdout, 'stderr': report.capstderr,
    })

def pytest_sessionfinish():
    Path(f'reports-{os.getpid()}.json').write_text(json.dumps(reports))
"""

_CASE = """import sys
import unittest

class TestOwners(unittest.TestCase):
    def exercise(self):
        for index in range(2):
            with self.subTest(index=index):
                label = LABEL + ':' + self._testMethodName + ':' + str(index)
                print('stdout-' + label)
                print('stderr-' + label, file=sys.stderr)
                self.assertLess(index, 2)

    def test_left(self):
        self.exercise()

    def test_right(self):
        self.exercise()
"""


def _junit_cases(path: Path) -> dict[str, tuple[dict[str, str], list[list[str]], str]]:
    result = {}
    for case in ET.parse(path).findall("./testsuite/testcase"):
        label = case.attrib["owner"]
        assert label not in result, f"duplicate JUnit owner: {label}"
        attributes = {key: value for key, value in case.attrib.items() if key != "time"}
        properties = [
            [entry.attrib["name"], entry.attrib["value"]]
            for entry in case.findall("./properties/property")
        ]
        captured = "\n".join(
            entry.text or ""
            for entry in case.findall("./system-out") + case.findall("./system-err")
        )
        result[label] = attributes, properties, captured
    return result


def _suite_clock(path: Path) -> tuple[str, bytes]:
    payload = path.read_bytes()
    suite = ET.fromstring(payload).find("./testsuite")
    assert suite is not None
    clock = suite.attrib["time"]
    prefix, marker, tail = payload.partition(b"<testsuite ")
    opening, closing, suffix = tail.partition(b">")
    assert marker
    assert closing
    attribute = f' time="{clock}"'.encode()
    assert opening.count(attribute) == 1
    return clock, prefix + marker + opening.replace(attribute, b' time=""') + closing + suffix


@pytest.mark.parametrize("workers", [1, 2])
def test_unittest_subtest_snapshots_preserve_canonical_junit_owners(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", _CONFIG)
    project.write("conftest.py", _HOOKS)
    for label in ("first", "second"):
        project.write(f"test_{label}.py", f"LABEL = {label!r}\n" + _CASE)
    oracle = project.run_python("-m", "pytest", "-q", "--junitxml=stock.xml")
    oracle.expect(0)
    expected = _junit_cases(tmp_path / "stock.xml")
    observations = list(tmp_path.glob("reports-*.json"))
    assert len(observations) == 1
    reports = json.loads(observations[0].read_text())
    assert len([report for report in reports if report["context"] is not None]) == 8
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "-q",
        "-rP",
        "--junitxml=result.xml",
        "--native-result-json=native.json",
    )
    diagnostic = candidate.stdout + candidate.stderr
    assert candidate.returncode == 0, diagnostic
    actual = _junit_cases(tmp_path / "result.xml")
    assert len(actual) == len(expected) == 4, diagnostic
    assert actual.keys() == expected.keys(), diagnostic
    for label, (attributes, properties, captured) in actual.items():
        assert (attributes, properties) == expected[label][:2], diagnostic
        assert properties == [
            ["owner", label],
            ["duplicate", label + "-first"],
            ["duplicate", label + "-second"],
        ], diagnostic
        filename, _, method = label.split("::")
        module = Path(filename).stem.removeprefix("test_")
        for index in range(2):
            for stream in ("stdout", "stderr"):
                payload = f"{stream}-{module}:{method}:{index}"
                assert payload in captured, diagnostic
                assert payload in expected[label][2], diagnostic
    native = json.loads((tmp_path / "native.json").read_text())
    assert native["counts"]["passed"] == 4, diagnostic
    assert native["counts"]["subtests_passed"] == 8, diagnostic
    topology = native["execution_topology"]
    assert topology["requested_workers"] == topology["active_workers"] == workers, diagnostic
    assert len(topology["worker_pids"]) == len(set(topology["worker_pids"])) == workers, diagnostic
    assert len(native["cases"]) == 4, diagnostic
    for case in native["cases"]:
        properties = expected[case["nodeid"]][1]
        assert case["user_properties"] == [list(pair) for pair in properties], diagnostic
        stock = [report for report in reports if report["nodeid"] == case["nodeid"]]
        parents = [report for report in stock if report["context"] is None]
        assert [(phase["phase"], phase["outcome"]) for phase in case["phases"]] == [
            (report["when"], report["outcome"]) for report in parents
        ], diagnostic
        children = [report for report in stock if report["context"] is not None]
        assert len(case["subtests"]) == len(children) == 2, diagnostic
        assert [
            [
                child["outcome"],
                child["report"]["context"]["kwargs"],
                child["report"]["stdout"],
                child["report"]["stderr"],
            ]
            for child in case["subtests"]
        ] == [
            [report["outcome"], report["context"], report["stdout"], report["stderr"]]
            for report in children
        ], diagnostic
    original_time, _ = _suite_clock(tmp_path / "result.xml")
    if workers == 1:
        assert re.fullmatch(r"[0-9]+\.[0-9]{3}", original_time), diagnostic
        assert 0 <= float(original_time) <= native["duration_ms"] / 1000 + 0.0005, diagnostic
