#!/usr/bin/env python3
"""Run and retain the parity-gated Rust evidence benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from benchmarks.benchmark_harness import (
    BenchmarkMode,
    benchmark_environment,
    run_checked,
    summarize,
)
from benchmarks.benchmark_provenance import (
    artifact_provenance,
    build_provenance,
    hardware_details,
    require_clean,
    require_revision,
    require_unchanged_artifact,
)

ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--comparison-revision", required=True)
    parser.add_argument("--warm-up-seconds", type=float, default=3.0)
    parser.add_argument("--measurement-seconds", type=float, default=5.0)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument(
        "--mode",
        choices=tuple(item.value for item in BenchmarkMode),
        default=BenchmarkMode.PUBLICATION.value,
    )
    parser.add_argument("--diagnostic", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.diagnostic:
        args.mode = BenchmarkMode.DIAGNOSTIC.value
    return args


def run(root: Path, *command: str) -> str:
    return run_checked(command, cwd=root).stdout.strip()


def load_target(root: Path, relative: Path) -> dict[str, object]:
    """Read one Criterion target and expose the shared raw timing summary."""

    directory = root / "target" / "criterion" / relative
    raw_criterion = {
        filename: json.loads((directory / filename).read_text(encoding="utf-8"))
        for filename in ("benchmark.json", "estimates.json", "sample.json")
    }
    estimates = raw_criterion["estimates.json"]
    sample = raw_criterion["sample.json"]
    per_iteration_ns = [
        elapsed / iterations
        for elapsed, iterations in zip(sample["times"], sample["iters"], strict=True)
    ]
    samples_ms = [float(value) / 1_000_000 for value in per_iteration_ns]
    return {
        "mean": estimates["mean"],
        "median": estimates["median"],
        "sampling_mode": sample["sampling_mode"],
        "samples_ns": per_iteration_ns,
        "timing": summarize(samples_ms),
        "criterion_raw": raw_criterion,
    }


def discover_targets(root: Path) -> dict[str, Path]:
    """Find the Criterion targets produced by this bench invocation."""

    criterion_root = root / "target" / "criterion"
    result: dict[str, Path] = {}
    for estimates in sorted(criterion_root.glob("**/estimates.json")):
        relative = estimates.parent.relative_to(criterion_root)
        if (estimates.parent / "sample.json").is_file():
            result[str(relative)] = relative
    if not result:
        raise RuntimeError(f"Criterion produced no retained targets in {criterion_root}")
    return result


def bench_executable_record(root: Path, cargo_output: str) -> dict[str, object]:
    """Hash the evidence bench executable named by Cargo's artifact event."""

    artifacts: list[dict[str, object]] = []
    for line in cargo_output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        target = event.get("target")
        if not isinstance(target, dict):
            continue
        kind = target.get("kind")
        if (
            event.get("reason") == "compiler-artifact"
            and target.get("name") == "evidence"
            and isinstance(kind, list)
            and "bench" in kind
            and event.get("executable")
        ):
            artifacts.append(event)
    if len(artifacts) != 1:
        raise RuntimeError(
            f"expected one Cargo evidence bench executable artifact, found {len(artifacts)}"
        )

    artifact = artifacts[0]
    target = artifact.get("target")
    if not isinstance(target, dict):
        raise RuntimeError("Cargo evidence bench artifact has no target metadata")
    target_name = target.get("name")
    target_kind = target.get("kind")
    if not isinstance(target_name, str) or not isinstance(target_kind, list):
        raise RuntimeError("Cargo evidence bench artifact has invalid target metadata")

    executable = Path(str(artifact["executable"]))
    if not executable.is_absolute():
        executable = root / executable
    try:
        contents = executable.read_bytes()
    except OSError as error:
        raise RuntimeError(f"cannot read evidence bench executable {executable}") from error
    try:
        recorded_path = str(executable.relative_to(root))
    except ValueError:
        recorded_path = str(executable)
    return {
        "target": target_name,
        "kind": target_kind,
        "path": recorded_path,
        "sha256": hashlib.sha256(contents).hexdigest(),
    }


def run_criterion(
    root: Path, command: tuple[str, ...]
) -> tuple[dict[str, Path], dict[str, object]]:
    """Run Criterion and retain its targets plus the actual bench executable hash."""

    shutil.rmtree(root / "target" / "criterion", ignore_errors=True)
    target_dir = (root / "target").resolve()
    completed = run_checked(
        command, cwd=root, env=benchmark_environment({"CARGO_TARGET_DIR": str(target_dir)})
    )
    executable = bench_executable_record(root, completed.stdout)
    executable["cargo_target_dir"] = str(target_dir)
    return discover_targets(root), executable


def median_estimate(target: object) -> float:
    if not isinstance(target, dict):
        raise RuntimeError("Criterion target is not a mapping")
    median = target.get("median")
    if not isinstance(median, dict) or not isinstance(median.get("point_estimate"), (int, float)):
        raise RuntimeError("Criterion target has no median point estimate")
    return float(median["point_estimate"])


def compare_target_medians(candidate: object, baseline: object) -> dict[str, float]:
    """Compare the median estimates from two loaded Criterion targets."""

    candidate_median = median_estimate(candidate)
    baseline_median = median_estimate(baseline)
    return {
        "candidate_median_ns": candidate_median,
        "baseline_median_ns": baseline_median,
        "baseline_over_candidate": baseline_median / candidate_median,
    }


def main() -> int:
    args = parse_args()
    mode = BenchmarkMode(args.mode)
    artifact_before = (
        artifact_provenance(strict=True) if mode is BenchmarkMode.PUBLICATION else None
    )
    if mode is BenchmarkMode.PUBLICATION and (args.samples < 15 or args.warm_up_seconds < 3):
        raise ValueError("publication requires three warm-up seconds and fifteen samples")
    starting = require_clean(ROOT) if mode is BenchmarkMode.PUBLICATION else None
    revision = run(ROOT, "git", "rev-parse", "HEAD")
    comparison_revision = run(
        ROOT, "git", "rev-parse", "--verify", f"{args.comparison_revision}^{{commit}}"
    )
    command = (
        "cargo",
        "bench",
        "--locked",
        "--message-format=json",
        "-p",
        "vfy-core",
        "--bench",
        "evidence",
        "--",
        "--warm-up-time",
        str(args.warm_up_seconds),
        "--measurement-time",
        str(args.measurement_seconds),
        "--sample-size",
        str(args.samples),
        "--noplot",
    )
    candidate_targets, candidate_executable = run_criterion(ROOT, command)
    require_revision(ROOT, revision)
    candidate_results = {
        name: load_target(ROOT, relative) for name, relative in candidate_targets.items()
    }

    baseline_results: dict[str, dict[str, object]] = {}
    baseline_executable: dict[str, object]
    with tempfile.TemporaryDirectory(prefix="vfy-evidence-base-") as temporary:
        baseline_root = Path(temporary) / "base"
        run_checked(
            ["git", "worktree", "add", "--detach", str(baseline_root), comparison_revision],
            cwd=ROOT,
        )
        try:
            baseline_targets, baseline_executable = run_criterion(baseline_root, command)
            baseline_results = {
                name: load_target(baseline_root, relative)
                for name, relative in baseline_targets.items()
            }
        finally:
            run_checked(["git", "worktree", "remove", "--force", str(baseline_root)], cwd=ROOT)

    metadata = json.loads(run(ROOT, "cargo", "metadata", "--locked", "--format-version", "1"))
    criterion_version = next(
        package["version"] for package in metadata["packages"] if package["name"] == "criterion"
    )
    comparisons: dict[str, object] = {}
    for name in sorted(candidate_results):
        candidate_target = candidate_results[name]
        baseline_target = baseline_results.get(name)
        if baseline_target is None:
            comparisons[name] = {"status": "new", "baseline_median_ns": None}
            continue
        comparisons[name] = {
            "status": "compared",
            **compare_target_medians(candidate_target, baseline_target),
        }
    if artifact_before is not None:
        require_unchanged_artifact(artifact_before)
    provenance = build_provenance(
        ROOT,
        workload={"comparison_revision": comparison_revision},
        workers=None,
        cache="isolated Criterion target directories",
        packages=(),
        publication=mode is BenchmarkMode.PUBLICATION,
    )
    provenance["comparison_revision"] = comparison_revision
    provenance["candidate_revision"] = revision
    if starting is not None:
        provenance["starting_source"] = starting.as_dict()
    result: dict[str, object] = {
        "benchmark": "evidence",
        "methodology": {
            "command": list(command),
            "mode": mode.value,
            "warm_up_seconds": args.warm_up_seconds,
            "measurement_seconds": args.measurement_seconds,
            "samples": args.samples,
            "validation": "compact records are asserted equal before Criterion timing",
            "comparison_implementation": (
                "Criterion targets are measured independently in detached exact-revision worktrees"
            ),
        },
        "provenance": provenance,
        "environment": {
            **hardware_details(),
            "rustc": run(ROOT, "rustc", "--version", "--verbose"),
            "criterion": criterion_version,
            "cargo_profile": "bench",
        },
        "results": candidate_results,
        "baseline_results": baseline_results,
        "bench_executables": {
            "candidate": candidate_executable,
            "baseline": baseline_executable,
        },
        "comparison": {
            "base_revision": comparison_revision,
            "candidate_revision": revision,
            "targets": comparisons,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
