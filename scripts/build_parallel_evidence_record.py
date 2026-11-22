"""Condense complete, parity-gated native parallel runs without publishing raw logs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_parallel_chart_summary import (
    ROUTES,
    _check_route_commands,
    _series,
)
from scripts.build_stock_parallel_chart_summary import _junit, _outcomes

ROOT = Path(__file__).resolve().parents[1]

GATES = ("stock-xdist-0", *ROUTES)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_path(artifact: dict[str, Any]) -> Path:
    path = Path(artifact["path"])
    digest = artifact.get("retained_sha256") or artifact.get("sha256")
    if not path.is_file() or _sha256(path) != digest:
        raise ValueError(f"missing or modified retained artifact: {path}")
    return path


def _command_junit(path: Path, command: dict[str, Any]) -> list[dict[str, Any]]:
    argv = command["argv"]
    root = Path(command["cwd"])
    index = argv.index("--basetemp") if "--basetemp" in argv else -1
    basetemp = Path(argv[index + 1]) if index >= 0 else root.parent / "pytest-tmp"
    return _junit(path, root, basetemp)


def _gate_proof(
    name: str, parity: dict[str, Any], cases: int
) -> tuple[dict[str, Any], dict[str, Any], dict[str, list[dict[str, Any]]]]:
    commands = {}
    reduced_parity = {}
    junit_cases = {}
    for route in GATES:
        gate = parity.get(route)
        if (
            not isinstance(gate, dict)
            or gate.get("passed") is not True
            or gate.get("cases") != cases
            or gate.get("errors") != []
        ):
            raise ValueError(f"{name}: {route} did not pass the complete JUnit gate")
        artifacts = gate["artifacts"]
        commands[route] = _json(_artifact_path(artifacts["command.json"]))
        junit_cases[route] = _command_junit(_artifact_path(artifacts["junit.xml"]), commands[route])
        if len(junit_cases[route]) != cases:
            raise ValueError(f"{name}: {route} retained JUnit has wrong inventory")
        reduced_parity[route] = {"passed": True, "cases": cases, "errors": []}
    if _outcomes(junit_cases["stock-xdist-0"]) != _outcomes(junit_cases["stock-xdist-4"]):
        raise ValueError(f"{name}: stock serial and parallel JUnit outcomes differ")
    for route in ("pytest-native-xdist-4", "pytest-native-j4"):
        if junit_cases[route] != junit_cases["stock-xdist-4"]:
            raise ValueError(f"{name}: {route} JUnit differs from stock four-worker route")
    return commands, reduced_parity, junit_cases


def _sample_proof(
    name: str,
    samples: list[dict[str, Any]],
    commands: dict[str, Any],
    junit_cases: dict[str, list[dict[str, Any]]],
) -> None:
    if len(samples) != 2 * len(ROUTES):
        raise ValueError(f"{name}: incomplete three-route timing rotation")
    for sample in samples:
        if sample.get("phase") != "sample" or sample.get("route") not in ROUTES:
            raise ValueError(f"{name}: unexpected timing phase or route")
        if (
            type(sample.get("seconds")) not in (float, int)
            or not math.isfinite(sample["seconds"])
            or sample["seconds"] <= 0
        ):
            raise ValueError(f"{name}: invalid timing sample")
        route = sample["route"]
        actual = _command_junit(_artifact_path(sample["artifacts"]["junit.xml"]), commands[route])
        if actual != junit_cases[route]:
            raise ValueError(f"{name}: {route} timed JUnit changed from full preflight")


def _project(record_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    record = _json(record_path)
    projects = record.get("projects")
    if not isinstance(projects, list) or len(projects) != 1:
        raise ValueError("each native parallel input must contain one measured project")
    raw = projects[0]
    name = raw.get("project")
    if not isinstance(name, str) or raw.get("status") != "measured":
        raise ValueError("native parallel project did not finish measurement")
    controller = record.get("controller")
    sampling = record.get("sampling")
    method = record.get("method")
    inputs = record.get("source_input_sha256")
    if (
        not isinstance(controller, dict)
        or controller.get("dirty") is not False
        or not isinstance(controller.get("revision"), str)
        or not isinstance(sampling, dict)
        or sampling.get("mode") != "diagnostic"
        or sampling.get("warmups") != 0
        or sampling.get("measured_runs") != 2
        or not isinstance(method, dict)
        or method.get("timing_routes") != list(ROUTES)
        or method.get("workers") != [0, 4]
        or method.get("native_workers") != [4]
        or method.get("capture") != "no"
        or method.get("distribution") != "load"
        or not isinstance(inputs, dict)
        or not inputs
    ):
        raise ValueError(f"{name}: native parallel measurement changes the common protocol")
    identity_path = record_path.parent / name / "identity.json"
    parity_path = record_path.parent / name / "preflight" / "parity.json"
    samples_path = record_path.parent / name / "timing" / "samples.json"
    identity = _json(identity_path)
    parity = _json(parity_path)
    samples = _json(samples_path)
    if parity != raw.get("parity") or samples.get("samples") != raw.get("timing", {}).get(
        "samples"
    ):
        raise ValueError(f"{name}: retained gate or sample record differs from final result")
    build = identity["build"]
    wheel = build["wheel_sha256"]
    if identity["prepared"]["wheel_sha256"] != wheel:
        raise ValueError(f"{name}: prepared environment differs from build wheel")
    cases = raw.get("cases")
    selectors = raw.get("selectors")
    if not isinstance(cases, int) or cases <= 0 or not isinstance(selectors, list) or not selectors:
        raise ValueError(f"{name}: missing complete suite identity")
    commands, reduced_parity, junit_cases = _gate_proof(name, parity, cases)
    expected_samples = samples["samples"]
    _sample_proof(name, expected_samples, commands, junit_cases)
    row = {
        "project": name,
        "status": "measured",
        "cases": cases,
        "selectors": selectors,
        "controller": controller,
        "source_input_sha256": inputs,
        "method": method,
        "sampling": sampling,
        "wheel_sha256": wheel,
        "identity": {
            "build": build,
            "prepared_revision": identity["prepared"]["revision"],
            "preparation_inputs": identity["prepared"]["preparation_inputs"],
            "benchmark_inputs": identity["prepared"]["benchmark_inputs"],
            "setup_diff_sha256": hashlib.sha256(
                identity["prepared"]["setup_diff"].encode()
            ).hexdigest(),
        },
        "route_commands": commands,
        "parity": reduced_parity,
        "timing": {
            "summary": raw["timing"]["summary"],
            "samples": [
                {
                    "phase": sample["phase"],
                    "index": sample["index"],
                    "route": sample["route"],
                    "seconds": sample["seconds"],
                }
                for sample in expected_samples
            ],
        },
        "raw_sha256": {
            "result.json": _sha256(record_path),
            "identity.json": _sha256(identity_path),
            "preflight/parity.json": _sha256(parity_path),
            "timing/samples.json": _sha256(samples_path),
        },
    }
    _check_route_commands(row, wheel)
    for route in ROUTES:
        median, count = _series(row, route, 2)
        if (
            not math.isclose(
                median, statistics.median(row["timing"]["summary"][route]["samples_ms"])
            )
            or count != 2
        ):
            raise ValueError(f"{name}: {route} sample summary is invalid")
    return row, record


def build_record(paths: list[Path]) -> dict[str, Any]:
    if len(paths) < 2:
        raise ValueError("combined native parallel record needs at least two projects")
    rows = []
    sources = []
    for path in paths:
        row, source = _project(path)
        rows.append(row)
        sources.append(source)
    if len({row["project"] for row in rows}) != len(rows):
        raise ValueError("native parallel inputs repeat a project")
    wheel = rows[0]["wheel_sha256"]
    build = rows[0]["identity"]["build"]
    machine = sources[0].get("machine")
    if any(
        row["wheel_sha256"] != wheel
        or row["identity"]["build"] != build
        or source.get("machine") != machine
        or source.get("controller") != sources[0].get("controller")
        for row, source in zip(rows, sources, strict=True)
    ):
        raise ValueError(
            "native parallel rows use different wheel builds, machines, or controllers"
        )
    return {
        "schema": 1,
        "machine": machine,
        "wheel_sha256": wheel,
        "build_manifest_sha256": _sha256(Path(build["path"])),
        "projects": rows,
        "raw_archive": {"status": "retained-local"},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    record = build_record(args.records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
