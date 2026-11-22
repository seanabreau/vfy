"""Summarize parity-gated Linux peak RSS runs without publishing raw artifacts.

Usage: python scripts/summarize_memory_benchmarks.py --result result.json \
    [--result other.json] --output memory-summary.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any

TOOLS = ("plain pytest", "vfy", "pytest-cov")
METHOD = "linux-single-child-rusage"


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label}: expected object")
    return value


def _read(path: Path) -> dict[str, Any]:
    return _object(json.loads(path.read_text(encoding="utf-8")), str(path))


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _artifact(descriptor: object, label: str) -> dict[str, Any]:
    item = _object(descriptor, label)
    path_value, digest = item.get("path"), item.get("sha256")
    if not isinstance(path_value, str) or not _digest(digest):
        raise ValueError(f"{label}: missing artifact path or SHA-256")
    path = Path(path_value)
    if not path.is_file() or _hash(path) != digest:
        raise ValueError(f"{label}: missing or changed artifact")
    return _read(path)


def _positive(value: object, label: str) -> float:
    if (
        not isinstance(value, (float, int))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label}: expected positive finite number")
    return float(value)


def _commands(record: dict[str, Any], source: Path) -> dict[str, list[str]]:
    commands = _object(record.get("commands"), f"{source}: commands")
    argvs: dict[str, list[str]] = {}
    for tool in TOOLS:
        phases = commands.get(tool)
        if not isinstance(phases, list) or len(phases) != 1:
            raise ValueError(f"{source}: {tool} requires one serial command")
        phase = _object(phases[0], f"{source}: {tool} phase")
        argv = phase.get("argv")
        if (
            phase.get("phase") != "serial"
            or not isinstance(argv, list)
            or not all(isinstance(arg, str) for arg in argv)
        ):
            raise ValueError(f"{source}: {tool} requires one serial command")
        argvs[tool] = argv
    reference, candidate, plain = (argvs[tool] for tool in ("pytest-cov", "vfy", "plain pytest"))
    if (
        reference[:3] != ["$PYTHON", "-m", "pytest"]
        or candidate
        != ["vfy.pytest_plugin" if arg == "pytest_cov.plugin" else arg for arg in reference]
        or reference.count("pytest_cov.plugin") != 1
        or "--cov-context=test" not in reference
        or "--cov-report=term" not in reference
        or not any(
            arg in {"--cov-report=json", "--cov-report=json:coverage.json"} for arg in reference
        )
        or not any(arg.startswith("--cov-config=") for arg in reference)
        or not any(arg.startswith("--cov=") for arg in reference)
        or plain[:3] != ["$PYTHON", "-m", "pytest"]
        or any(arg.startswith("--cov") for arg in plain)
    ):
        raise ValueError(f"{source}: incompatible coverage, context, or report commands")
    return argvs


def _matches_command(actual: object, shown: list[str], python: object) -> bool:
    if not isinstance(actual, list) or len(actual) != len(shown):
        return False
    for index, (raw, expected) in enumerate(zip(actual, shown, strict=True)):
        if not isinstance(raw, str):
            return False
        if index == 0:
            if expected != "$PYTHON" or raw != python:
                return False
        elif "$PROJECT" in expected:
            prefix, suffix = expected.split("$PROJECT", 1)
            if not raw.startswith(prefix) or not raw.endswith(suffix):
                return False
        elif raw != expected:
            return False
    return True


def _provenance(record: dict[str, Any], source: Path) -> dict[str, str]:
    project = _object(record.get("project"), f"{source}: project")
    provenance = _object(record.get("provenance"), f"{source}: provenance")
    build = _object(provenance.get("vfy_build"), f"{source}: release build")
    engine = _object(provenance.get("engine_source"), f"{source}: engine source")
    source_meta = _object(provenance.get("source"), f"{source}: controller source")
    prepared = _object(record.get("preparation"), f"{source}: preparation")
    prepared_wheel = _object(prepared.get("vfy_wheel"), f"{source}: prepared wheel")
    interpreter = _object(provenance.get("interpreter"), f"{source}: interpreter")
    controller, upstream, revision, wheel = (
        source_meta.get("revision"),
        project.get("commit"),
        build.get("source_revision"),
        build.get("wheel_sha256"),
    )
    if (
        not all(
            isinstance(value, str) and len(value) == 40
            for value in (controller, upstream, revision)
        )
        or not _digest(wheel)
        or not _digest(build.get("extension_sha256"))
        or build.get("native_profile") != "release"
        or source_meta.get("dirty") is not False
        or project.get("vfy_commit") != revision
        or engine.get("revision") != revision
        or provenance.get("upstream_revision") != upstream
        or prepared.get("revision") != upstream
        or prepared_wheel.get("wheel_sha256") != wheel
        or prepared_wheel.get("source_revision") != revision
        or interpreter.get("extension_sha256") != build.get("extension_sha256")
        or _object(provenance.get("validation"), f"{source}: validation").get("status") != "passed"
    ):
        raise ValueError(f"{source}: release build or source provenance differs")
    assert isinstance(controller, str)
    assert isinstance(upstream, str)
    assert isinstance(revision, str)
    assert isinstance(wheel, str)
    assert isinstance(build["extension_sha256"], str)
    return {
        "controller_revision": controller,
        "upstream_revision": upstream,
        "source_revision": revision,
        "wheel_sha256": wheel,
        "extension_sha256": build["extension_sha256"],
    }


def _result(source: Path) -> tuple[dict[str, Any], str]:
    record = _read(source)
    project = _object(record.get("project"), f"{source}: project")
    methodology = _object(record.get("methodology"), f"{source}: methodology")
    resource_method = _object(methodology.get("resources"), f"{source}: resource method")
    if (
        record.get("schema") != 3
        or record.get("benchmark") != "projects"
        or record.get("mode") != "diagnostic"
        or record.get("warmups") != 0
        or record.get("runs") != 2
        or methodology.get("mode") != "diagnostic"
        or methodology.get("warmups") != 0
        or methodology.get("measured_runs") != 2
        or methodology.get("sampling") != {"warmups": 0, "measured_runs": 2}
        or methodology.get("phases") != ["serial"]
        or methodology.get("coverage_context") != "test"
        or methodology.get("reports") not in (["term", "json"], ["term", "json:coverage.json"])
        or resource_method.get("method") != METHOD
        or resource_method.get("rss_scope")
        != "largest individual process peak, not concurrent process-tree RSS"
        or project.get("phases") != ["serial"]
        or not isinstance(project.get("name"), str)
        or not isinstance(project.get("scope"), str)
    ):
        raise ValueError(f"{source}: incompatible serial resource protocol")
    argvs = _commands(record, source)
    provenance = _provenance(record, source)
    parity = _artifact(record.get("resource_parity_artifact"), f"{source}: parity")
    if (
        parity.get("parity") != "exact"
        or parity.get("project") != project["name"]
        or parity.get("commit") != project["commit"]
        or parity.get("vfy_commit") != provenance["source_revision"]
        or parity.get("methodology") != methodology
        or parity.get("provenance") != record.get("provenance")
    ):
        raise ValueError(f"{source}: exact matching parity gate required")
    resources = _object(record.get("resources"), f"{source}: resources")
    python = _object(record["preparation"], f"{source}: preparation").get("python")
    values: dict[str, Any] = {}
    collector: str | None = None
    for tool in TOOLS:
        series = _object(resources.get(tool), f"{source}: {tool} resources")
        samples, descriptors = series.get("samples_peak_rss_bytes"), series.get("records")
        if (
            not isinstance(samples, list)
            or not isinstance(descriptors, list)
            or len(samples) != 2
            or len(descriptors) != 2
        ):
            raise ValueError(f"{source}: {tool} requires two retained resource samples")
        measured = []
        for index, descriptor in enumerate(descriptors):
            item = _object(descriptor, f"{source}: {tool} sample {index + 1}")
            raw = _artifact(item, f"{source}: {tool} sample {index + 1}")
            receipt = _object(item.get("validation"), f"{source}: {tool} validation")
            if (
                item.get("peak_rss_bytes") != raw.get("peak_rss_bytes")
                or item.get("collector_sha256") != raw.get("collector_sha256")
                or item.get("child_command") != raw.get("child_command")
                or item.get("child_exit_status") != raw.get("child_exit_status")
                or raw.get("schema_version") != 1
                or raw.get("method") != METHOD
                or raw.get("rss_scope")
                != "largest process RSS, not a sum of concurrent descendants"
                or raw.get("child_exit_status") != 0
                or receipt.get("outcomes") != "passed"
                or receipt.get("coverage_data")
                != ("not applicable" if tool == "plain pytest" else "passed")
                or not _digest(raw.get("collector_sha256"))
                or not _matches_command(raw.get("child_command"), argvs[tool], python)
                or raw["child_command"].count("vfy.pytest_plugin") != (1 if tool == "vfy" else 0)
                or raw["child_command"].count("pytest_cov.plugin")
                != (1 if tool == "pytest-cov" else 0)
            ):
                raise ValueError(f"{source}: {tool} resource record or validation differs")
            _positive(raw.get("child_wall_ms"), f"{source}: {tool} child wall")
            peak = _positive(raw.get("peak_rss_bytes"), f"{source}: {tool} peak RSS")
            if type(raw["peak_rss_bytes"]) is not int or samples[index] != raw["peak_rss_bytes"]:
                raise ValueError(f"{source}: {tool} sample differs from retained record")
            digest = raw["collector_sha256"]
            assert isinstance(digest, str)
            if collector is not None and collector != digest:
                raise ValueError(f"{source}: mixed resource collectors")
            collector = digest
            measured.append(peak)
        median = statistics.median(measured)
        if series.get("median_peak_rss_bytes") != median:
            raise ValueError(f"{source}: {tool} median differs from samples")
        values[tool] = {"samples_bytes": [int(value) for value in measured], "median_bytes": median}
        timings = _object(record.get(tool), f"{source}: {tool} timings")
        if not isinstance(timings.get("samples_ms"), list) or len(timings["samples_ms"]) != 2:
            raise ValueError(f"{source}: {tool} timing rotation incomplete")
        if argvs[tool][:3] != ["$PYTHON", "-m", "pytest"]:
            raise ValueError(f"{source}: {tool} command has wrong runner")
    assert collector is not None
    return (
        {
            "project": project["name"],
            "scope": project["scope"],
            "parity": "exact",
            "warmups": 0,
            "runs": 2,
            "values": values,
            "provenance": {**provenance, "collector_sha256": collector},
        },
        provenance["wheel_sha256"],
    )


def build_summary(paths: list[Path]) -> dict[str, Any]:
    if not paths:
        raise ValueError("at least one benchmark result is required")
    rows = []
    seen: set[str] = set()
    wheel: str | None = None
    for source in paths:
        row, current_wheel = _result(source)
        if row["project"] in seen or (wheel is not None and wheel != current_wheel):
            raise ValueError("duplicate project or mixed release wheels")
        seen.add(row["project"])
        wheel = current_wheel
        rows.append(row)
    return {
        "schema": 1,
        "metric": "largest_process_peak_rss_bytes",
        "method": METHOD,
        "route": "serial-stock-pytest-coverage",
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = build_summary(args.result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
