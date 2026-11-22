"""Controller evidence retains completed reports across normalization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.skipif(int(pytest.version_tuple[0]) < 9, reason="pytest 9 provides subtests")
def test_duplicate_nodeids_and_subtests_produce_same_result_evidence(tmp_path: Path) -> None:
    evidence = []
    for module in ("pytest", "vfy.independent.entry"):
        project = VfyProject(tmp_path / module)
        project.write(
            "test_cases.py",
            """import unittest

class TestValues(unittest.TestCase):
    def test_values(self):
        with self.subTest(value=1):
            self.assertEqual(1, 2)
""",
        )
        result_path = project.root / "evidence.json"
        plugins = (
            ("-p", "vfy.pytest_plugin") if module == "pytest" else ("-p", "vfy.independent.results")
        )
        result = project.run_python(
            "-m",
            module,
            *plugins,
            "-q",
            "--keep-duplicates",
            "test_cases.py",
            "test_cases.py",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "VFY_TEST_RESULTS": str(result_path)},
        )
        result.expect(1)
        document = json.loads(result_path.read_text())
        # Duplicate collected cases fold into one identity; the independent
        # controller also marks the resulting count mismatch incomplete.
        assert document["infrastructure_failed"] is (module == "vfy.independent.entry")
        evidence.append(
            [
                (entry["nodeid"], entry["outcome"], entry.get("failed_phase"))
                for entry in document["tests"]
            ]
        )
    assert (
        evidence[0] == evidence[1] == [("test_cases.py::TestValues::test_values", "failed", "call")]
    )


def test_protocol_wrapper_late_report_edit_reaches_controller_evidence(tmp_path: Path) -> None:
    observed = []
    for module in ("pytest", "vfy.independent.entry"):
        project = VfyProject(tmp_path / module)
        project.write("test_body.py", "def test_body(): pass\n")
        project.write(
            "conftest.py",
            """import json
from pathlib import Path
import pytest

reports = []

def pytest_runtest_logreport(report):
    reports.append(report)

@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    result = yield
    assert [report.when for report in reports] == ['setup', 'call', 'teardown']
    reports[1].outcome = 'failed'
    reports[1].duration = 1.25
    Path('observed.json').write_text(json.dumps([reports[1].outcome, reports[1].duration]))
    return result
""",
        )
        plugins = (
            ("-p", "vfy.pytest_plugin") if module == "pytest" else ("-p", "vfy.independent.results")
        )
        result_path = project.root / "evidence.json"
        result = project.run_python(
            "-m",
            module,
            *plugins,
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "VFY_TEST_RESULTS": str(result_path)},
        )
        assert result.returncode in (0, 1), result.stdout + result.stderr
        observed.append(json.loads((project.root / "observed.json").read_text()))
        if module == "vfy.independent.entry":
            document = json.loads(result_path.read_text())
            assert not document["infrastructure_failed"]
            assert len(document["tests"]) == 1
            entry = document["tests"][0]
            assert (
                entry["nodeid"],
                entry["outcome"],
                entry["failed_phase"],
                entry["call_duration_ms"],
            ) == ("test_body.py::test_body", "failed", "call", 1250.0)
    assert observed == [["failed", 1.25], ["failed", 1.25]]


@pytest.mark.skipif(int(pytest.version_tuple[0]) < 9, reason="pytest 9 provides subtests")
def test_teardown_subtest_keeps_report_order_in_controller_evidence(tmp_path: Path) -> None:
    documents = []
    for module in ("pytest", "vfy.independent.entry"):
        project = VfyProject(tmp_path / module)
        project.write(
            "conftest.py",
            """import pytest

@pytest.fixture
def later(subtests):
    yield
    with subtests.test(stage='teardown'):
        assert False, 'teardown subtest sentinel'

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    report.duration = 0.25
    return report

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report):
    if hasattr(report, 'context'):
        report.duration = 0.75
""",
        )
        project.write("test_case.py", "def test_case(later): pass\n")
        result_path = project.root / "evidence.json"
        plugins = (
            ("-p", "vfy.pytest_plugin") if module == "pytest" else ("-p", "vfy.independent.results")
        )
        result = project.run_python(
            "-m",
            module,
            *plugins,
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "VFY_TEST_RESULTS": str(result_path)},
        )
        result.expect(1)
        assert "teardown subtest sentinel" in result.stdout + result.stderr
        documents.append(json.loads(result_path.read_text()))
    assert documents[0] == documents[1]
    assert documents[0] == {
        "infrastructure_failed": False,
        "tests": [
            {
                "nodeid": "test_case.py::test_case",
                "outcome": "failed",
                "failed_phase": "call",
                "duration_ms": 1500.0,
                "call_duration_ms": 750.0,
            }
        ],
    }
