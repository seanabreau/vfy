"""Protect result comparisons in the pinned native project checker."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from validation.pytest_runner.check_native_project import (
    CoverageResult,
    JunitCase,
    ProjectResult,
    compare_cases,
    compare_coverage,
    compare_results,
    junit_cases,
    load_json,
)


def write_junit(path: Path, cases: list[JunitCase]) -> Path:
    root = ET.Element("testsuite", tests=str(len(cases)))
    for classname, name, outcomes in cases:
        testcase = ET.SubElement(root, "testcase", classname=classname, name=name)
        for tag, message in outcomes:
            ET.SubElement(testcase, tag, message=message)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


@pytest.mark.parametrize(
    "candidate_cases",
    [
        pytest.param(
            [
                ("tests.test_example", "test_first", (("failure", "expected 42"),)),
            ],
            id="missing-case",
        ),
        pytest.param(
            [
                ("tests.test_example", "test_second", (("skipped", "requires terminal"),)),
                ("tests.test_example", "test_first", (("failure", "expected 42"),)),
            ],
            id="reordered-cases",
        ),
        pytest.param(
            [
                ("tests.test_example", "test_first", (("failure", "expected 42"),)),
                ("tests.test_example", "test_second", ()),
            ],
            id="skip-became-pass",
        ),
        pytest.param(
            [
                ("tests.test_example", "test_first", (("failure", "expected 43"),)),
                ("tests.test_example", "test_second", (("skipped", "requires terminal"),)),
            ],
            id="changed-failure-diagnostic",
        ),
        pytest.param(
            [
                ("tests.test_example", "test_first", (("error", "expected 42"),)),
                ("tests.test_example", "test_second", (("skipped", "requires terminal"),)),
            ],
            id="failure-became-error",
        ),
    ],
)
def test_project_comparison_rejects_missing_reordered_and_changed_junit_outcomes(
    tmp_path: Path, candidate_cases: list[JunitCase]
) -> None:
    reference_cases: list[JunitCase] = [
        ("tests.test_example", "test_first", (("failure", "expected 42"),)),
        ("tests.test_example", "test_second", (("skipped", "requires terminal"),)),
    ]
    reference = junit_cases(write_junit(tmp_path / "reference.xml", reference_cases))
    candidate = junit_cases(write_junit(tmp_path / "candidate.xml", candidate_cases))

    assert reference == reference_cases
    with pytest.raises(ValueError, match="differs in cases"):
        compare_cases(reference, candidate)


def test_project_comparison_accepts_equal_junit_outcomes(tmp_path: Path) -> None:
    cases: list[JunitCase] = [
        ("tests.test_example", "test_first", (("failure", "expected 42"),)),
        ("tests.test_example", "test_second", (("skipped", "requires terminal"),)),
    ]
    reference = junit_cases(write_junit(tmp_path / "reference.xml", cases))
    candidate = junit_cases(write_junit(tmp_path / "candidate.xml", cases))

    compare_cases(reference, candidate)


def test_project_comparison_normalizes_checkout_paths_but_detects_filename_changes(
    tmp_path: Path,
) -> None:
    reference_root = tmp_path / "reference-project"
    candidate_root = tmp_path / "candidate-project"
    reference_root.mkdir()
    candidate_root.mkdir()
    case: JunitCase = ("tests.test_example", "test_first", ())
    totals = {"covered_lines": 1}

    def report(root: Path, path: str) -> ProjectResult:
        report_path = tmp_path / f"{root.name}.json"
        report_path.write_text(
            json.dumps(
                {
                    "files": {str(root / path): {"executed_lines": [1]}},
                    "totals": totals,
                }
            ),
            encoding="utf-8",
        )
        return {
            "cases": [case],
            "exit_status": 0,
            "files": load_json(report_path, root),
            "totals": totals,
        }

    compare_coverage(
        report(reference_root, "rich/module.py"), report(candidate_root, "rich/module.py")
    )
    with pytest.raises(ValueError, match="differs in files"):
        compare_coverage(
            report(reference_root, "rich/module.py"),
            report(candidate_root, "rich/other.py"),
        )


@pytest.mark.parametrize(
    ("field", "expected_message"),
    [
        pytest.param("files", "coverage report differs in files", id="file-data"),
        pytest.param("totals", "coverage report differs in totals", id="totals"),
    ],
)
def test_coverage_comparison_rejects_same_run_report_corruption(
    field: str, expected_message: str
) -> None:
    run_result: CoverageResult = {
        "files": {"rich/module.py": {"executed_lines": [1, 2]}},
        "totals": {"covered_lines": 2, "num_statements": 3},
    }
    reported: CoverageResult = {
        "files": {"rich/module.py": {"executed_lines": [1, 3]}},
        "totals": {"covered_lines": 2, "num_statements": 3},
    }
    if field == "totals":
        reported["files"] = run_result["files"]
        reported["totals"] = {"covered_lines": 1, "num_statements": 3}

    with pytest.raises(ValueError, match=expected_message):
        compare_coverage(run_result, reported)


def test_project_comparison_rejects_different_exit_status() -> None:
    reference: ProjectResult = {"cases": [], "files": {}, "totals": {}, "exit_status": 1}
    candidate: ProjectResult = {"cases": [], "files": {}, "totals": {}, "exit_status": 0}
    with pytest.raises(ValueError, match="exit status differs"):
        compare_results(reference, candidate)


def test_project_comparison_rejects_empty_reference_inventory() -> None:
    empty: ProjectResult = {"cases": [], "files": {}, "totals": {}, "exit_status": 0}
    with pytest.raises(ValueError, match="no test cases"):
        compare_results(empty, empty)
