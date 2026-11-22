"""Audit retained native publication cohorts and calculate paired uncertainty.

This reads existing benchmark artifacts; it never executes a workload. Memory
and timing-only cohorts stay separate, including their matching round indices.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import statistics
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.benchmark_harness import paired_uncertainty, rotated_orders
from validation.pytest_runner import check_native_adoption_matrix as matrix
from validation.pytest_runner.benchmark_vfy_coverage import verify_installed_distribution_files
from validation.pytest_runner.check_native_xdist_adoption import (
    compare_phase_outcomes,
    junit_records,
)

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ("stock-xdist-4", "pytest-native-xdist-4", "pytest-native-j4")
CASES = {"rich": 981, "attrs": 1_412, "click": 2_016, "packaging": 62_435}


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json(payload: bytes) -> dict[str, Any]:
    # Retained harness records have heterogeneous, externally supplied schemas.
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


@dataclass(frozen=True)
class Reader:
    """Resolve an archived path without rewriting its retained identity."""

    relocations: tuple[tuple[str, str], ...] = ()

    def path(self, value: str) -> Path:
        for original, restored in self.relocations:
            if value == original or value.startswith(original.rstrip("/") + "/"):
                return Path(restored + value[len(original) :])
        return Path(value)

    def artifact(self, record: Mapping[str, Any]) -> bytes:
        path = self.path(record["path"])
        retained = path.read_bytes()
        if (
            _digest(retained) != record["retained_sha256"]
            or len(retained) != record["retained_bytes"]
        ):
            raise ValueError(f"retained artifact changed: {path}")
        payload = gzip.decompress(retained) if path.suffix == ".gz" else retained
        if _digest(payload) != record["sha256"] or len(payload) != record["bytes"]:
            raise ValueError(f"raw artifact changed: {path}")
        return payload


def validate_rotation(
    samples: Sequence[dict[str, Any]],
    *,
    memory: bool,
    native_environment: Mapping[str, str] | None = None,
) -> None:
    """Require complete, ordered experimental blocks before pairing samples."""
    expected = [
        (phase, index, route)
        for phase, count in (("warmup", 3), ("sample", 15))
        for index, order in enumerate(rotated_orders(ROUTES, count))
        for route in order
    ]
    observed = [(row.get("phase"), row.get("index"), row.get("route")) for row in samples]
    if observed != expected or any(type(row.get("index")) is not int for row in samples):
        raise ValueError("missing, duplicated, or reordered experimental blocks")
    for row in samples:
        expected_environment = (
            dict(native_environment or {}) if row["route"].startswith("pytest-native") else {}
        )
        if row.get("native_environment") != expected_environment:
            raise ValueError("sample native experiment flags differ from declared route")
        seconds = row.get("seconds")
        if (
            not isinstance(seconds, (float, int))
            or isinstance(seconds, bool)
            or not math.isfinite(seconds)
            or seconds <= 0
        ):
            raise ValueError("invalid command wall time")
        if ("resources" in row) != memory:
            raise ValueError("memory-sampled and timing-only cohorts cannot be mixed")
        if memory and (
            row["resources"].get("method") != "linux-process-tree-smaps-rollup"
            or row["resources"].get("sample_interval_ms") != 20
            or row["resources"].get("child_exit_status") != 0
            or row["resources"].get("peak_tree_pss_bytes", 0) <= 0
            or type(row["resources"].get("peak_tree_pss_bytes")) is not int
            or not math.isclose(row["resources"]["child_wall_ms"], seconds * 1_000)
        ):
            raise ValueError("invalid process-tree resource record")


def _has(argv: list[str], key: str, value: str) -> bool:
    return any(argv[index : index + 2] == [key, value] for index in range(len(argv) - 1))


def validate_command(command: dict[str, Any], route: str, selectors: list[str]) -> None:
    """Keep public route identity, workload, worker count, and reporting fixed."""
    argv = command["argv"]
    if (
        not selectors
        or argv[-len(selectors) :] != selectors
        or "--capture=no" not in argv
        or not any(arg.startswith("--junitxml=") for arg in argv)
        or not _has(argv, "-o", "junit_logging=all")
        or not _has(argv, "-o", "junit_log_passing_tests=true")
        or any(arg.startswith("--cov") for arg in argv)
        or not command.get("cwd")
    ):
        raise ValueError("command changed workload, capture, coverage, or JUnit reporting")
    workers = [
        arg
        for arg in argv
        if arg == "-n"
        or arg.startswith("--numprocesses")
        or (arg.startswith("-n") and len(arg) > 2)
    ]
    native_workers = [arg for arg in argv if arg.startswith("--workers")]
    if route.startswith("stock"):
        valid = (
            argv[1:4] == ["-P", "-m", "pytest"]
            and workers == ["-n"]
            and _has(argv, "-n", route.rsplit("-", 1)[1])
            and "--dist=load" in argv
            and not native_workers
        )
    else:
        valid = argv[1:4] == ["-m", "vfy", "test"] and "--no-cov" in argv
        if route == "pytest-native-j4":
            valid = valid and not workers and native_workers == ["--workers=4"]
        else:
            valid = (
                valid
                and len(workers) == 1
                and _has(argv, workers[0], "4")
                and "--dist=load" in argv
                and not native_workers
            )
    if not valid:
        raise ValueError(f"command does not execute the declared route: {route}")


def _command_identity(command: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    argv = command["argv"]
    cwd = command.get("cwd")
    if not isinstance(cwd, str):
        raise ValueError("command lacks a string checkout identity")
    normalized: list[str] = []
    for index, arg in enumerate(argv):
        if not isinstance(arg, str):
            raise ValueError("command arguments must be strings")
        if arg.startswith("--junitxml="):
            normalized.append("--junitxml=<OUTPUT>")
        elif index and argv[index - 1] == "--history":
            normalized.append("<HISTORY>")
        else:
            normalized.append(arg)
    return cwd, tuple(normalized)


def _read_run(reader: Reader, artifacts: dict[str, Any]) -> tuple[dict[str, Any], bytes, float]:
    payloads = {name: reader.artifact(value) for name, value in artifacts.items()}
    status = _json(payloads["status.json"])
    if (
        status.get("completed") is not True
        or type(status.get("exit_status")) is not int
        or status.get("exit_status") != 0
    ):
        raise ValueError("retained child did not complete successfully")
    return _json(payloads["command.json"]), payloads["junit.xml"], status["seconds"]


def _junit(payload: bytes, command: dict[str, Any]) -> list[dict[str, Any]]:
    root = Path(command["cwd"])
    argv = command["argv"]
    basetemp = (
        Path(argv[argv.index("--basetemp") + 1])
        if "--basetemp" in argv
        else root.parent / "pytest-tmp"
    )
    with tempfile.TemporaryDirectory(prefix="vfy-junit-audit-") as directory:
        path = Path(directory) / "junit.xml"
        path.write_bytes(payload)
        return junit_records(path, root, basetemp)


def _source_proof(record: dict[str, Any]) -> None:
    revision = record["controller"]["revision"]
    if record["controller"].get("dirty") is not False or len(revision) != 40:
        raise ValueError("publication controller is not identified and clean")
    inputs = record["source_input_sha256"]
    if not inputs:
        raise ValueError("missing benchmark source inputs")
    for name, expected in inputs.items():
        payload = subprocess.check_output(["git", "show", f"{revision}:{name}"], cwd=ROOT)
        if _digest(payload) != expected:
            raise ValueError(f"benchmark source input differs from controller revision: {name}")


def _build_proof(reader: Reader, identity: dict[str, Any]) -> dict[str, Any]:
    build = identity["build"]
    manifest_path = reader.path(build["path"])
    manifest = _json(manifest_path.read_bytes())
    if (
        build["native_profile"] != "release"
        or manifest.get("source_dirty", False) is not False
        or manifest.get("dirty", False) is not False
        or len(build["source_revision"]) != 40
        or identity["prepared"]["wheel_sha256"] != build["wheel_sha256"]
        or any(
            manifest[field] != build[field]
            for field in ("source_revision", "wheel_sha256", "extension_sha256", "native_profile")
        )
    ):
        raise ValueError("prepared runtime and release build identities differ")
    wheel = reader.path(manifest["wheel_path"])
    if _digest(wheel.read_bytes()) != build["wheel_sha256"]:
        raise ValueError("release wheel bytes changed")
    installed = identity["installed"]
    if (
        installed["native_profile"] != "release"
        or installed["vfy_extension_sha256"] != build["extension_sha256"]
    ):
        raise ValueError("installed extension differs from identified release build")
    verify_installed_distribution_files("native publication", "vfy", installed, wheel)
    with zipfile.ZipFile(wheel) as archive:
        extensions = [
            _digest(archive.read(name))
            for name in archive.namelist()
            if name.endswith((".so", ".pyd", ".dylib"))
        ]
    if build["extension_sha256"] not in extensions:
        raise ValueError("recorded extension does not occur in release wheel")
    subprocess.run(
        ["git", "cat-file", "-e", f"{build['source_revision']}^{{commit}}"], cwd=ROOT, check=True
    )
    return {**build, "manifest_sha256": _digest(manifest_path.read_bytes())}


def _preparation_proof(
    identity: dict[str, Any], record: dict[str, Any], key: str, reference: Path | None
) -> dict[str, Any]:
    prepared = identity["prepared"]
    project = matrix.PROJECTS_BY_KEY[key]
    inputs = prepared["preparation_inputs"]
    lock = (
        project.dependency_lock
        or ROOT / "validation/pytest_runner/native-adoption-requirements.txt"
    )
    if (
        prepared["revision"] != project.revision
        or inputs["project"]["revision"] != project.revision
        or inputs["dependency_lock_sha256"]
        != record["source_input_sha256"][lock.relative_to(ROOT).as_posix()]
        or inputs["preparation_source_sha256"]
        != record["source_input_sha256"]["validation/pytest_runner/check_native_adoption_matrix.py"]
        or inputs["benchmark_preparation_source_sha256"]
        != record["source_input_sha256"]["validation/pytest_runner/benchmark_native_projects.py"]
    ):
        raise ValueError("prepared project pin, dependency lock, or preparation source changed")
    environment = {
        field: identity["installed"][field]
        for field in ("python", "implementation", "pytest", "pytest_files", "dependencies")
    }
    if reference is not None:
        original = _json((reference / f"xdist-{key}" / key / "identity.json").read_bytes())
        for field in ("revision", "snapshot_inputs", "setup_diff", "benchmark_inputs"):
            if prepared[field] != original["prepared"][field]:
                raise ValueError(f"prepared {field} differs from recorded original workload")
        for field, installed_value in environment.items():
            actual = installed_value
            expected = original["installed"][field]
            if field == "dependencies":
                actual = [item for item in actual if item[0] != "vfy"]
                expected = [item for item in expected if item[0] != "vfy"]
            if actual != expected:
                raise ValueError(f"installed {field} differs from recorded original environment")
    return environment


def _native_proof(
    reader: Reader, gate: dict[str, Any], command: dict[str, Any], inventory: list[str], route: str
) -> None:
    records = gate["native_execution"]
    expected = (
        {f"native.json.gw{index}" for index in range(4)}
        if route == "pytest-native-xdist-4"
        else {"native.json"}
    )
    if (
        {Path(row["path"]).name for row in records} != expected
        or len(records) != len(expected)
        or {name for name in gate["artifacts"] if name.startswith("native.json")} != expected
    ):
        raise ValueError("native preflight has missing, duplicate, or unexpected worker records")
    root = Path(command["cwd"])
    python = Path(command["argv"][0])
    basetemp = root.parent / "pytest-tmp"
    executed = []
    for row in records:
        payload = reader.artifact(gate["artifacts"][Path(row["path"]).name])
        raw = _json(payload)
        if (
            _digest(payload) != row["sha256"]
            or raw.get("engine") != "pytest-runner-native-v1"
            or row.get("engine") != raw.get("engine")
            or raw.get("collected") != len(inventory)
            or row.get("collected") != len(inventory)
        ):
            raise ValueError("native engine, collection count, or record hash changed")
        cases, session = matrix.normalize_native(raw, root, python, basetemp)
        matrix.validate_cases(cases, session)
        if row.get("cases") != len(cases):
            raise ValueError("native executed count differs from its retained metadata")
        executed.extend(matrix.normalize_nodeid(case["nodeid"], root) for case in cases)
    if sorted(executed) != sorted(inventory):
        raise ValueError("native executed test-ID multiset differs from complete stock inventory")


def _publication_method(
    record: dict[str, Any], *, memory: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    if record["sampling"] != {"mode": "publication", "warmups": 3, "measured_runs": 15}:
        raise ValueError("publication cohort requires exactly three warmups and fifteen samples")
    method = record["method"]
    if (
        method.get("timing_routes") != list(ROUTES)
        or method.get("workers") != [0, 4]
        or method.get("native_workers") != [4]
        or method.get("capture") != "no"
        or method.get("distribution") != "load"
        or method.get("resource_method") != ("linux-process-tree-smaps-rollup" if memory else None)
    ):
        raise ValueError("cohort changes declared routes or measurement method")
    if method["native_environment"] != {}:
        raise ValueError("publication changes the recorded default native route configuration")
    if len(record["projects"]) != 1:
        raise ValueError("each cohort must retain one complete project")
    project = record["projects"][0]
    key = project["project"]
    if project.get("status") != "measured" or project.get("cases") != CASES.get(key):
        raise ValueError("project did not measure its complete configured inventory")
    return project, method


def _gate_proof(
    reader: Reader, project: dict[str, Any], folder: Path, method: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    key = project["project"]
    inventory = _json((folder / "preflight/inventory.json").read_bytes())["nodeids"]
    if len(inventory) != CASES[key]:
        raise ValueError("stock inventory count changed")
    parity = _json((folder / "preflight/parity.json").read_bytes())
    if parity != project["parity"]:
        raise ValueError("retained preflight differs from result")
    commands, expected = {}, {}
    for route in ("stock-xdist-0", *ROUTES):
        gate = parity[route]
        if (
            gate.get("passed") is not True
            or gate.get("errors") != []
            or gate["cases"] != CASES[key]
        ):
            raise ValueError(f"complete preflight did not pass: {route}")
        command, payload, _ = _read_run(reader, gate["artifacts"])
        validate_command(command, route, project["selectors"])
        commands[route] = command
        expected[route] = _junit(payload, command)
        if route.startswith("pytest-native"):
            if gate.get("native_environment") != method["native_environment"]:
                raise ValueError("native preflight environment differs from declared experiment")
        elif gate.get("native_environment") != {} and (
            route != "stock-xdist-0"
            or any(
                not name.startswith(("VFY_PYTEST_RECORD_", "VFY_PYTEST_OBSERVER_"))
                for name in gate["native_environment"]
            )
        ):
            raise ValueError("stock preflight contains native experiment flags")
        if len(expected[route]) != CASES[key]:
            raise ValueError("preflight JUnit testcase count changed")
        root, python = Path(command["cwd"]), Path(command["argv"][0])
        if route == "stock-xdist-0":
            raw = _json(reader.artifact(gate["artifacts"]["execution.json"]))
            _, _, observed = matrix.normalize_outcome_record(
                "stock", raw, {"exit_status": 0}, root, python, root.parent / "pytest-tmp"
            )
            if observed != inventory:
                raise ValueError("stock observer differs from recorded ordered inventory")
        elif route.startswith("pytest-native"):
            _native_proof(reader, gate, command, inventory, route)
            if expected[route] != expected["stock-xdist-4"]:
                raise ValueError("native complete JUnit testcase multiset differs from stock")
    if not compare_phase_outcomes(
        {"junit_cases": expected["stock-xdist-0"]},
        {"junit_cases": expected["stock-xdist-4"]},
    )["equal"]:
        raise ValueError("stock distributed outcomes differ from complete serial inventory")
    return commands, expected


def _sample_proof(
    reader: Reader,
    project: dict[str, Any],
    folder: Path,
    method: dict[str, Any],
    commands: dict[str, Any],
    expected: dict[str, Any],
    *,
    memory: bool,
) -> list[dict[str, Any]]:
    parity = project["parity"]
    samples = project["timing"]["samples"]
    if _json((folder / "timing/samples.json").read_bytes())["samples"] != samples:
        raise ValueError("retained sample record differs from result")
    validate_rotation(samples, memory=memory, native_environment=method["native_environment"])
    for row in samples:
        route = row["route"]
        if row.get("native_environment") != parity[route].get("native_environment"):
            raise ValueError("sample native experiment flags differ from gated route")
        command, payload, seconds = _read_run(reader, row["artifacts"])
        if not math.isclose(seconds, row["seconds"], rel_tol=1e-9, abs_tol=1e-6):
            raise ValueError("sample wall time differs from retained process status")
        if _command_identity(command) != _command_identity(commands[route]):
            raise ValueError("sample command differs from its gated route")
        if _junit(payload, command) != expected[route]:
            raise ValueError("sample changed complete JUnit testcase multiset")
        if (
            memory
            and _json(reader.artifact(row["artifacts"]["resources.json"])) != row["resources"]
        ):
            raise ValueError("sample resource record differs from retained artifact")
        if memory and row["resources"]["child_command"] != command["argv"]:
            raise ValueError("resource collector measured a different child command")
    return samples


def _sample_summaries(
    project: dict[str, Any], samples: list[dict[str, Any]], *, memory: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    routes = {}
    for route in ROUTES:
        rows = [row for row in samples if row["phase"] == "sample" and row["route"] == route]
        wall = [row["seconds"] * 1_000 for row in rows]
        if wall != project["timing"]["summary"][route]["samples_ms"]:
            raise ValueError("summary sample vector differs from raw experimental blocks")
        routes[route] = {"wall_ms": wall, "median_ms": statistics.median(wall)}
        if memory:
            pss = [row["resources"]["peak_tree_pss_bytes"] / 1_048_576 for row in rows]
            routes[route].update(
                {"tree_pss_mib": pss, "median_tree_pss_mib": statistics.median(pss)}
            )
            routes[route]["resource_stats"] = {
                field: {
                    "median": statistics.median(row["resources"][field] for row in rows),
                    "min": min(row["resources"][field] for row in rows),
                    "max": max(row["resources"][field] for row in rows),
                }
                for field in (
                    "max_sample_gap_ms",
                    "snapshot_count",
                    "peak_process_count",
                    "tracked_process_count",
                    "total_cpu_ms",
                )
            }
    comparisons: dict[str, dict[str, object]] = {}
    for route in ROUTES[1:]:
        comparisons[route] = {
            "wall_difference_ms": paired_uncertainty(
                routes[route]["wall_ms"], routes[ROUTES[0]]["wall_ms"]
            )
        }
        if memory:
            comparisons[route]["memory_difference_mib"] = {
                name.replace("_ms", "_mib"): value
                for name, value in paired_uncertainty(
                    routes[route]["tree_pss_mib"], routes[ROUTES[0]]["tree_pss_mib"]
                ).items()
            }
    return routes, comparisons


def audit_cohort(
    path: Path, *, memory: bool, reader: Reader, reference: Path | None = None
) -> dict[str, Any]:
    record = _json(path.read_bytes())
    _source_proof(record)
    project, method = _publication_method(record, memory=memory)
    key = project["project"]
    folder = path.parent / key
    identity = _json((folder / "identity.json").read_bytes())
    build = _build_proof(reader, identity)
    environment = _preparation_proof(identity, record, key, reference)
    commands, expected = _gate_proof(reader, project, folder, method)
    samples = _sample_proof(reader, project, folder, method, commands, expected, memory=memory)
    routes, comparisons = _sample_summaries(project, samples, memory=memory)
    return {
        "project": key,
        "cases": CASES[key],
        "cohort": "memory-sampled" if memory else "timing-only",
        "result_path": str(path),
        "result_sha256": _digest(path.read_bytes()),
        "controller": record["controller"],
        "build": build,
        "machine": record["machine"],
        "cpu_max": record.get("cpu_max"),
        "method": method,
        "source_input_sha256": record["source_input_sha256"],
        "upstream_revision": identity["prepared"]["revision"],
        "environment": environment,
        "preparation_inputs": identity["prepared"]["preparation_inputs"],
        "benchmark_inputs": identity["prepared"]["benchmark_inputs"],
        "setup_diff_sha256": _digest(identity["prepared"]["setup_diff"].encode()),
        "route_commands": {route: commands[route] for route in ROUTES},
        "routes": routes,
        "comparisons": comparisons,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory", type=Path, nargs=4, required=True, metavar="RESULT")
    parser.add_argument("--rich-timing", type=Path, required=True)
    parser.add_argument(
        "--reference-root",
        type=Path,
        required=True,
        help="verified restored original benchmark-artifacts root for workload/environment parity",
    )
    parser.add_argument("--relocate", action="append", default=[], metavar="ORIGINAL=RESTORED")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    relocations = []
    for value in args.relocate:
        original, separator, restored = value.partition("=")
        if not separator or not original or not restored:
            parser.error("--relocate requires ORIGINAL=RESTORED")
        relocations.append((original.rstrip("/"), restored.rstrip("/")))
    reader = Reader(tuple(relocations))
    rows = [
        audit_cohort(path, memory=True, reader=reader, reference=args.reference_root)
        for path in args.memory
    ]
    if {row["project"] for row in rows} != set(CASES):
        raise ValueError("memory publication requires each of the four supported suites once")
    timing = audit_cohort(
        args.rich_timing, memory=False, reader=reader, reference=args.reference_root
    )
    if timing["project"] != "rich":
        raise ValueError("separate timing-only cohort must run Rich")
    rows.append(timing)
    for field in ("controller", "build", "machine", "cpu_max"):
        if any(row[field] != rows[0][field] for row in rows[1:]):
            raise ValueError(f"publication cohorts use different {field} identities")
    report = {
        "schema": "vfy.native-performance-publication.v1",
        "analysis_source_sha256": {
            "scripts/native_perf_evidence.py": _digest(Path(__file__).read_bytes()),
            "benchmarks/benchmark_harness.py": _digest(
                (ROOT / "benchmarks/benchmark_harness.py").read_bytes()
            ),
        },
        "bootstrap": {
            "resamples": 10_000,
            "seed": 0,
            "confidence": 0.95,
            "statistic": "difference-of-medians",
            "paired_block": "same rotated measured round within one cohort",
        },
        "gate_scope": (
            "complete JUnit testcase multisets and successful status in every run; "
            "stock inventory and native engine/executed IDs in preflight; "
            "complete stock phase/session-warning parity is not established"
        ),
        "series": rows,
        "blocked": {
            "black": "unsupported native pytest_plugins configuration",
            "fastapi": "unsupported required third-party plugins",
        },
    }
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")


if __name__ == "__main__":
    main()
