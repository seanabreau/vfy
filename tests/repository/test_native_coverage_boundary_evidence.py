"""Keep the coverage differential's result checker sensitive to lost evidence."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

import pytest
from coverage import CoverageData
from validation.pytest_runner.check_vfy_coverage_boundary import (
    assert_run_evidence,
    event_test_source,
    expected_phase_records,
    normalize_context,
    normalize_nodeid,
    run_unsupported_case,
)

from tests.support import VfyProject

EXPECTED = {
    "test_contract.py::test_first": "passed",
    "test_contract.py::test_second": "failed",
}
RUN_ID = "latest-run"


def test_xdist_stepwise_rejection_preserves_prior_coverage_evidence(tmp_path: Path) -> None:
    seed = tmp_path / "previous-coverage"
    data = CoverageData(basename=str(seed))
    try:
        data.add_arcs({str(tmp_path / "previous.py"): {(-1, 1), (1, -1)}})
        data.write()
    finally:
        data.close()

    result = run_unsupported_case(
        tmp_path,
        Path(sys.executable),
        seed.read_bytes(),
        "xdist-stepwise",
        event_test_source("test_xdist_stepwise_rejected"),
        extra_arguments=("--numprocesses", "2", "--stepwise"),
        expected_diagnostic="native pytest does not implement stepwise execution with xdist",
        cache_seed=b"previous cache value\n",
    )

    assert result["exit_status"] == pytest.ExitCode.USAGE_ERROR
    assert result["seeded_cache_value_preserved"] is True
    assert "does not implement stepwise execution with xdist" in str(result["rejection"])


def test_project_specific_test_contexts_are_normalized_across_roots(tmp_path: Path) -> None:
    project = tmp_path / "candidate-project"
    project.mkdir()
    nodeid = f"{project}/test_contract.py::test_value[param]"

    assert normalize_nodeid(nodeid, project) == "test_contract.py::test_value[param]"
    assert normalize_context(f"{nodeid}|setup", project) == (
        "test_contract.py::test_value[param]|setup"
    )
    assert normalize_context("user-defined-context", project) == "user-defined-context"


def _project(tmp_path: Path) -> VfyProject:
    project = VfyProject(tmp_path / "project")
    tests = {
        nodeid: {
            "observations": [
                {
                    "run_id": RUN_ID,
                    "attempt": "primary",
                    "outcome": outcome,
                }
            ]
        }
        for nodeid, outcome in EXPECTED.items()
    }
    project.write(".vfy/test-history.json", json.dumps({"tests": tests}))
    return project


def _phase_rows(
    outcomes: dict[str, str] = EXPECTED,
) -> list[dict[str, str]]:
    return [
        {
            **phase,
            "stdout": "",
            "stderr": "",
        }
        for phase in expected_phase_records(outcomes)
    ]


def _native_record(path: Path, phases: list[dict[str, str]]) -> Path:
    by_node: dict[str, list[dict[str, str]]] = {}
    for phase in phases:
        by_node.setdefault(phase["nodeid"], []).append(
            {key: value for key, value in phase.items() if key != "nodeid"}
        )
    cases = [{"nodeid": nodeid, "phases": by_node[nodeid]} for nodeid in EXPECTED]
    path.write_text(
        json.dumps({"engine": "pytest-runner-native-v1", "cases": cases}),
        encoding="utf-8",
    )
    return path


def _stock_phase_log(path: Path, phases: list[dict[str, str]]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in phases), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        ("missing", "identities"),
        ("duplicate", "identities"),
        ("reordered", "identities"),
        ("extra", "identities"),
    ],
)
def test_candidate_cases_must_match_the_ordered_expected_run(
    tmp_path: Path,
    mutation: Literal["missing", "duplicate", "reordered", "extra"],
    diagnostic: str,
) -> None:
    project = _project(tmp_path)
    cases = list(EXPECTED)
    if mutation == "missing":
        cases.pop()
    elif mutation == "duplicate":
        cases[1] = cases[0]
    elif mutation == "reordered":
        cases.reverse()
    elif mutation == "extra":
        cases.append("test_contract.py::test_unexpected")
    record = {
        "engine": "pytest-runner-native-v1",
        "cases": [
            {
                "nodeid": nodeid,
                "phases": [
                    {
                        "phase": phase["phase"],
                        "outcome": phase["outcome"],
                        "stdout": "",
                        "stderr": "",
                    }
                    for phase in expected_phase_records(EXPECTED)
                    if phase["nodeid"] == (nodeid if nodeid in EXPECTED else next(iter(EXPECTED)))
                ],
            }
            for nodeid in cases
        ],
    }
    result = tmp_path / "native-result.json"
    result.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(RuntimeError, match=diagnostic):
        assert_run_evidence(project, result, EXPECTED, candidate=True)


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        ("missing", "missing, duplicated, or out of order"),
        ("duplicate", "missing, duplicated, or out of order"),
        ("reordered", "missing, duplicated, or out of order"),
        ("wrong_outcome", "wrong call outcome"),
    ],
)
def test_candidate_phases_must_match_order_and_stock_outcomes(
    tmp_path: Path,
    mutation: Literal["missing", "duplicate", "reordered", "wrong_outcome"],
    diagnostic: str,
) -> None:
    project = _project(tmp_path)
    phases = _phase_rows()
    if mutation == "missing":
        phases.pop(1)
    elif mutation == "duplicate":
        phases.insert(1, phases[0].copy())
    elif mutation == "reordered":
        phases[0], phases[1] = phases[1], phases[0]
    elif mutation == "wrong_outcome":
        phases[4]["outcome"] = "passed"
    result = _native_record(tmp_path / "native-result.json", phases)

    with pytest.raises(RuntimeError, match=diagnostic):
        assert_run_evidence(project, result, EXPECTED, candidate=True)


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        ("extra_history_test", "extra or missing tests"),
        ("duplicate_history_observation", "duplicate observations"),
    ],
)
def test_stock_history_must_account_for_each_node_once_per_run(
    tmp_path: Path,
    mutation: Literal["extra_history_test", "duplicate_history_observation"],
    diagnostic: str,
) -> None:
    project = _project(tmp_path)
    history_path = project.root / ".vfy/test-history.json"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    if mutation == "extra_history_test":
        history["tests"]["test_contract.py::test_unexpected"] = {
            "observations": [{"run_id": RUN_ID, "attempt": "primary", "outcome": "passed"}]
        }
    else:
        history["tests"][next(iter(EXPECTED))]["observations"].append(
            {"run_id": RUN_ID, "attempt": "primary", "outcome": "passed"}
        )
    history_path.write_text(json.dumps(history), encoding="utf-8")

    with pytest.raises(RuntimeError, match=diagnostic):
        assert_run_evidence(
            project,
            tmp_path / "no-native-result.json",
            EXPECTED,
            candidate=False,
            phase_log=_stock_phase_log(tmp_path / "stock.jsonl", _phase_rows()),
        )


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        ("missing", "missing, duplicated, or out of order"),
        ("duplicate", "missing, duplicated, or out of order"),
        ("reordered", "missing, duplicated, or out of order"),
        ("wrong_outcome", "wrong call outcome"),
    ],
)
def test_stock_phase_oracle_must_match_the_full_ordered_run(
    tmp_path: Path,
    mutation: Literal["missing", "duplicate", "reordered", "wrong_outcome"],
    diagnostic: str,
) -> None:
    project = _project(tmp_path)
    phases = _phase_rows()
    if mutation == "missing":
        phases.pop(1)
    elif mutation == "duplicate":
        phases.insert(1, phases[0].copy())
    elif mutation == "reordered":
        phases[0], phases[1] = phases[1], phases[0]
    elif mutation == "wrong_outcome":
        phases[4]["outcome"] = "passed"
    stock_phases = _stock_phase_log(tmp_path / "stock.jsonl", phases)

    with pytest.raises(RuntimeError, match=diagnostic):
        assert_run_evidence(
            project,
            tmp_path / "no-native-result.json",
            EXPECTED,
            candidate=False,
            phase_log=stock_phases,
        )
