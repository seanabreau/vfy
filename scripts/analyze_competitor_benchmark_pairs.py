"""Retain paired median intervals for rounds in a compact competitor index.

Run after ``summarize_competitor_benchmarks.py``. Each index row must contain
matching rounds of one experiment; samples from separate cohorts cannot be
paired by ordinal position. Intervals describe sample variability on the
recorded machine and cache workload, not variation across hosts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.benchmark_harness import paired_uncertainty

from scripts.summarize_competitor_benchmarks import Record, object_record, positive, records


def _samples(route: Record, field: str, count: int, divisor: int = 1) -> list[float]:
    values = route.get(field)
    if not isinstance(values, list) or len(values) != count:
        raise ValueError(f"{field} does not contain every paired measured round")
    return [positive(value, field) / divisor for value in values]


def _metric(candidate: list[float], reference: list[float], unit: str) -> Record:
    estimate = paired_uncertainty(candidate, reference)
    uncertainty = {
        key.removesuffix("_ms") + f"_{unit}" if key.endswith("_ms") else key: value
        for key, value in estimate.items()
    }
    low, high = estimate["ci95_low_ms"], estimate["ci95_high_ms"]
    direction = "inconclusive"
    if low is not None and high is not None:
        if high < 0:
            direction = "candidate lower"
        elif low > 0:
            direction = "candidate higher"
    return {
        "unit": unit,
        "candidate": candidate,
        "reference": reference,
        "uncertainty": uncertainty,
        "interval_direction": direction,
    }


def analyze(index: Record) -> Record:
    if index.get("schema") != "vfy.competitor-benchmark-index.v1":
        raise ValueError("expected a compact competitor benchmark index")
    comparisons = []
    for row_index, row in enumerate(records(index.get("results"), "index results")):
        if row.get("status") != "measured":
            raise ValueError("index results must contain only measured experiments")
        sampling = object_record(row.get("sampling"), "sampling")
        count = sampling.get("measured_runs")
        if type(count) is not int or count < 2:
            raise ValueError("paired intervals require at least two measured rounds")
        routes = object_record(row.get("routes"), "routes")
        for comparison in records(row.get("comparisons"), "comparisons"):
            candidate_name, reference_name = comparison["candidate"], comparison["reference"]
            candidate = object_record(routes[candidate_name], "candidate route")
            reference = object_record(routes[reference_name], "reference route")
            unequal_cache = row.get("benchmark") == "mutation" and row.get("workload") in {
                "warm",
                "cached",
            }
            helper_boundary = row.get("benchmark") == "real-mutation"
            record = {
                "row_index": row_index,
                "row_sha256": hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(),
                "benchmark": row.get("benchmark"),
                "workload": row.get("workload"),
                "source": row.get("source"),
                "candidate": candidate_name,
                "reference": reference_name,
                "sampling": sampling,
                "unequal_cache_workloads": unequal_cache,
                "semantic_limits": row.get("semantic_limits"),
                "wall_boundary": (
                    "complete workflow including selection/catalog preparation and parsing"
                    if helper_boundary
                    else "public child command"
                ),
                "memory_boundary": (
                    "sampled public command and live descendants; "
                    "excludes controller, collector, and helper preparation"
                ),
                "wall": _metric(
                    _samples(candidate, "wall_ms", count),
                    _samples(reference, "wall_ms", count),
                    "ms",
                ),
                "tree_pss": _metric(
                    _samples(candidate, "peak_tree_pss_bytes", count, 1024**2),
                    _samples(reference, "peak_tree_pss_bytes", count, 1024**2),
                    "mib",
                ),
            }
            if helper_boundary:
                record["public_child_command_wall"] = _metric(
                    _samples(candidate, "command_wall_ms", count),
                    _samples(reference, "command_wall_ms", count),
                    "ms",
                )
            comparisons.append(record)
    return {
        "schema": "vfy.competitor-paired-analysis.v1",
        "method": (
            "paired percentile bootstrap of difference of medians; matching rounds "
            "resampled together; 10000 resamples; seed 0; inclusive 2.5/97.5 percentiles"
        ),
        "interpretation": (
            "candidate minus reference; intervals exclude zero only when their direction "
            "is candidate lower or candidate higher; unequal caches do not establish "
            "equivalent-cache superiority"
        ),
        "comparisons": comparisons,
        "blocked": index.get("blocked", []),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    analysis = analyze(object_record(json.loads(args.index.read_text()), "index"))
    analysis["index"] = {
        "path": str(args.index),
        "sha256": hashlib.sha256(args.index.read_bytes()).hexdigest(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(analysis, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
