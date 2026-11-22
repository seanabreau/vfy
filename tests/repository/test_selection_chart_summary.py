"""Selection charts must derive medians and selected counts from validated reruns."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from scripts.build_selection_chart_summary import build_summary
from scripts.render_benchmark_charts import parse_project_charts, render_project_chart


def _sample(reference_ms: float, candidate_ms: float) -> dict[str, object]:
    coverage = {
        "aggregate_schema7": "exact",
        "per_context_schema7": "differs",
        "vfy_contexts": 3,
        "oracle_contexts": 3,
        "vfy_only_contexts": [],
        "oracle_only_contexts": [],
        "different_context_file_pairs": 1,
        "first_differences": [{"context": "test-a", "file": "module.py"}],
        "vfy_data": "fixture/vfy.coverage",
        "oracle_data": "fixture/oracle.coverage",
    }
    return {
        "kind": "measured",
        "selection_relation": "vfy_conservative",
        "parity": (
            "exact selected outcomes and required targets; vfy JSON report and aggregate "
            "schema-7 coverage equal full pytest-cov; per-test context differences disclosed"
        ),
        "baseline_coverage": copy.deepcopy(coverage),
        "affected_coverage": copy.deepcopy(coverage),
        "elapsed_ms": {"testmon": reference_ms, "vfy": candidate_ms},
        "vfy_tests": ["case-a", "case-b"],
        "testmon_tests": ["case-a"],
        "full_tests": ["case-a", "case-b", "case-c"],
    }


def _record(project: str) -> dict[str, Any]:
    first = _sample(1000, 500)
    second = _sample(1200, 600)
    return {
        "benchmark": "real-selection",
        "project": project.title(),
        "sampling": {"mode": "diagnostic", "warmups": 0, "measured_runs": 2},
        "provenance": {
            "source": {"revision": "abc", "dirty": False},
            "vfy_build": {"native_profile": "release", "wheel_sha256": "a" * 64},
            "validation": {"status": "passed"},
            "workers": 1,
            "sampling": {"mode": "diagnostic", "warmups": 0, "measured_runs": 2},
        },
        "workload": {
            "workers": 1,
            "capture": "no on pytest-native vfy, pytest-testmon, and full pytest-cov oracle",
            "vfy_operation": "pytest-native affected selection with coverage JSON and JUnit",
            "testmon_operation": "affected selection with pytest-testmon",
        },
        "commands": {"testmon": ["python", "-m", "pytest", "--capture=no", "--testmon"]},
        "testmon_version": "2.2.0",
        "edit": {"path": "src/module.py", "before": "x", "after": "y"},
        "validation": _sample(0, 0),
        "samples": [first, second],
        "vfy": {"median_ms": 550},
        "testmon": {"median_ms": 1100},
        "speedup_vs_testmon": 2,
    }


def test_selection_summary_derives_times_counts_and_distinct_report_caption() -> None:
    summary = build_summary(
        [
            (_record("platformdirs"), "fixture/platformdirs.json"),
            (_record("rich"), "fixture/rich.json"),
        ]
    )
    charts = summary["charts"]
    assert isinstance(charts, list)
    rows = charts[0]["rows"]
    assert rows[0]["reference_samples_ms"] == [1000, 1200]
    assert rows[0]["candidate_samples_ms"] == [500, 600]
    chart = parse_project_charts(summary)[0]
    assert len(chart.rows) == 2
    assert chart.rows[0].reference_median_ms == 1100
    assert chart.rows[0].candidate_median_ms == 550
    assert chart.rows[0].full_cases == 3
    assert chart.rows[0].reference_cases == 1
    assert chart.rows[0].candidate_cases == 2
    assert chart.rows[0].context_status == "differs"
    svg = render_project_chart(chart)
    assert "Selected 2 vfy / 1 testmon of 3 tests" in svg
    assert "vfy JSON + aggregate coverage match oracle; contexts may differ" in svg
    assert "per-test context status: differs" in svg


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("drift", "selected test counts drift"),
        ("wheel", "different vfy wheels"),
        ("parity", "selection or coverage parity"),
        ("samples", "measured sample count"),
        ("median", "retained median differs"),
        ("reports", "comparison route or report outputs changed"),
        ("aggregate", "aggregate coverage or context diagnostics missing"),
        ("context", "context status contradicts diagnostics"),
    ],
)
def test_selection_summary_rejects_unqualified_records(change: str, message: str) -> None:
    first = _record("platformdirs")
    second = _record("rich")
    if change == "drift":
        sample = second["samples"]
        assert isinstance(sample, list)
        sample[1]["vfy_tests"] = ["case-a", "case-b", "case-c"]
    elif change == "wheel":
        provenance = second["provenance"]
        assert isinstance(provenance, dict)
        provenance["vfy_build"]["wheel_sha256"] = "b" * 64
    elif change == "parity":
        second["validation"]["parity"] = "failed"
    elif change == "samples":
        second["samples"] = [copy.deepcopy(second["samples"][0])]
    elif change == "median":
        second["vfy"]["median_ms"] = 1
    elif change == "reports":
        second["workload"]["vfy_operation"] = "native selection without coverage"
    elif change == "aggregate":
        second["validation"]["affected_coverage"]["aggregate_schema7"] = "differs"
    elif change == "context":
        second["validation"]["affected_coverage"]["per_context_schema7"] = "exact"
    with pytest.raises(ValueError, match=message):
        build_summary([(first, "fixture/a.json"), (second, "fixture/b.json")])
