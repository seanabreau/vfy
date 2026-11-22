"""Condense three serial coverage routes and two native parallel routes per project.

Only local parity-gated resource artifacts are read. Raw reports stay outside the
summary. Pair inputs by their project identity, not by command-line position.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from _pytest.junitxml import mangle_test_address

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.benchmark_projects import outcome_records

from scripts.build_parallel_evidence_record import _artifact_path, _command_junit
from scripts.build_stock_parallel_chart_summary import _outcomes
from scripts.summarize_memory_benchmarks import (
    _artifact,
    _commands,
    _digest,
    _matches_command,
    _object,
    _positive,
    _provenance,
    _read,
)

ROOT = Path(__file__).resolve().parents[1]

METHOD = "linux-process-tree-smaps-rollup"
INTERVAL = 20
SERIAL = ("plain pytest", "vfy", "pytest-cov")
PARALLEL = ("stock-xdist-4", "pytest-native-j4")
SCOPE = "sum of simultaneous live child and descendant PSS; collector excluded"


def _serial_junit_outcomes(
    records: list[tuple[str, str, str, str | None]], source: Path
) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Project pytest lifecycle records onto JUnit case identities and result tags."""
    cases: dict[str, dict[str, str]] = {}
    for nodeid, phase, outcome, _xfail in records:
        if phase not in {"setup", "call", "teardown"} or outcome not in {"passed", "skipped"}:
            raise ValueError(f"{source}: serial test did not have a successful JUnit outcome")
        phases = cases.setdefault(nodeid, {})
        if phase in phases:
            raise ValueError(f"{source}: repeated serial test lifecycle phase")
        phases[phase] = outcome
    normalized = []
    for nodeid, phases in cases.items():
        if not {"setup", "teardown"} <= phases.keys() or not (
            "call" in phases or phases["setup"] == "skipped"
        ):
            raise ValueError(f"{source}: incomplete serial test lifecycle")
        tags = ("skipped",) if "skipped" in phases.values() else ()
        address = mangle_test_address(nodeid)
        if len(address) < 2:
            raise ValueError(f"{source}: invalid serial JUnit test address")
        normalized.append(((".".join(address[:-1]), address[-1]), tags))
    return sorted(normalized)


def _median(values: Sequence[float], reported: object, label: str) -> float:
    if len(values) != 2:
        raise ValueError(f"{label}: expected two measured samples")
    median = float(statistics.median(values))
    if (
        not isinstance(reported, (int, float))
        or isinstance(reported, bool)
        or not math.isclose(median, reported, abs_tol=0.000001)
    ):
        raise ValueError(f"{label}: median differs from measured samples")
    return median


def _resource(raw: dict[str, Any], command: list[str], python: object, label: str) -> int:
    if (
        raw.get("schema_version") != 1
        or raw.get("method") != METHOD
        or raw.get("tree_scope") != SCOPE
        or raw.get("sample_interval_ms") != INTERVAL
        or raw.get("child_exit_status") != 0
        or not _digest(raw.get("collector_sha256"))
        or not _matches_command(raw.get("child_command"), ["$PYTHON", *command[1:]], python)
        or type(raw.get("snapshot_count")) is not int
        or raw["snapshot_count"] < 1
        or type(raw.get("peak_tree_pss_bytes")) is not int
        or raw["peak_tree_pss_bytes"] <= 0
        or type(raw.get("peak_tree_rss_bytes")) is not int
        or raw["peak_tree_rss_bytes"] < raw["peak_tree_pss_bytes"]
        or type(raw.get("peak_process_count")) is not int
        or raw["peak_process_count"] < 1
        or type(raw.get("tracked_process_count")) is not int
        or raw["tracked_process_count"] < raw["peak_process_count"]
    ):
        raise ValueError(f"{label}: invalid tree resource record")
    _positive(raw.get("child_wall_ms"), f"{label}: child wall time")
    gap = raw.get("max_sample_gap_ms")
    if (
        not isinstance(gap, (int, float))
        or isinstance(gap, bool)
        or not math.isfinite(gap)
        or gap < 0
    ):
        raise ValueError(f"{label}: invalid sample gap")
    peak = raw["peak_tree_pss_bytes"]
    assert isinstance(peak, int)
    return peak


def _serial_runtime(record: dict[str, Any], source: Path) -> dict[str, str]:
    environment = _object(record.get("environment"), f"{source}: environment")
    provenance = _object(record.get("provenance"), f"{source}: provenance")
    tools = _object(
        _object(provenance.get("environment"), f"{source}: provenance environment").get("tools"),
        f"{source}: tools",
    )
    runtime = {
        "python": environment.get("python"),
        "implementation": environment.get("implementation"),
        "pytest": tools.get("pytest"),
        "pytest_cov": environment.get("pytest_cov"),
        "vfy": environment.get("vfy"),
    }
    if (
        not all(isinstance(value, str) and value for value in runtime.values())
        or tools.get("python") != runtime["python"]
        or tools.get("pytest-cov") != runtime["pytest_cov"]
        or tools.get("vfy") != runtime["vfy"]
    ):
        raise ValueError(f"{source}: incomplete or inconsistent serial runtime")
    return {key: value for key, value in runtime.items() if isinstance(value, str)}


def _serial(source: Path) -> dict[str, Any]:
    record = _read(source)
    project = _object(record.get("project"), f"{source}: project")
    method = _object(record.get("methodology"), f"{source}: methodology")
    resources_method = _object(method.get("resources"), f"{source}: resource method")
    if (
        record.get("schema") != 3
        or record.get("benchmark") != "projects"
        or record.get("mode") != "diagnostic"
        or record.get("warmups") != 0
        or record.get("runs") != 2
        or method.get("sampling") != {"warmups": 0, "measured_runs": 2}
        or method.get("phases") != ["serial"]
        or method.get("coverage_context") != "test"
        or method.get("reports") not in (["term", "json"], ["term", "json:coverage.json"])
        or resources_method.get("method") != METHOD
        or resources_method.get("rss_scope")
        != "sampled sum of simultaneous live child and descendant PSS"
        or project.get("phases") != ["serial"]
    ):
        raise ValueError(f"{source}: serial coverage and tree resource protocol differs")
    commands = _commands(record, source)
    provenance = _provenance(record, source)
    runtime = _serial_runtime(record, source)
    parity_descriptor = _object(record.get("resource_parity_artifact"), f"{source}: parity")
    parity = _artifact(parity_descriptor, f"{source}: parity")
    if (
        parity.get("parity") != "exact"
        or parity.get("project") != project.get("name")
        or parity.get("commit") != project.get("commit")
        or parity.get("vfy_commit") != provenance["source_revision"]
        or parity.get("methodology") != method
        or parity.get("provenance") != record.get("provenance")
    ):
        raise ValueError(f"{source}: exact matching serial coverage gate required")
    parity_path = Path(parity_descriptor["path"])
    outcomes = outcome_records(parity_path.parent / "pytest-cov-serial-outcomes.jsonl")
    cases = len({nodeid for nodeid, _phase, _outcome, _xfail in outcomes})
    if cases <= 0:
        raise ValueError(f"{source}: empty full-suite outcome inventory")
    junit_outcomes = _serial_junit_outcomes(outcomes, source)
    if len(junit_outcomes) != cases:
        raise ValueError(f"{source}: serial JUnit identities are not unique")
    resources = _object(record.get("resources"), f"{source}: resources")
    python = _object(record.get("preparation"), f"{source}: preparation").get("python")
    routes = {}
    collector: str | None = None
    for tool in SERIAL:
        series = _object(resources.get(tool), f"{source}: {tool} resources")
        descriptors = series.get("records")
        reported_memory = series.get("samples_peak_tree_pss_bytes")
        if (
            not isinstance(descriptors, list)
            or len(descriptors) != 2
            or not isinstance(reported_memory, list)
        ):
            raise ValueError(f"{source}: {tool} tree samples are incomplete")
        memory = []
        times = []
        for index, descriptor in enumerate(descriptors):
            item = _object(descriptor, f"{source}: {tool} sample")
            raw = _artifact(item, f"{source}: {tool} sample")
            receipt = _object(item.get("validation"), f"{source}: {tool} validation")
            if (
                item.get("peak_tree_pss_bytes") != raw.get("peak_tree_pss_bytes")
                or item.get("child_command") != raw.get("child_command")
                or item.get("collector_sha256") != raw.get("collector_sha256")
                or receipt.get("outcomes") != "passed"
                or receipt.get("coverage_data")
                != ("not applicable" if tool == "plain pytest" else "passed")
            ):
                raise ValueError(f"{source}: {tool} measured evidence differs")
            peak = _resource(raw, commands[tool], python, f"{source}: {tool}")
            if index >= len(reported_memory) or reported_memory[index] != peak:
                raise ValueError(f"{source}: {tool} reported memory differs from raw sample")
            digest = raw["collector_sha256"]
            assert isinstance(digest, str)
            if collector is not None and collector != digest:
                raise ValueError(f"{source}: mixed tree resource collectors")
            collector = digest
            memory.append(peak)
            times.append(float(raw["child_wall_ms"]))
        timing = _object(record.get(tool), f"{source}: {tool} time series")
        reported_times = timing.get("samples_ms")
        if not isinstance(reported_times, list) or any(
            not math.isclose(actual, reported, abs_tol=0.000001)
            for actual, reported in zip(times, reported_times, strict=True)
        ):
            raise ValueError(f"{source}: {tool} time samples differ from child records")
        routes[tool] = {
            "time_samples_ms": times,
            "time_median_ms": _median(times, timing.get("median_ms"), f"{source}: {tool} time"),
            "peak_tree_pss_samples_bytes": memory,
            "peak_tree_pss_median_bytes": _median(
                memory, series.get("median_peak_tree_pss_bytes"), f"{source}: {tool} PSS"
            ),
        }
    return {
        "key": project["key"],
        "project": project["name"],
        "scope": project["scope"],
        "selectors": parity["tests"],
        "cases": cases,
        "junit_outcomes": junit_outcomes,
        "routes": routes,
        "provenance": {**provenance, "serial_collector_sha256": collector, "runtime": runtime},
    }


def _parallel_gate(
    source: Path,
    parity: dict[str, Any],
    cases_count: int,
    serial_outcomes: list[tuple[tuple[str, ...], tuple[str, ...]]],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    commands = {}
    cases = {}
    for route in ("stock-xdist-0", *PARALLEL):
        gate = _object(parity.get(route), f"{source}: {route} gate")
        if (
            gate.get("passed") is not True
            or gate.get("errors") != []
            or gate.get("cases") != cases_count
        ):
            raise ValueError(f"{source}: {route} full-suite gate failed")
        artifacts = _object(gate.get("artifacts"), f"{source}: {route} artifacts")
        command = _artifact_path(artifacts["command.json"])
        commands[route] = _read(command)
        cases[route] = _command_junit(_artifact_path(artifacts["junit.xml"]), commands[route])
        if len(cases[route]) != cases_count:
            raise ValueError(f"{source}: {route} JUnit inventory differs")
    if (
        _outcomes(cases["stock-xdist-0"]) != _outcomes(cases["stock-xdist-4"])
        or cases["pytest-native-j4"] != cases["stock-xdist-0"]
        or _outcomes(cases["stock-xdist-0"]) != serial_outcomes
    ):
        raise ValueError(f"{source}: serial and parallel full-suite identities or outcomes differ")
    return commands, cases


def _check_collector_input(record: dict[str, Any], source: Path, digest: str) -> None:
    inputs = _object(record.get("source_input_sha256"), f"{source}: source inputs")
    if inputs.get("validation/pytest_runner/benchmark_child_resources.py") != digest:
        raise ValueError(f"{source}: resource collector differs from serial")


def _check_native_runtime(identity: dict[str, Any], serial: dict[str, Any], source: Path) -> None:
    installed = _object(identity.get("installed"), f"{source}: installed runtime")
    dependencies = installed.get("dependencies")
    if not isinstance(dependencies, list) or not all(
        isinstance(item, list) and len(item) == 2 for item in dependencies
    ):
        raise ValueError(f"{source}: installed dependencies are missing")
    versions = dict(dependencies)
    runtime = serial["provenance"]["runtime"]
    if (
        installed.get("python") != runtime["python"]
        or installed.get("implementation") != runtime["implementation"]
        or installed.get("vfy") != runtime["vfy"]
        or versions.get("pytest") != runtime["pytest"]
        or versions.get("pytest-cov") != runtime["pytest_cov"]
    ):
        raise ValueError(f"{source}: installed native runtime differs from serial")


def _parallel(source: Path, serial: dict[str, Any]) -> dict[str, Any]:
    record = _read(source)
    projects = record.get("projects")
    if not isinstance(projects, list) or len(projects) != 1:
        raise ValueError(f"{source}: expected one complete parallel project")
    project = _object(projects[0], f"{source}: project")
    sampling = _object(record.get("sampling"), f"{source}: sampling")
    method = _object(record.get("method"), f"{source}: method")
    controller = _object(record.get("controller"), f"{source}: controller")
    if (
        project.get("status") != "measured"
        or project.get("project") != serial["key"]
        or project.get("cases") != serial["cases"]
        or project.get("selectors") != serial["selectors"]
        or sampling.get("mode") != "diagnostic"
        or sampling.get("warmups") != 0
        or sampling.get("measured_runs") != 2
        or method.get("timing_routes") != list(PARALLEL)
        or method.get("workers") != [0, 4]
        or method.get("native_workers") != [4]
        or method.get("capture") != "no"
        or method.get("override_upstream_capture") is not True
        or method.get("resource_method") != METHOD
        or controller.get("dirty") is not False
        or not isinstance(controller.get("revision"), str)
    ):
        raise ValueError(f"{source}: parallel full-suite and tree protocol differs")
    identity = _read(source.parent / serial["key"] / "identity.json")
    build = _object(identity.get("build"), f"{source}: build")
    prepared = _object(identity.get("prepared"), f"{source}: preparation")
    serial_provenance = serial["provenance"]
    if (
        build.get("native_profile") != "release"
        or build.get("wheel_sha256") != serial_provenance["wheel_sha256"]
        or build.get("source_revision") != serial_provenance["source_revision"]
        or prepared.get("wheel_sha256") != serial_provenance["wheel_sha256"]
        or prepared.get("revision") != serial_provenance["upstream_revision"]
    ):
        raise ValueError(f"{source}: installed wheel or upstream revision differs from serial")
    _check_collector_input(record, source, serial_provenance["serial_collector_sha256"])
    _check_native_runtime(identity, serial, source)
    parity = _read(source.parent / serial["key"] / "preflight" / "parity.json")
    timing = _object(project.get("timing"), f"{source}: timing")
    retained_samples = _read(source.parent / serial["key"] / "timing" / "samples.json")
    if parity != project.get("parity") or retained_samples.get("samples") != timing.get("samples"):
        raise ValueError(f"{source}: retained parallel gate or samples differ from result")
    commands, cases = _parallel_gate(source, parity, serial["cases"], serial["junit_outcomes"])
    samples = timing["samples"]
    if not isinstance(samples, list) or len(samples) != 4:
        raise ValueError(f"{source}: incomplete two-route rotation")
    routes = {}
    collector: str | None = None
    for route in PARALLEL:
        chosen = [
            sample
            for sample in samples
            if sample.get("phase") == "sample" and sample.get("route") == route
        ]
        if len(chosen) != 2 or sorted(sample["index"] for sample in chosen) != [0, 1]:
            raise ValueError(f"{source}: {route} requires two measured samples")
        chosen.sort(key=lambda sample: sample["index"])
        memory = []
        times = []
        for sample in chosen:
            artifacts = _object(sample.get("artifacts"), f"{source}: {route} sample artifacts")
            status = _read(_artifact_path(artifacts["status.json"]))
            sample_command = _read(_artifact_path(artifacts["command.json"]))
            if status.get("completed") is not True or status.get("exit_status") != 0:
                raise ValueError(f"{source}: {route} timed command failed")
            if (
                _command_junit(_artifact_path(artifacts["junit.xml"]), commands[route])
                != cases[route]
            ):
                raise ValueError(f"{source}: {route} timed JUnit differs from full gate")
            raw = _read(_artifact_path(artifacts["resources.json"]))
            if raw != sample.get("resources") or sample_command.get("argv") != raw.get(
                "child_command"
            ):
                raise ValueError(f"{source}: {route} resource record differs from timed command")
            peak = _resource(
                raw, raw["child_command"], raw["child_command"][0], f"{source}: {route}"
            )
            if not math.isclose(sample["seconds"] * 1000, raw["child_wall_ms"], abs_tol=0.000001):
                raise ValueError(f"{source}: {route} wall time differs from child record")
            digest = raw["collector_sha256"]
            assert isinstance(digest, str)
            if collector is not None and collector != digest:
                raise ValueError(f"{source}: mixed parallel resource collectors")
            collector = digest
            memory.append(peak)
            times.append(float(raw["child_wall_ms"]))
        series = _object(
            _object(timing.get("summary"), f"{source}: summary").get(route),
            f"{source}: {route} series",
        )
        reported_times = series.get("samples_ms")
        if not isinstance(reported_times, list) or reported_times != times:
            raise ValueError(f"{source}: {route} time series differs from samples")
        routes[route] = {
            "time_samples_ms": times,
            "time_median_ms": _median(times, series.get("median_ms"), f"{source}: {route} time"),
            "peak_tree_pss_samples_bytes": memory,
            "peak_tree_pss_median_bytes": float(statistics.median(memory)),
        }
    return {
        "routes": routes,
        "provenance": {
            "native_controller_revision": controller["revision"],
            "parallel_collector_sha256": collector,
        },
    }


def build_summary(serial_results: list[Path], parallel_results: list[Path]) -> dict[str, Any]:
    serial = {}
    for path in serial_results:
        row = _serial(path)
        if row["key"] in serial:
            raise ValueError(f"duplicate serial project: {row['key']}")
        serial[row["key"]] = row
    if set(serial) != {"packaging", "rich", "attrs"} or len(parallel_results) != 3:
        raise ValueError("README tree summary requires Packaging, Rich, and attrs pairs")
    parallel = {}
    for path in parallel_results:
        raw = _read(path)
        projects = raw.get("projects")
        if not isinstance(projects, list) or len(projects) != 1:
            raise ValueError(f"{path}: expected one parallel project")
        key = _object(projects[0], f"{path}: project").get("project")
        if key not in serial or key in parallel:
            raise ValueError(f"{path}: missing or duplicate serial partner")
        parallel[key] = _parallel(path, serial[key])
    if set(parallel) != set(serial):
        raise ValueError("each serial project needs one parallel partner")
    wheels = {row["provenance"]["wheel_sha256"] for row in serial.values()}
    if len(wheels) != 1:
        raise ValueError("README tree summary mixes release wheels")
    runtimes = {tuple(sorted(row["provenance"]["runtime"].items())) for row in serial.values()}
    if len(runtimes) != 1:
        raise ValueError("README tree summary mixes Python or pytest runtimes")
    rows = []
    for key in ("packaging", "rich", "attrs"):
        row = serial[key]
        native = parallel[key]
        if (
            row["provenance"]["serial_collector_sha256"]
            != native["provenance"]["parallel_collector_sha256"]
        ):
            raise ValueError(f"{key}: serial and parallel routes used different collectors")
        rows.append(
            {
                "project": row["project"],
                "scope": row["scope"],
                "parity": "exact",
                "warmups": 0,
                "runs": 2,
                "cases": row["cases"],
                "routes": {**row["routes"], **native["routes"]},
                "provenance": {
                    **row["provenance"],
                    **native["provenance"],
                    "collector_sha256": row["provenance"]["serial_collector_sha256"],
                },
                "gates": {"serial_coverage": "exact", "parallel_junit": "passed"},
            }
        )
    return {
        "schema": 1,
        "metric": "peak_tree_pss_bytes",
        "method": METHOD,
        "sample_interval_ms": INTERVAL,
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-result", type=Path, action="append", required=True)
    parser.add_argument("--parallel-result", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = build_summary(args.serial_result, args.parallel_result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
