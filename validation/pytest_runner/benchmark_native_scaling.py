"""Parity-gated full-suite no-coverage scaling, with CPU and RSS on every sample."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import statistics
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SOURCE_INPUTS = (
    "validation/pytest_runner/benchmark_native_scaling.py",
    "validation/pytest_runner/benchmark_child_resources.py",
    "validation/pytest_runner/benchmark_public_pytest.py",
    "validation/pytest_runner/benchmark_vfy_coverage.py",
    "benchmarks/benchmark_harness.py",
    "benchmarks/benchmark_provenance.py",
    "scripts/process_tree.py",
    "scripts/pytest_execution_record.py",
)
SOURCE_INPUT_SHA256 = {
    name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_INPUTS
}

from benchmarks.benchmark_harness import (  # noqa: E402
    SamplingConfig,
    add_sampling_arguments,
    paired_uncertainty,
    rotated_orders,
    sampling_config,
    summarize,
    timed_process,
)
from benchmarks.benchmark_provenance import (  # noqa: E402
    SourceState,
    hardware_details,
    require_clean,
)
from scripts.pytest_execution_record import (  # noqa: E402
    file_sha256,
    resolve_executable,
    write_atomic,
)
from validation.pytest_runner import (  # noqa: E402
    benchmark_public_pytest as public,
    benchmark_vfy_coverage as coverage,
)

Route = Literal["pytest", "vfy"]
Family = Literal["parametrized", "functions", "fixtures", "capture"]
ROUTES: tuple[Route, ...] = ("pytest", "vfy")
BODY_TRACE = "VFY_SCALING_BODY_TRACE"
SHAPE_TRACE = "VFY_SCALING_SHAPE_TRACE"
RESOURCE_COLLECTOR = Path(__file__).with_name("benchmark_child_resources.py")


@dataclass(frozen=True)
class Workload:
    """A complete generated inventory, with no explicit node selection."""

    family: Family
    cases: int

    def __post_init__(self) -> None:
        if self.cases < 1:
            raise ValueError("a scaling workload requires a positive complete case count")

    @property
    def name(self) -> str:
        return f"{self.family}-{self.cases}"

    def expected_names(self) -> list[str]:
        if self.family == "functions":
            return [f"test_case_{index:05d}" for index in range(self.cases)]
        return public.expected_case_names(self.cases)


BODY_WRITER = """
import atexit
import json
import os
from pathlib import Path

_executed = []


def _write_execution_trace():
    Path(os.environ["VFY_SCALING_BODY_TRACE"]).write_text(
        json.dumps(_executed), encoding="utf-8"
    )
    shape_destination = os.environ.get("VFY_SCALING_SHAPE_TRACE")
    if shape_destination:
        import hashlib
        from types import FunctionType

        functions = {
            name: value for name, value in globals().items()
            if name == "test_case" or name.startswith("test_case_")
        }
        if not all(isinstance(value, FunctionType) for value in functions.values()):
            raise TypeError("generated test binding is not a Python function")
        names = sorted(functions)
        shape = {
            "function_bindings": len(functions),
            "distinct_code_objects": len({id(value.__code__) for value in functions.values()}),
            "names_sha256": hashlib.sha256(
                json.dumps(names, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        Path(shape_destination).write_text(json.dumps(shape), encoding="utf-8")


atexit.register(_write_execution_trace)
"""


def fixture_inputs(root: Path) -> dict[str, str]:
    """Bind generated sources while allowing their warmed bytecode caches."""
    return {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".ini"}
    }


def create_project(root: Path, workload: Workload) -> dict[str, str]:
    """Reuse the public generator; ordinary functions have distinct definitions."""
    if workload.family in {"fixtures", "capture"}:
        coverage.create_project(root, workload.cases, workload=workload.family)
    else:
        public.create_project(root, workload.cases)
        path = root / "test_workload.py"
        if workload.family == "parametrized":
            source = path.read_text(encoding="utf-8").replace(
                "    assert value >= 0\n", "    assert value >= 0\n    _executed.append(value)\n"
            )
            source += BODY_WRITER
        else:
            source = "from __future__ import annotations\n\nimport pytest\n" + BODY_WRITER
            source += "\n".join(
                f"\ndef test_case_{index:05d}():\n"
                f"    value = {index}\n"
                "    assert value >= 0\n"
                "    _executed.append(value)\n"
                for index in range(workload.cases)
            )
        path.write_text(source, encoding="utf-8")
    return fixture_inputs(root)


def case_digest(records: list[dict[str, str]]) -> str:
    return hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def check_execution_trace(workload: Workload, evidence: Path) -> dict[str, Any]:
    """Require actual body execution and complete fixture teardown."""
    if workload.family in {"fixtures", "capture"}:
        expected = coverage.case_ids(workload.cases)
        return {
            "execution_trace": coverage.check_ordered_execution(evidence / "order.txt", expected),
            "lifecycle_trace": coverage.check_lifecycle(
                evidence / "lifecycle.txt", workload.family, expected
            ),
        }
    path = evidence / "body.json"
    if json.loads(path.read_text(encoding="utf-8")) != list(range(workload.cases)):
        raise RuntimeError("actual test-body trace is missing, reordered or incomplete")
    return {"execution_trace": {"case_count": workload.cases, "sha256": file_sha256(path)}}


def check_run(
    workload: Workload,
    evidence: Path,
    junit: Path,
    reference_digest: str | None,
) -> dict[str, Any]:
    """Reject missing/reordered/failed nodes and missing actual body/lifecycle events."""
    outcomes = public.junit_outcomes(
        junit, workload.cases, expected_names=workload.expected_names()
    )
    if any(record["outcome"] != "passed" for record in outcomes):
        raise RuntimeError("scaling command did not pass every requested case")
    digest = case_digest(outcomes)
    if reference_digest is not None and digest != reference_digest:
        raise RuntimeError("ordered JUnit outcomes differ from the stock preflight")
    return {
        "case_count": len(outcomes),
        "ordered_outcomes_sha256": digest,
        **check_execution_trace(workload, evidence),
    }


def reset_runtime(root: Path) -> None:
    """Clear replay/history state outside timing, retaining warmed source bytecode."""
    for name in (".vfy", ".pytest_cache"):
        path = root / name
        if path.exists():
            shutil.rmtree(path)


def run_environment(output: Path, evidence: Path, workload: Workload) -> dict[str, str]:
    temporary = evidence / "tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    env = public.controlled_environment(output / "home", temporary)
    if workload.family in {"fixtures", "capture"}:
        env[coverage.ORDER_PATH] = str(evidence / "order.txt")
        env[coverage.LIFECYCLE_PATH] = str(evidence / "lifecycle.txt")
    else:
        env[BODY_TRACE] = str(evidence / "body.json")
    return env


def raw_artifacts(evidence: Path) -> list[dict[str, str | int]]:
    return [
        {"path": str(path), "sha256": file_sha256(path), "bytes": path.stat().st_size}
        for path in sorted(evidence.rglob("*"))
        if path.is_file()
    ]


def preflight(
    workload: Workload,
    route: Route,
    python: Path,
    root: Path,
    output: Path,
    reference_digest: str | None,
    timeout: float,
) -> dict[str, Any]:
    """Reuse the committed public preflight, including its exact status/ordered JUnit gate."""
    evidence = output / workload.name / f"preflight-{route}"
    env = run_environment(output, evidence, workload)
    reset_runtime(root)
    if workload.family in {"parametrized", "functions"}:
        env[SHAPE_TRACE] = str(evidence / "shape.json")
    gate = public.run_preflight(
        route,
        python,
        root,
        evidence / "public",
        workload.cases,
        env,
        timeout,
        expected_names=workload.expected_names(),
    )
    digest = gate["outcomes_sha256"]
    if reference_digest is not None and digest != reference_digest:
        raise RuntimeError("ordered JUnit outcomes differ from the stock preflight")
    proof = {
        "case_count": len(gate["outcomes"]),
        "ordered_outcomes_sha256": digest,
        **check_execution_trace(workload, evidence),
    }
    if workload.family in {"parametrized", "functions"}:
        shape = json.loads((evidence / "shape.json").read_text(encoding="utf-8"))
        names = workload.expected_names() if workload.family == "functions" else ["test_case"]
        names_digest = hashlib.sha256(json.dumps(names, separators=(",", ":")).encode()).hexdigest()
        if (
            shape["function_bindings"] != len(names)
            or shape["distinct_code_objects"] != len(names)
            or shape["names_sha256"] != names_digest
        ):
            raise RuntimeError("generated test functions/code objects are aliased or incomplete")
        proof["function_shape"] = shape
    return {
        "route": route,
        "status": "passed",
        "command": gate["command"],
        "exit_status": gate["exit_status"],
        "environment": env,
        "validation": proof,
        "raw_artifacts": raw_artifacts(evidence),
    }


def sample(
    workload: Workload,
    route: Route,
    python: Path,
    root: Path,
    output: Path,
    reference_digest: str,
    phase: str,
    index: int,
    timeout: float,
    *,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Use one fresh resource collector per child, then check parity outside its interval."""
    evidence = output / workload.name / f"{phase}-{index:02d}-{route}"
    env = run_environment(output, evidence, workload)
    reset_runtime(root)
    junit = evidence / "junit.xml"
    child_command = public.command(route, python, root, junit=junit)
    resource_path = evidence / "resources.json"
    argv = [
        str(python),
        "-P",
        str(RESOURCE_COLLECTOR),
        "--record",
        str(resource_path),
        "--stdout",
        str(evidence / "stdout.txt"),
        "--stderr",
        str(evidence / "stderr.txt"),
        "--",
        *child_command,
    ]
    record: dict[str, Any] = {
        "route": route,
        "phase": phase,
        "index": index,
        "status": "running",
        "command": child_command,
        "collector_command": argv,
        "cwd": str(root),
        "environment": env,
        "timeout_seconds": timeout,
    }
    if records is not None:
        records.append(record)
    write_atomic(evidence / "command.json", record)
    try:
        outer_wall_ms, completed = timed_process(argv, cwd=root, env=env, timeout=timeout)
        record["outer_wrapper_wall_ms"] = outer_wall_ms
        record["exit_status"] = completed.returncode
        (evidence / "collector-stdout.txt").write_text(completed.stdout, encoding="utf-8")
        (evidence / "collector-stderr.txt").write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            raise RuntimeError(f"{route} {phase} exited {completed.returncode}; see {evidence}")
        resources = json.loads(resource_path.read_text(encoding="utf-8"))
        if resources["child_exit_status"] != completed.returncode:
            raise RuntimeError("collector and subprocess exit statuses disagree")
        if resources["child_command"] != child_command:
            raise RuntimeError("resource collector executed a different public command")
        if resources["collector_sha256"] != file_sha256(RESOURCE_COLLECTOR):
            raise RuntimeError("resource collector source changed")
        for name in (
            "child_wall_ms",
            "user_cpu_ms",
            "system_cpu_ms",
            "total_cpu_ms",
            "peak_rss_bytes",
        ):
            if not math.isfinite(resources[name]) or resources[name] < 0:
                raise RuntimeError(f"invalid resource measurement: {name}")
        record["resources"] = resources
        record["validation"] = check_run(workload, evidence, junit, reference_digest)
        record["status"] = "passed"
        return record
    except BaseException as error:
        record["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        record["error"] = {"type": type(error).__name__, "message": str(error)[:2_000]}
        (evidence / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        write_atomic(evidence / "command.json", record)
        record["raw_artifacts"] = raw_artifacts(evidence)


def summarize_stratum(row: dict[str, Any], sampling: SamplingConfig) -> dict[str, Any]:
    """Summarize every completed sample; partial strata cannot receive a summary."""
    measured = [run for run in row["runs"] if run["phase"] == "measured"]
    warmups = [run for run in row["runs"] if run["phase"] == "warmup"]
    roles: dict[str, Any] = {}
    for route in ROUTES:
        selected = [run for run in measured if run["route"] == route]
        warm = [run for run in warmups if run["route"] == route]
        if len(selected) != sampling.runs or len(warm) != sampling.warmups:
            raise RuntimeError("scaling stratum does not contain the complete sampling protocol")
        if any(run["status"] != "passed" for run in (*selected, *warm)):
            raise RuntimeError("scaling stratum contains failed or interrupted samples")
        rss = [run["resources"]["peak_rss_bytes"] for run in selected]
        roles[route] = {
            "child_wall": summarize([run["resources"]["child_wall_ms"] for run in selected]),
            "total_cpu": summarize([run["resources"]["total_cpu_ms"] for run in selected]),
            "user_cpu": summarize([run["resources"]["user_cpu_ms"] for run in selected]),
            "system_cpu": summarize([run["resources"]["system_cpu_ms"] for run in selected]),
            "outer_wrapper_wall": summarize([run["outer_wrapper_wall_ms"] for run in selected]),
            "peak_rss": {"median_bytes": statistics.median(rss), "samples_bytes": rss},
        }
    stock = roles["pytest"]
    native = roles["vfy"]
    return {
        "roles": roles,
        "stock_over_native_child_wall": stock["child_wall"]["median_ms"]
        / native["child_wall"]["median_ms"],
        "stock_over_native_total_cpu": stock["total_cpu"]["median_ms"]
        / native["total_cpu"]["median_ms"],
        "stock_over_native_peak_rss": stock["peak_rss"]["median_bytes"]
        / native["peak_rss"]["median_bytes"],
        "child_wall_uncertainty": paired_uncertainty(
            stock["child_wall"]["samples_ms"], native["child_wall"]["samples_ms"]
        ),
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--build-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--counts",
        type=int,
        nargs="+",
        choices=(1_000, 10_000, 50_000),
        default=[1_000, 10_000, 50_000],
    )
    parser.add_argument(
        "--controls", choices=("fixtures", "capture"), nargs="*", default=["fixtures", "capture"]
    )
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--validate-only", action="store_true")
    add_sampling_arguments(parser)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("this CPU/RSS protocol requires Linux")
    if len(set(args.counts)) != len(args.counts) or len(set(args.controls)) != len(args.controls):
        parser.error("counts and controls must be unique")
    return args


def gate_workloads(
    workloads: list[Workload],
    python: Path,
    output: Path,
    result: dict[str, Any],
    timeout: float,
    checkpoint: Callable[[], None],
) -> None:
    """Validate every complete inventory before the first warmup."""
    for workload in workloads:
        root = output / "projects" / workload.name
        row: dict[str, Any] = {
            "name": workload.name,
            "family": workload.family,
            "cases": workload.cases,
            "project": str(root),
            "status": "preflight",
            "preflight": {},
            "runs": [],
        }
        result["strata"].append(row)
        result["active_stratum"] = workload.name
        row["fixture_inputs"] = create_project(root, workload)
        reference = None
        for route in ROUTES:
            row["active_route"] = route
            row["preflight"][route] = {
                "status": "running",
                "evidence": str(output / workload.name / f"preflight-{route}"),
            }
            checkpoint()
            gate = preflight(workload, route, python, root, output, reference, timeout)
            row["preflight"][route] = gate
            reference = gate["validation"]["ordered_outcomes_sha256"]
            checkpoint()
        row.pop("active_route", None)
        row["status"] = "validated"
        checkpoint()


def measure_workloads(
    workloads: list[Workload],
    python: Path,
    output: Path,
    result: dict[str, Any],
    timeout: float,
    sampling: SamplingConfig,
    checkpoint: Callable[[], None],
) -> None:
    """Retain complete rotated pairs and summarize only admitted samples."""
    for workload, row in zip(workloads, result["strata"], strict=True):
        result["active_stratum"] = workload.name
        root = Path(row["project"])
        reference = row["preflight"]["pytest"]["validation"]["ordered_outcomes_sha256"]
        for phase, count in (("warmup", sampling.warmups), ("measured", sampling.runs)):
            row["status"] = phase
            checkpoint()
            for index, order in enumerate(rotated_orders(ROUTES, count)):
                for route in order:
                    row["active_route"] = route
                    sample(
                        workload,
                        cast(Route, route),
                        python,
                        root,
                        output,
                        reference,
                        phase,
                        index,
                        timeout,
                        records=row["runs"],
                    )
                    checkpoint()
        if fixture_inputs(root) != row["fixture_inputs"]:
            raise RuntimeError("generated sources changed during scaling samples")
        row.pop("active_route", None)
        row["summary"] = summarize_stratum(row, sampling)
        row["status"] = "complete"
        checkpoint()
        sys.stdout.write(json.dumps({"name": workload.name, "status": "complete"}) + "\n")
        sys.stdout.flush()


def record_failure(result: dict[str, Any], error: BaseException, output: Path) -> int:
    exit_status = 130 if isinstance(error, KeyboardInterrupt) else 1
    result["status"] = "interrupted" if exit_status == 130 else "failed"
    result["error"] = {"type": type(error).__name__, "message": str(error)[:2_000]}
    row = next(
        (row for row in result["strata"] if row["name"] == result.get("active_stratum")), None
    )
    if row is not None:
        row["failed_phase"] = row["status"]
        row["status"] = result["status"]
        if row["failed_phase"] == "preflight" and "active_route" in row:
            gate = row["preflight"][row["active_route"]]
            gate["status"] = result["status"]
            gate["error"] = result["error"]
            gate["raw_artifacts"] = raw_artifacts(Path(gate["evidence"]))
    (output / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
    return exit_status


def verify_after(
    result: dict[str, Any],
    source: SourceState,
    candidate: public.CandidateIdentity,
    python: Path,
    wheel: Path,
    manifest_path: Path,
    build_revision: str,
) -> None:
    """Bind every completed sample to unchanged source and installed release bytes."""
    source_after = require_clean(ROOT)
    after = public.verify_candidate(python, wheel, manifest_path, build_revision)
    result["controller_source_after"] = source_after.as_dict()
    result["candidate_after"] = after
    if source_after != source or after != candidate:
        raise RuntimeError("controller source or installed release artifact changed")
    if any(file_sha256(ROOT / name) != digest for name, digest in SOURCE_INPUT_SHA256.items()):
        raise RuntimeError("harness inputs changed during scaling measurements")
    for row in result["strata"]:
        if (
            "fixture_inputs" in row
            and fixture_inputs(Path(row["project"])) != row["fixture_inputs"]
        ):
            raise RuntimeError("generated workload source changed")
    result["identity_valid"] = True


def main() -> int:
    args = arguments()
    sampling = sampling_config(args)
    source = require_clean(ROOT)
    python = resolve_executable(args.python)
    wheel, manifest_path, output = (
        args.wheel.resolve(),
        args.build_manifest.resolve(),
        args.output.resolve(),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    build_revision = manifest.get("source_revision")
    if not isinstance(build_revision, str) or not build_revision:
        raise RuntimeError("release build manifest has no source revision")
    candidate = public.verify_candidate(python, wheel, manifest_path, build_revision)
    output.mkdir(parents=True, exist_ok=False)
    (output / "home").mkdir()
    result: dict[str, Any] = {
        "schema_version": 1,
        "benchmark": "native-pytest-full-scaling-no-cov",
        "status": "running",
        "command": [sys.executable, *sys.argv],
        "controller_source": source.as_dict(),
        "source_input_sha256": SOURCE_INPUT_SHA256,
        "candidate_before": candidate,
        "artifact_build_revision": build_revision,
        "machine": hardware_details(),
        "methodology": {
            **sampling.as_dict(),
            "validate_only": args.validate_only,
            "workers": 1,
            "coverage": False,
            "complete_inventories": True,
            "timeout_seconds": args.timeout,
            "scope": "complete parameter families and distinct functions; 1000-case controls",
            "wall": "child spawn, capture/drain and reap; unprofiled; collector startup excluded",
            "outer_wall": "wrapper subprocess spawn, capture/drain, reap retained separately",
            "cpu": "same child RUSAGE_CHILDREN; collector CPU excluded",
            "rss": "largest process RSS in bytes, not summed concurrent RSS",
            "observation": "common JUnit and body/lifecycle traces persisted in child shutdown",
            "bytecode": "warm bytecode; history and temporary state reset outside intervals",
            "environment": (
                "common no-autoload/cacheprovider protocol; other public defaults preserved"
            ),
        },
        "strata": [],
        "identity_valid": False,
        "publishable": False,
    }

    def checkpoint() -> None:
        write_atomic(output / "result.json", result)

    checkpoint()
    exit_status = 0
    try:
        workloads = [
            Workload(family, count)
            for count in args.counts
            for family in (cast(Family, "parametrized"), cast(Family, "functions"))
        ]
        workloads.extend(Workload(cast(Family, family), 1_000) for family in args.controls)
        gate_workloads(workloads, python, output, result, args.timeout, checkpoint)
        if not args.validate_only:
            measure_workloads(workloads, python, output, result, args.timeout, sampling, checkpoint)
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - retain partial run evidence.
        exit_status = record_failure(result, error, output)
    finally:
        try:
            verify_after(result, source, candidate, python, wheel, manifest_path, build_revision)
        except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - invalidate drifted evidence.
            result["identity_error"] = {"type": type(error).__name__, "message": str(error)[:2_000]}
            exit_status = 1 if exit_status != 130 else exit_status
            for row in result["strata"]:
                row.pop("summary", None)
                if row["status"] == "complete":
                    row["status"] = "invalid-provenance"
        if result["status"] == "running":
            result["status"] = "passed" if exit_status == 0 else "failed"
        result.pop("active_stratum", None)
        result["exit_status"] = exit_status
        result["publishable"] = (
            sampling.publishable
            and not args.validate_only
            and exit_status == 0
            and result["identity_valid"]
        )
        checkpoint()
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
