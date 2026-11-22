"""Build a compact time/PSS index from completed competitor benchmark evidence."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_selection_chart_summary import _check_sample

type Record = dict[str, Any]


def object_record(value: object, field: str) -> Record:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return cast("Record", value)


def records(value: object, field: str) -> list[Record]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return [object_record(item, field) for item in value]


def positive(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field} must be finite and positive")
    return float(value)


def resource(value: object, *, status: int = 0) -> Record:
    record = object_record(value, "resource evidence")
    if (
        record.get("method") != "linux-process-tree-smaps-rollup"
        or type(record.get("child_exit_status")) is not int
        or record.get("child_exit_status") != status
        or not isinstance(record.get("child_command"), list)
        or not record["child_command"]
        or len(str(record.get("collector_sha256", ""))) != 64
        or any(
            character not in "0123456789abcdef"
            for character in str(record.get("collector_sha256", ""))
        )
        or record.get("sample_interval_ms") != 20
    ):
        raise ValueError("missing process-tree command, status, method, or collector evidence")
    for key in ("peak_tree_pss_bytes", "peak_tree_rss_bytes", "snapshot_count"):
        if type(record.get(key)) is not int or record[key] <= 0:
            raise ValueError(f"missing positive process-tree {key}")
    positive(record.get("child_wall_ms"), "resource child wall time")
    if record["peak_tree_pss_bytes"] > record["peak_tree_rss_bytes"]:
        raise ValueError("process-tree PSS exceeds RSS")
    return record


def sampling(value: object, *, mutation: bool = False) -> Record:
    raw = object_record(value, "sampling")
    mode, warmups = raw.get("mode"), raw.get("warmups")
    runs = raw.get("measured_runs", raw.get("runs"))
    if (
        mode not in {"diagnostic", "publication"}
        or type(warmups) is not int
        or warmups < 0
        or type(runs) is not int
        or runs < 1
    ):
        raise ValueError("invalid sampling mode or counts")
    if mode == "publication" and (warmups < 3 or runs < (30 if mutation else 15)):
        raise ValueError("publication sampling is incomplete")
    return {"mode": mode, "warmups": warmups, "measured_runs": runs}


def route(
    times: object, evidence: list[object], count: int, *, helper: bool = False, status: int = 0
) -> Record:
    if not isinstance(times, list) or len(times) != count or len(evidence) != count:
        raise ValueError("measured timing/resource sample count differs from sampling")
    wall = [positive(value, "wall sample") for value in times]
    retained = [resource(value, status=status) for value in evidence]
    child = [float(item["child_wall_ms"]) for item in retained]
    if any(
        actual + 0.01 < command if helper else not math.isclose(actual, command, rel_tol=1e-8)
        for actual, command in zip(wall, child, strict=True)
    ):
        raise ValueError("wall samples do not match the recorded command boundary")
    pss = [item["peak_tree_pss_bytes"] for item in retained]
    return {
        "wall_ms": wall,
        "command_wall_ms": child,
        "peak_tree_pss_bytes": pss,
        "median_wall_ms": statistics.median(wall),
        "median_peak_tree_pss_bytes": statistics.median(pss),
        "collector_sha256": sorted({item["collector_sha256"] for item in retained}),
        "max_sample_gap_ms": [item.get("max_sample_gap_ms") for item in retained],
        "first_command": retained[0]["child_command"],
        "command_sha256": [
            hashlib.sha256(json.dumps(item["child_command"]).encode()).hexdigest()
            for item in retained
        ],
    }


def comparison(routes: Record, candidate: str, reference: str) -> Record:
    left, right = routes[candidate], routes[reference]
    speed = right["median_wall_ms"] / left["median_wall_ms"]
    memory = right["median_peak_tree_pss_bytes"] / left["median_peak_tree_pss_bytes"]
    return {
        "candidate": candidate,
        "reference": reference,
        "reference_over_candidate_wall": speed,
        "reference_over_candidate_pss": memory,
        "candidate_faster": speed > 1,
        "candidate_lower_memory": memory > 1,
    }


def paired_row(
    benchmark: str,
    workload: object,
    method: Record,
    routes: Record,
    candidate: str,
    reference: str,
    limits: object,
) -> Record:
    return {
        "benchmark": benchmark,
        "workload": workload,
        "status": "measured",
        "sampling": method,
        "publication_sampling": method["mode"] == "publication",
        "routes": routes,
        "comparisons": [comparison(routes, candidate, reference)],
        "semantic_limits": limits,
    }


def indexed_samples(rows: list[Record], method: Record, field: str) -> list[Record]:
    measured = [row for row in rows if row.get(field) == "measured"]
    warmups = [row for row in rows if row.get(field) == "warmup"]
    if len(measured) != method["measured_runs"] or len(warmups) != method["warmups"]:
        raise ValueError("missing or unexpected measured/warmup samples")
    if len(rows) != len(measured) + len(warmups):
        raise ValueError("unexpected sample phase")
    for group in (measured, warmups):
        if (
            group
            and all("index" in row for row in group)
            and sorted(row["index"] for row in group) != list(range(len(group)))
        ):
            raise ValueError("duplicate or missing sample index")
    if (
        rows
        and all("round" in row for row in rows)
        and sorted(row["round"] for row in rows) != list(range(1, len(rows) + 1))
    ):
        raise ValueError("duplicate or missing sample round")
    return measured


def real_selection(data: Record) -> list[Record]:
    method = sampling(data.get("sampling"))
    rows = records(data.get("samples"), "selection samples")
    measured = indexed_samples(rows, method, "kind")
    _check_sample(data.get("validation"), str(data.get("project")), timed=False)
    for row in rows:
        _check_sample(row, str(data.get("project")))
    routes = {
        name: route(
            [row["elapsed_ms"][name] for row in measured],
            [
                object_record(row.get("resources"), "selection resources").get(name)
                for row in measured
            ],
            method["measured_runs"],
        )
        for name in ("vfy", "testmon")
    }
    return [
        paired_row(
            "real-selection",
            data.get("project"),
            method,
            routes,
            "vfy",
            "testmon",
            {
                "selection_relation": data["validation"]["selection_relation"],
                "baseline_coverage": data["validation"]["baseline_coverage"],
                "affected_coverage": data["validation"]["affected_coverage"],
                "edit": data.get("edit"),
            },
        )
    ]


def real_mutation(data: Record) -> list[Record]:
    scope = object_record(data.get("scope"), "mutation scope")
    workload = {
        "project": data.get("project"),
        "scope": scope.get("function"),
        "source": scope.get("source"),
        "test": scope.get("test"),
    }
    if any(not isinstance(value, str) or not value.strip() for value in workload.values()):
        raise ValueError("real mutation requires nonempty project, function, source, and test IDs")
    if object_record(data.get("validation"), "mutation validation").get("status") != "passed":
        raise ValueError("real mutation parity gate failed")
    parity = object_record(data.get("parity"), "mutation parity")
    if not parity.get("outcomes") or parity.get("real_call_failure_observed") is not True:
        raise ValueError("real mutation lacks exact outcomes or killer failure proof")
    method = sampling(data.get("methodology"), mutation=True)
    measured = indexed_samples(records(data.get("samples"), "mutation samples"), method, "phase")
    expected = object_record(parity["outcomes"], "mutation parity outcomes")
    expected_mutmut = object_record(
        object_record(data.get("mutmut_outcomes"), "mutmut parity summary").get("outcomes"),
        "mutmut parity outcomes",
    )
    for row in measured:
        report = object_record(row.get("vfy_report"), "vfy mutation report")
        statuses = {}
        for mutation in records(report.get("mutations"), "vfy mutation outcomes"):
            source = workload["source"]
            path = str(mutation.get("path", ""))
            if not (path == source or path.endswith(f"/{source}")):
                raise ValueError("vfy mutation source differs from the parity scope")
            key = "|".join(
                map(
                    str,
                    (
                        source,
                        mutation.get("start"),
                        mutation.get("end"),
                        mutation.get("original"),
                        mutation.get("replacement"),
                    ),
                )
            )
            status = mutation.get("status")
            if key in statuses or status not in {"protected", "survived", "unreached"}:
                raise ValueError("duplicate or infrastructure vfy mutation outcome")
            if status == "protected" and not mutation.get("killer"):
                raise ValueError("protected mutation lacks a killer")
            statuses[key] = status
        if statuses != {key: value["vfy"] for key, value in expected.items()}:
            raise ValueError("vfy mutation sample differs from the parity outcomes")
        reference = object_record(
            object_record(row.get("mutmut_report"), "mutmut report").get("summary"),
            "mutmut summary",
        )
        actual = object_record(reference.get("outcomes"), "mutmut outcomes")
        if len(actual) != len(expected) or any(
            value not in {"killed", "survived"} for value in actual.values()
        ):
            raise ValueError("mutmut sample has missing or infrastructure outcomes")
        if actual != expected_mutmut:
            raise ValueError("mutmut sample differs from the parity outcomes")
    routes = {
        name: route(
            [row.get(f"{name}_ms") for row in measured],
            [
                object_record(row.get(f"{name}_report"), "mutation report").get("resources")
                for row in measured
            ],
            method["measured_runs"],
            helper=True,
        )
        for name in ("vfy", "mutmut")
    }
    return [
        paired_row(
            "real-mutation",
            workload,
            method,
            routes,
            "vfy",
            "mutmut",
            {
                "scope": data.get("scope"),
                "common_catalog": data.get("catalog"),
                "boundary": data["methodology"].get("boundary"),
                "memory_boundary": data["methodology"].get("resources"),
            },
        )
    ]


def native_xdist(data: Record) -> list[Record]:
    method = sampling(data.get("sampling"))
    output = []
    for project in records(data.get("projects"), "native projects"):
        if project.get("status") != "measured":
            output.append(
                {
                    "benchmark": "native-xdist",
                    "workload": project.get("project"),
                    "status": "blocked",
                    "details": project,
                }
            )
            continue
        parity = object_record(project.get("parity"), "native parity")
        rows = records(
            object_record(project.get("timing"), "native timing").get("samples"), "native samples"
        )
        if not rows:
            raise ValueError("native measured result has no samples")
        routes = {}
        for name in sorted({str(row.get("route")) for row in rows}):
            proof = object_record(parity.get(name), "native route parity")
            if proof.get("passed") is not True or proof.get("errors"):
                raise ValueError("native sampled route failed its parity gate")
            selected = [row for row in rows if row.get("route") == name]
            converted = [
                {**row, "phase": "measured" if row.get("phase") == "sample" else row.get("phase")}
                for row in selected
            ]
            measured = indexed_samples(converted, method, "phase")
            routes[name] = route(
                [positive(row.get("seconds"), "native seconds") * 1000 for row in measured],
                [row.get("resources") for row in measured],
                method["measured_runs"],
            )
        comparisons = []
        for name in routes:
            if not name.startswith("pytest-native"):
                continue
            reference = (
                name.replace("pytest-native", "stock", 1)
                if name.startswith("pytest-native-xdist-")
                else f"stock-xdist-{name.rsplit('j', 1)[-1]}"
            )
            if name in {"pytest-native-j1", "pytest-native-direct-j1"}:
                reference = "stock-xdist-0"
            if reference not in routes:
                raise ValueError(f"native route {name} has no measured stock reference")
            comparisons.append(comparison(routes, name, reference))
        if not comparisons:
            raise ValueError("native result contains no measured competitor comparison")
        output.append(
            {
                "benchmark": "native-xdist",
                "workload": project.get("project"),
                "status": "measured",
                "sampling": method,
                "publication_sampling": method["mode"] == "publication",
                "routes": routes,
                "comparisons": comparisons,
                "semantic_limits": data.get("method"),
                "cases": project.get("cases"),
            }
        )
        for name, proof in parity.items():
            if isinstance(proof, dict) and proof.get("passed") is False:
                output.append(
                    {
                        "benchmark": "native-xdist",
                        "workload": project.get("project"),
                        "route": name,
                        "status": "blocked",
                        "details": proof,
                    }
                )
    return output


def edit_loop_selection(data: Record) -> Record:
    """Retain the declared selection policy separately from the observed ID sets."""
    policy = data.get("selection_vs_testmon")
    if not isinstance(policy, str) or policy not in {"exact", "vfy_conservative"}:
        raise ValueError("edit-loop selection policy is missing or invalid")
    outcomes = object_record(data.get("outcomes"), "edit-loop outcomes")
    selected = object_record(outcomes.get("selected"), "vfy selected outcomes")
    reference = object_record(outcomes.get("testmon"), "testmon selected outcomes")
    oracle = object_record(outcomes.get("full_oracle"), "full oracle outcomes")
    for values in (selected, reference, oracle):
        if not values or any(
            not isinstance(nodeid, str) or not nodeid or outcome != "passed"
            for nodeid, outcome in values.items()
        ):
            raise ValueError("edit-loop selected or oracle outcomes are invalid")
    vfy_ids, testmon_ids, oracle_ids = set(selected), set(reference), set(oracle)
    if not vfy_ids < oracle_ids or not testmon_ids <= vfy_ids:
        raise ValueError("edit-loop selected test IDs violate oracle or conservative inclusion")
    if policy == "exact" and vfy_ids != testmon_ids:
        raise ValueError("edit-loop exact selection policy requires equal test ID sets")
    retained_ids = data.get("selected_tests")
    if (
        not isinstance(retained_ids, list)
        or any(not isinstance(nodeid, str) for nodeid in retained_ids)
        or len(retained_ids) != len(vfy_ids)
        or set(retained_ids) != vfy_ids
        or outcomes.get("vfy_default") != selected
    ):
        raise ValueError("edit-loop retained/default selected test IDs differ from vfy outcomes")
    return {
        "declared_policy": policy,
        "observed_relation": "equal" if vfy_ids == testmon_ids else "strict_superset",
        "vfy_test_ids": sorted(vfy_ids),
        "testmon_test_ids": sorted(testmon_ids),
        "oracle_test_ids": sorted(oracle_ids),
        "vfy_outcomes": selected,
        "testmon_outcomes": reference,
    }


def edit_loop(data: Record) -> list[Record]:
    method = sampling(data.get("methodology"))
    selection = edit_loop_selection(data)
    limits = {
        "methodology": {
            key: value
            for key, value in object_record(data.get("methodology"), "methodology").items()
            if key != "validation"
        },
        "affected_selection": selection,
    }
    sequences = records(
        object_record(data.get("developer_sequence"), "developer sequence").get("samples"),
        "sequence samples",
    )
    if len(sequences) != method["measured_runs"] or any(
        row.get("validation") != "passed" for row in sequences
    ):
        raise ValueError("edit-loop developer sequence validation is incomplete")
    outcomes = object_record(data.get("outcomes"), "edit-loop outcomes")
    if not outcomes.get("selected") or not outcomes.get("testmon"):
        raise ValueError("edit-loop selected outcomes are missing")
    breaking = object_record(outcomes.get("breaking"), "breaking outcomes")
    if any(
        "failed" not in object_record(breaking.get(name), "breaking tool outcomes").values()
        for name in ("vfy", "testmon")
    ):
        raise ValueError("edit-loop breaking edit did not fail both tools")
    resources = records(
        object_record(data.get("resources"), "edit-loop resources").get("samples"),
        "resource samples",
    )

    def extract(summary: object, workflow: str) -> Record:
        times = object_record(summary, "timing summary").get("samples_ms")
        evidence = [row.get("record") for row in resources if row.get("workflow") == workflow]
        return route(times, evidence, method["measured_runs"])

    output = []
    phases = (
        (
            "affected",
            data.get("selected"),
            data.get("testmon"),
            "affected-selected",
            "affected-testmon",
        ),
        (
            "unchanged",
            data["controls"]["unchanged"].get("vfy"),
            data["controls"]["unchanged"].get("testmon"),
            "unchanged-vfy",
            "unchanged-testmon",
        ),
        (
            "cold",
            data["controls"]["cold_baseline"].get("vfy"),
            data["controls"]["cold_baseline"].get("testmon"),
            "cold-vfy",
            "cold-testmon",
        ),
    )
    for phase, vfy, testmon, vfy_workflow, testmon_workflow in phases:
        routes = {"vfy": extract(vfy, vfy_workflow), "testmon": extract(testmon, testmon_workflow)}
        output.append(
            paired_row(
                "edit-loop",
                {"kind": data.get("workload"), "modules": data.get("modules"), "phase": phase},
                method,
                routes,
                "vfy",
                "testmon",
                limits,
            )
        )
    controls = {
        "default-vfy": extract(data.get("vfy_default"), "default-vfy"),
        "full-oracle": extract(data.get("full_oracle"), "affected-full"),
        "cold-plain": extract(data["controls"]["cold_baseline"].get("plain_pytest"), "cold-plain"),
    }
    sequence_phases = (
        "baseline",
        "unchanged",
        "local-edit",
        "shared-edit",
        "breaking",
        "repair",
        "full",
    )
    for phase in sequence_phases:
        times = []
        for sequence in sequences:
            phases = records(sequence.get("phases"), "developer sequence phases")
            if [item.get("phase") for item in phases] != list(sequence_phases):
                raise ValueError("missing or reordered developer sequence phase")
            times.append(next(item["elapsed_ms"] for item in phases if item["phase"] == phase))
        workflow = f"sequence-{phase}"
        controls[workflow] = route(
            times,
            [row.get("record") for row in resources if row.get("workflow") == workflow],
            method["measured_runs"],
            status=1 if phase == "breaking" else 0,
        )
    if len(resources) != 16 * method["measured_runs"]:
        raise ValueError("missing or extra edit-loop resource controls")
    output[0]["controls"] = controls
    return output


def generated_mutation(data: Record, path: Path) -> list[Record]:
    gates = object_record(data.get("gates", data.get("validation")), "mutation gates")
    if (
        not gates
        or any(value is False for value in gates.values())
        or gates.get("status", "passed") != "passed"
    ):
        raise ValueError("generated mutation gate failed")
    method = sampling(data.get("methodology"), mutation=True)
    parity = object_record(data.get("fixture_parity"), "generated mutation parity")
    if not parity.get("outcomes") or parity.get("real_call_failure_observed") is not True:
        raise ValueError("generated mutation lacks exact outcomes or killer failure proof")
    output = []
    for stage, field in (
        ("cold", "vfy_first_run"),
        ("warm", "vfy_warm"),
        ("cached", "vfy_incremental"),
    ):
        if field not in data:
            continue
        routes = {}
        for name, key, suffix in (
            ("vfy", field, f"vfy-{stage}"),
            ("mutmut", "mutmut", "mutmut-cold"),
        ):
            evidence = []
            artifacts = []
            for index in range(method["warmups"], method["warmups"] + method["measured_runs"]):
                retained = (
                    path.with_suffix(".artifacts")
                    / "resources"
                    / f"round-{index:03d}-{suffix}.json"
                )
                evidence.append(json.loads(retained.read_text(encoding="utf-8")))
                artifacts.append(
                    {
                        "path": str(retained),
                        "sha256": hashlib.sha256(retained.read_bytes()).hexdigest(),
                    }
                )
            routes[name] = route(
                object_record(data.get(key), "mutation timing").get("samples_ms"),
                evidence,
                method["measured_runs"],
            )
            routes[name]["resource_artifacts"] = artifacts
        output.append(
            paired_row(
                "mutation",
                stage,
                method,
                routes,
                "vfy",
                "mutmut",
                {
                    "catalog": data.get("catalog"),
                    "methodology": data.get("methodology"),
                    "mutmut_reference": (
                        "cold command; warm/cached vfy are different cache workloads"
                    ),
                },
            )
        )
    if not output:
        raise ValueError("generated mutation has no measured command samples")
    return output


def build_index(paths: list[Path], failed_gates: list[Path], date: str) -> Record:
    datetime.date.fromisoformat(date)
    result: Record = {
        "schema": "vfy.competitor-benchmark-index.v1",
        "date": date,
        "sources": [],
        "results": [],
        "blocked": [],
        "measurement": {
            "memory": (
                "sampled peak live-process-tree PSS; 20 ms target interval; "
                "process reads are sequential"
            ),
            "timing": "measured with resource sampling enabled; sampler overhead is included",
            "ratios": "reference divided by candidate; values above one favor the candidate",
            "comparison_flags": "median ordering only; interpret paired uncertainty separately",
        },
    }
    for path in paths:
        data = object_record(json.loads(path.read_text(encoding="utf-8")), str(path))
        name = data.get("benchmark")
        if "projects" in data and "sampling" in data:
            extracted = native_xdist(data)
            for row in extracted:
                if row["status"] != "measured":
                    continue
                identity_path = path.parent / str(row["workload"]) / "identity.json"
                identity = object_record(
                    json.loads(identity_path.read_text(encoding="utf-8")), "native identity"
                )
                prepared = object_record(identity.get("prepared"), "prepared native project")
                build = object_record(identity.get("build"), "native release build")
                revision = build.get("source_revision")
                if (
                    build.get("native_profile") != "release"
                    or not isinstance(revision, str)
                    or len(revision) != 40
                    or any(character not in "0123456789abcdef" for character in revision)
                    or len(str(build.get("wheel_sha256", ""))) != 64
                    or build["wheel_sha256"] != prepared.get("wheel_sha256")
                    or not prepared.get("revision")
                ):
                    raise ValueError("native build or pinned project identity is missing")
                row["identity"] = {
                    "path": str(identity_path),
                    "sha256": hashlib.sha256(identity_path.read_bytes()).hexdigest(),
                    "build": identity["build"],
                    "upstream_revision": prepared["revision"],
                    "wheel_sha256": prepared["wheel_sha256"],
                }
        elif name == "real-selection":
            extracted = real_selection(data)
        elif data.get("schema") == "vfy.real-mutation-benchmark":
            extracted = real_mutation(data)
        elif name == "edit-loop":
            extracted = edit_loop(data)
        elif name == "mutation":
            extracted = generated_mutation(data, path)
        else:
            raise ValueError(f"unsupported competitor result: {path}")
        provenance = data.get("provenance") or {
            key: data[key]
            for key in ("controller", "machine", "cpu_max", "source_input_sha256")
            if key in data
        }
        if not provenance:
            raise ValueError(f"missing benchmark provenance: {path}")
        provenance = object_record(provenance, "benchmark provenance")
        environment = object_record(
            provenance.get("environment", provenance.get("machine")), "machine provenance"
        )
        source_state = object_record(
            provenance.get("source", provenance.get("controller")), "controller provenance"
        )
        if not environment or not source_state.get("revision"):
            raise ValueError(f"missing machine or controller provenance: {path}")
        source = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "provenance": provenance,
        }
        result["sources"].append(source)
        for row in extracted:
            row["source"] = str(path)
            result["blocked" if row["status"] == "blocked" else "results"].append(row)
    for path in failed_gates:
        details = json.loads(path.read_text(encoding="utf-8"))
        result["blocked"].append(
            {
                "source": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "status": "blocked",
                "details": details,
            }
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, nargs="*")
    parser.add_argument("--failed-gate", type=Path, action="append", default=[])
    parser.add_argument("--date", required=True, help="measurement date, YYYY-MM-DD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.results and not args.failed_gate:
        parser.error("provide a result or --failed-gate artifact")
    index = build_index(args.results, args.failed_gate, args.date)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
