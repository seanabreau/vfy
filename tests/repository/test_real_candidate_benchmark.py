"""Reject history corruption without hiding it in timing normalization."""

from __future__ import annotations

import copy
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from validation.pytest_runner.benchmark_real_candidates import (
    case_outcomes,
    cli_selectors,
    normalized_history,
    ordered_roles,
    require_controller_python,
)
from validation.pytest_runner.check_native_adoption_matrix import PROJECTS_BY_KEY


def test_report_oracle_rejects_a_different_target_python_minor() -> None:
    major, minor = sys.version_info[:2]
    require_controller_python({"python": f"{major}.{minor}.99"})
    with pytest.raises(ValueError, match="source analysis requires controller Python"):
        require_controller_python({"python": f"{major}.{minor + 1}.0"})
    with pytest.raises(ValueError, match="metadata must include"):
        require_controller_python({})


def test_candidate_roles_require_baseline_and_keep_arbitrary_candidate_order() -> None:
    runtimes = {"collector": {}, "baseline": {}, "literal": {}}
    assert ordered_roles(runtimes) == ("baseline", "collector", "literal")
    del runtimes["collector"]
    assert ordered_roles(runtimes) == ("baseline", "literal")
    del runtimes["baseline"]
    with pytest.raises(ValueError, match="require a baseline"):
        ordered_roles(runtimes)
    runtimes["baseline"] = {}
    runtimes["../candidate"] = {}
    with pytest.raises(ValueError, match="path components"):
        ordered_roles(runtimes)


def history_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    seed: dict[str, Any] = {
        "version": 1,
        "extension": {"preserve": "root"},
        "tests": {
            "test.py::selected": {
                "duration_ms": 1,
                "extension": [3, 4],
                "observations": [
                    {"run_id": "old", "attempt": "primary", "outcome": "passed", "duration_ms": 2}
                ],
            },
            "test.py::other": {"duration_ms": 8, "observations": []},
        },
    }
    actual = copy.deepcopy(seed)
    actual["tests"]["test.py::selected"]["duration_ms"] = 9
    actual["tests"]["test.py::selected"]["observations"].append(
        {
            "run_id": "a" * 32,
            "attempt": "primary",
            "outcome": "passed",
            "duration_ms": 10,
        }
    )
    return seed, actual


def test_history_normalizes_only_current_selected_observation() -> None:
    seed, actual = history_pair()
    proof = normalized_history(actual, seed, {"test.py::selected": "passed"})
    assert proof["tests"]["test.py::other"] == seed["tests"]["test.py::other"]
    assert (
        proof["tests"]["test.py::selected"]["observations"][0]
        == seed["tests"]["test.py::selected"]["observations"][0]
    )
    assert actual["tests"]["test.py::selected"]["duration_ms"] == 9


@pytest.mark.parametrize(
    "damage", ["old-duration", "untouched", "unknown", "missing", "outcome", "run-id"]
)
def test_history_rejects_changes_outside_new_durations_and_run_id(damage: str) -> None:
    seed, actual = history_pair()
    selected = actual["tests"]["test.py::selected"]
    if damage == "old-duration":
        selected["observations"][0]["duration_ms"] = 7
    elif damage == "untouched":
        actual["tests"]["test.py::other"]["duration_ms"] = 7
    elif damage == "unknown":
        selected["extension"] = []
    elif damage == "missing":
        del actual["tests"]["test.py::selected"]
    elif damage == "outcome":
        selected["observations"][-1]["outcome"] = "skipped"
    else:
        selected["observations"][-1]["run_id"] = "invalid"
    with pytest.raises(ValueError, match="history"):
        normalized_history(actual, seed, {"test.py::selected": "passed"})


def test_setup_skip_preserves_absent_call_duration() -> None:
    seed, actual = history_pair()
    del seed["tests"]["test.py::selected"]["duration_ms"]
    del actual["tests"]["test.py::selected"]["duration_ms"]
    actual["tests"]["test.py::selected"]["observations"][-1]["outcome"] = "skipped"
    proof = normalized_history(actual, seed, {"test.py::selected": "skipped"}, set())
    assert "duration_ms" not in proof["tests"]["test.py::selected"]
    actual["tests"]["test.py::selected"]["duration_ms"] = 1
    with pytest.raises(ValueError, match="history changed"):
        normalized_history(actual, seed, {"test.py::selected": "skipped"}, set())


def test_history_outcomes_come_from_execution_reports() -> None:
    cases = [
        {"nodeid": "passed", "reports": [{"phase": "call", "outcome": "passed"}]},
        {"nodeid": "skip", "reports": [{"phase": "setup", "outcome": "skipped"}]},
        {"nodeid": "xfail", "reports": [{"phase": "call", "outcome": "xfailed"}]},
    ]
    assert case_outcomes(cases) == {"passed": "passed", "skip": "skipped", "xfail": "skipped"}
    cases[0]["reports"][0]["outcome"] = "error"
    with pytest.raises(ValueError, match="failed phase"):
        case_outcomes(cases)


def test_cli_selectors_preserve_node_suffix_and_upstream_root(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_align.py").write_text("")
    (tmp_path / "test_direct.py").write_text("")
    nodeids = ("test_align.py::test_bottom[case::value]", "test_direct.py::test_one")
    assert cli_selectors(PROJECTS_BY_KEY["rich"], tmp_path, nodeids) == (
        "tests/test_align.py::test_bottom[case::value]",
        "test_direct.py::test_one",
    )
    assert nodeids[0] == "test_align.py::test_bottom[case::value]"


def test_cli_selectors_refuse_missing_or_ambiguous_source(tmp_path: Path) -> None:
    project = replace(PROJECTS_BY_KEY["rich"], tests=("first", "second"))
    with pytest.raises(ValueError, match="no unique CLI source"):
        cli_selectors(project, tmp_path, ("test_missing.py::test_one",))
    for name in ("first", "second"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "test_same.py").write_text("")
    with pytest.raises(ValueError, match="no unique CLI source"):
        cli_selectors(project, tmp_path, ("test_same.py::test_one",))
