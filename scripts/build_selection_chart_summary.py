"""Build a selection chart from parity-gated real-project edit-rerun records."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.render_benchmark_charts import parse_project_charts

ROUTE = "Affected-test rerun · serial · distinct report outputs"
ROOT = Path(__file__).resolve().parents[1]
PARITY = (
    "exact selected outcomes and required targets; vfy JSON report and aggregate "
    "schema-7 coverage equal full pytest-cov; per-test context differences disclosed"
)


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return cast(dict[str, object], value)


def _positive(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be positive")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def _nodes(value: object, field: str) -> set[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{field} must contain test IDs")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} has duplicate test IDs")
    return set(value)


def _check_coverage_stage(value: object, project: str, phase: str) -> bool:
    stage = _object(value, f"{project} {phase} coverage")
    per_context = stage.get("per_context_schema7")
    different_pairs = stage.get("different_context_file_pairs")
    if (
        stage.get("aggregate_schema7") != "exact"
        or per_context not in {"exact", "differs"}
        or not isinstance(different_pairs, int)
        or different_pairs < 0
    ):
        raise ValueError(f"{project} {phase} aggregate coverage or context diagnostics missing")
    for key in ("vfy_contexts", "oracle_contexts"):
        count = stage.get(key)
        if not isinstance(count, int) or count <= 0:
            raise ValueError(f"{project} {phase} context counts missing")
    for key in ("vfy_only_contexts", "oracle_only_contexts", "first_differences"):
        if not isinstance(stage.get(key), list):
            raise ValueError(f"{project} {phase} context diagnostics missing")
    for key in ("vfy_data", "oracle_data"):
        if not isinstance(stage.get(key), str) or not stage[key]:
            raise ValueError(f"{project} {phase} raw coverage source missing")
    mismatch = (
        bool(different_pairs)
        or stage["vfy_contexts"] != stage["oracle_contexts"]
        or bool(stage["vfy_only_contexts"])
        or bool(stage["oracle_only_contexts"])
    )
    if (per_context == "differs") != mismatch:
        raise ValueError(f"{project} {phase} context status contradicts diagnostics")
    return mismatch


def _check_sample(
    value: object, project: str, *, timed: bool = True
) -> tuple[float, float, int, int, int, bool]:
    sample = _object(value, f"{project} sample")
    if (
        sample.get("selection_relation") not in {"exact", "vfy_conservative"}
        or sample.get("parity") != PARITY
    ):
        raise ValueError(f"{project} selection or coverage parity did not pass")
    context_statuses = [
        _check_coverage_stage(sample.get(f"{phase}_coverage"), project, phase)
        for phase in ("baseline", "affected")
    ]
    contexts_differ = any(context_statuses)
    vfy = _nodes(sample.get("vfy_tests"), f"{project} vfy tests")
    testmon = _nodes(sample.get("testmon_tests"), f"{project} testmon tests")
    full = _nodes(sample.get("full_tests"), f"{project} full suite")
    if not testmon or not vfy or not full or not testmon <= vfy <= full:
        raise ValueError(f"{project} selected IDs do not match full-suite inclusion")
    if (sample["selection_relation"] == "exact") != (vfy == testmon):
        raise ValueError(f"{project} declared selection relation is wrong")
    elapsed = _object(sample.get("elapsed_ms"), f"{project} elapsed_ms")
    if not timed:
        if elapsed.get("testmon") != 0 or elapsed.get("vfy") != 0:
            raise ValueError(f"{project} validation gate must be untimed")
        return 0.0, 0.0, len(full), len(testmon), len(vfy), contexts_differ
    return (
        _positive(elapsed.get("testmon"), f"{project} testmon elapsed"),
        _positive(elapsed.get("vfy"), f"{project} vfy elapsed"),
        len(full),
        len(testmon),
        len(vfy),
        contexts_differ,
    )


def _record_row(record: object, source: str) -> tuple[dict[str, object], str]:
    result = _object(record, source)
    project_name = result.get("project")
    project = project_name.lower() if isinstance(project_name, str) else None
    if project not in {"platformdirs", "rich"} or result.get("benchmark") != "real-selection":
        raise ValueError(f"{source} is not a supported real-selection project")
    assert isinstance(project, str)
    sampling = _object(result.get("sampling"), f"{project} sampling")
    mode = sampling.get("mode")
    warmups = sampling.get("warmups")
    runs = sampling.get("measured_runs")
    if (
        mode not in {"publication", "diagnostic"}
        or not isinstance(warmups, int)
        or (
            not isinstance(runs, int)
            or runs < (15 if mode == "publication" else 2)
            or warmups < (3 if mode == "publication" else 0)
        )
    ):
        raise ValueError(f"{project} has insufficient measured samples or warmups")
    provenance = _object(result.get("provenance"), f"{project} provenance")
    source_state = _object(provenance.get("source"), f"{project} controller")
    build = _object(provenance.get("vfy_build"), f"{project} release wheel")
    validation = _object(provenance.get("validation"), f"{project} validation")
    wheel_sha = build.get("wheel_sha256")
    if (
        not source_state.get("revision")
        or build.get("native_profile") != "release"
        or not isinstance(wheel_sha, str)
        or len(wheel_sha) != 64
        or validation.get("status") != "passed"
        or provenance.get("workers") != 1
        or provenance.get("sampling") != sampling
    ):
        raise ValueError(f"{project} lacks pinned one-worker release provenance")
    workload = _object(result.get("workload"), f"{project} workload")
    commands = _object(result.get("commands"), f"{project} commands")
    command = commands.get("testmon")
    if (
        workload.get("workers") != 1
        or workload.get("capture")
        != "no on pytest-native vfy, pytest-testmon, and full pytest-cov oracle"
        or workload.get("vfy_operation")
        != "pytest-native affected selection with coverage JSON and JUnit"
        or workload.get("testmon_operation") != "affected selection with pytest-testmon"
        or not isinstance(command, list)
        or "--testmon" not in command
        or "--capture=no" not in command
        or not result.get("testmon_version")
    ):
        raise ValueError(f"{project} comparison route or report outputs changed")
    edit = _object(result.get("edit"), f"{project} edit")
    if not edit.get("path") or edit.get("before") == edit.get("after"):
        raise ValueError(f"{project} requires one recorded source edit")
    validation = _check_sample(result.get("validation"), project, timed=False)
    expected = validation[2:5]
    contexts_differ = validation[5]
    samples = result.get("samples")
    if not isinstance(samples, list):
        raise ValueError(f"{project} has no samples")
    measured = [
        sample
        for sample in samples
        if isinstance(sample, dict) and sample.get("kind") == "measured"
    ]
    if len(measured) != runs or len(samples) != runs + warmups:
        raise ValueError(f"{project} measured sample count differs from methodology")
    reference_times: list[float] = []
    candidate_times: list[float] = []
    for sample in measured:
        reference, candidate, full, testmon, vfy, sample_contexts_differ = _check_sample(
            sample, project
        )
        if (full, testmon, vfy) != expected:
            raise ValueError(f"{project} selected test counts drift between samples")
        reference_times.append(reference)
        candidate_times.append(candidate)
        contexts_differ |= sample_contexts_differ
    reference_median = statistics.median(reference_times)
    candidate_median = statistics.median(candidate_times)
    for key, value in (("testmon", reference_median), ("vfy", candidate_median)):
        summary = _object(result.get(key), f"{project} {key} summary")
        if not math.isclose(
            _positive(summary.get("median_ms"), f"{project} median"), value, rel_tol=1e-9
        ):
            raise ValueError(f"{project} retained median differs from raw samples")
    speedup = _positive(result.get("speedup_vs_testmon"), f"{project} speedup")
    if not math.isclose(speedup, reference_median / candidate_median, rel_tol=1e-9):
        raise ValueError(f"{project} retained speedup differs from raw medians")
    full, testmon, vfy = expected
    return (
        {
            "project": "Platformdirs" if project == "platformdirs" else "Rich",
            "scope": (
                f"{full:,} full tests; edited {edit['path']}; "
                f"{testmon} testmon and {vfy} vfy selected"
            ),
            "display_scope": "Edited full suite",
            "route": ROUTE,
            "reference_median_ms": reference_median,
            "candidate_median_ms": candidate_median,
            "reference_samples": runs,
            "candidate_samples": runs,
            "reference_samples_ms": reference_times,
            "candidate_samples_ms": candidate_times,
            "vfy_source_revision": build.get("source_revision"),
            "wheel_sha256": wheel_sha,
            "controller_revision": source_state.get("revision"),
            "upstream_revision": result.get("upstream_revision"),
            "edit": edit,
            "runtime_versions": result.get("measured_runtime_versions", {}),
            "warmups": warmups,
            "status": mode,
            "parity": "passed",
            "source": source,
            "full_cases": full,
            "reference_cases": testmon,
            "candidate_cases": vfy,
            "context_status": "differs" if contexts_differ else "exact",
        },
        wheel_sha,
    )


def build_summary(records: list[tuple[object, str]]) -> dict[str, object]:
    if len(records) < 2:
        raise ValueError("selection chart requires at least two real projects")
    extracted = [_record_row(record, source) for record, source in records]
    wheel_hashes = {wheel for _, wheel in extracted}
    if len(wheel_hashes) != 1:
        raise ValueError("selection projects use different vfy wheels")
    rows = [row for row, _ in extracted]
    if len({row["project"] for row in rows}) != len(rows):
        raise ValueError("selection chart repeats a project")
    summary = {
        "schema": 1,
        "charts": [
            {
                "kind": "selection",
                "title": "Affected-test selection",
                "route": ROUTE,
                "context": f"{len(rows)} edited suites · CPython 3.12 · Linux",
                "reference": "pytest-testmon",
                "candidate": "vfy",
                "rows": rows,
            }
        ],
    }
    parse_project_charts(summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    records = []
    for path in args.records:
        source = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
        records.append((json.loads(path.read_text(encoding="utf-8")), source))
    summary = build_summary(records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
