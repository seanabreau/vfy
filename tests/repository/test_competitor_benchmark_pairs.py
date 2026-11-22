"""Paired analysis preserves losses, units, and unequal-cache boundaries."""

from __future__ import annotations

import pytest
from scripts.analyze_competitor_benchmark_pairs import analyze
from scripts.summarize_competitor_benchmarks import Record


def index(*, workload: str = "cold") -> Record:
    return {
        "schema": "vfy.competitor-benchmark-index.v1",
        "results": [
            {
                "benchmark": "mutation",
                "workload": workload,
                "status": "measured",
                "sampling": {"measured_runs": 2},
                "routes": {
                    "vfy": {"wall_ms": [20, 20], "peak_tree_pss_bytes": [2**21, 2**21]},
                    "mutmut": {"wall_ms": [10, 10], "peak_tree_pss_bytes": [2**20, 2**20]},
                },
                "comparisons": [{"candidate": "vfy", "reference": "mutmut"}],
            }
        ],
        "blocked": [{"status": "blocked", "details": "parity failed"}],
    }


def test_paired_analysis_preserves_losses_and_memory_units() -> None:
    result = analyze(index())
    row = result["comparisons"][0]
    assert row["wall"]["uncertainty"]["median_difference_ms"] == 10
    assert row["wall"]["interval_direction"] == "candidate higher"
    assert row["tree_pss"]["uncertainty"]["ci95_low_mib"] == 1
    assert row["tree_pss"]["interval_direction"] == "candidate higher"
    assert row["unequal_cache_workloads"] is False
    assert result["blocked"] == [{"status": "blocked", "details": "parity failed"}]


@pytest.mark.parametrize("workload", ["warm", "cached"])
def test_paired_analysis_marks_unequal_generated_cache_workloads(workload: str) -> None:
    assert analyze(index(workload=workload))["comparisons"][0]["unequal_cache_workloads"] is True


def test_paired_analysis_rejects_incomplete_pair_vectors() -> None:
    data = index()
    data["results"][0]["routes"]["vfy"]["wall_ms"] = [20]
    with pytest.raises(ValueError, match="every paired measured round"):
        analyze(data)


def test_real_mutation_preserves_both_workflow_and_command_boundaries() -> None:
    data = index()
    row = data["results"][0]
    row["benchmark"] = "real-mutation"
    row["routes"]["vfy"]["command_wall_ms"] = [5, 5]
    row["routes"]["mutmut"]["command_wall_ms"] = [8, 8]
    result = analyze(data)["comparisons"][0]
    assert result["wall"]["interval_direction"] == "candidate higher"
    assert result["public_child_command_wall"]["interval_direction"] == "candidate lower"
    assert "workflow" in result["wall_boundary"]
