"""Measure ancestor-baseline loading and PR selection against a full coverage oracle."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from contextlib import chdir
from pathlib import Path

from benchmarks.benchmark_edit_loop import (
    assert_parity,
    command_full,
    command_vfy,
    create_project,
    edit_project,
    executed_tests,
    execution_environment,
    git,
    run,
)
from benchmarks.benchmark_harness import (
    add_sampling_arguments,
    benchmark_environment,
    sampling_config,
    summarize,
    timed_call,
    timed_process,
)
from benchmarks.benchmark_provenance import (
    artifact_provenance,
    build_provenance,
    require_unchanged_artifact,
)
from benchmarks.benchmark_rust_boundary import peak_rss_bytes
from vfy.native import build_profile, finalize_ci_plan, import_ci_baseline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modules", type=int, default=128)
    add_sampling_arguments(parser, warmups=3, runs=15)
    parser.add_argument("--diagnostic", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--validate-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--probe-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--probe-fingerprint", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.diagnostic or args.validate_only:
        args.mode = "diagnostic"
    if args.modules < 2:
        parser.error("--modules must be at least 2")
    return args


def ci_command(root: Path, *, export: bool = False) -> list[str]:
    command = command_vfy(root, "selected.json", all_tests=export)
    index = command.index("-q")
    options = (
        ["--export-baseline", ".vfy/baseline.json"]
        if export
        else ["--baseline", ".vfy/baseline.json", "--plan-output", ".vfy/plan.json"]
    )
    command[index:index] = options
    return command


def loading_probe(root: Path, fingerprint: str) -> dict[str, float | int | None]:
    with chdir(root):
        elapsed, data = timed_call(
            lambda: import_ci_baseline(
                ".vfy/baseline.json", fingerprint, [], ".vfy/probe-request.json"
            )
        )
        if data is None:
            raise RuntimeError("CI evidence probe unexpectedly fell back")
        collected = sorted(json.loads((root / ".vfy/plan.json").read_text())["collected"])
        selection_ms, _ = timed_call(
            lambda: finalize_ci_plan(".vfy/probe-request.json", collected, ".vfy/probe-plan.json")
        )
    return {
        "baseline_load_and_selection_ms": elapsed,
        "collection_finalization_ms": selection_ms,
        "process_peak_rss_bytes": peak_rss_bytes(),
    }


def checked_runs(
    root: Path, environment: dict[str, str], *, order: int, timed_run: bool = True
) -> tuple[float, float, float]:
    selected_log = root / "selected-tests.txt"
    full_log = root / "full-tests.txt"
    vfy_full_log = root / "vfy-full-tests.txt"
    for log in (selected_log, full_log, vfy_full_log):
        log.unlink(missing_ok=True)
    calls = {
        "selected": (ci_command(root), execution_environment(environment, selected_log)),
        "full": (
            command_full(root, "full.json"),
            {
                **execution_environment(environment, full_log),
                "COVERAGE_FILE": str(root / "full.coverage"),
            },
        ),
        "vfy_full": (
            command_vfy(root, "vfy-full.json", all_tests=True, data_file="vfy-full.coverage"),
            execution_environment(environment, vfy_full_log),
        ),
    }
    elapsed: dict[str, float] = {}
    orders = (
        ("selected", "full", "vfy_full"),
        ("full", "vfy_full", "selected"),
        ("vfy_full", "selected", "full"),
    )
    for name in orders[order % len(orders)]:
        command, child_environment = calls[name]
        if timed_run:
            elapsed[name], completed = timed_process(
                command, cwd=root, env=child_environment, timeout=300
            )
        else:
            completed = run(command, root, child_environment)
            elapsed[name] = 0.0
        if completed.returncode:
            raise RuntimeError(completed.stdout + completed.stderr)
    selected = executed_tests(selected_log)
    plan = json.loads((root / ".vfy/plan.json").read_text())
    if (
        selected != ["tests/test_module_000.py::test_module_000"]
        or sorted(plan["selection"]["selected"]) != selected
    ):
        raise RuntimeError("CI selection did not execute the expected changed-file test")
    if not set(selected) < set(executed_tests(full_log)):
        raise RuntimeError("CI selection must execute a strict subset")
    if executed_tests(vfy_full_log) != executed_tests(full_log):
        raise RuntimeError("vfy's full run must execute the complete reference suite")
    assert_parity(
        root / "selected.json", root / "full.json", root / ".coverage", root / "full.coverage", root
    )
    assert_parity(
        root / "vfy-full.json",
        root / "full.json",
        root / "vfy-full.coverage",
        root / "full.coverage",
        root,
    )
    return elapsed["selected"], elapsed["full"], elapsed["vfy_full"]


def main() -> int:
    args = parse_args()
    if args.probe_root:
        print(json.dumps(loading_probe(args.probe_root, args.probe_fingerprint)))
        return 0
    sampling = sampling_config(args)
    if build_profile() != "release":
        raise SystemExit("CI selection benchmarks require a release-mode build")
    if not args.validate_only and args.output is None:
        raise SystemExit("CI selection benchmarks require --output")
    artifact_before = artifact_provenance(strict=True) if sampling.publishable else None
    provenance = build_provenance(
        Path(__file__).resolve().parents[1],
        workload={"name": "ci-selection", "modules": args.modules},
        workers=1,
        cache="restored ancestor baseline per sample",
        packages=("pytest", "pytest-cov", "coverage"),
        publication=sampling.publishable,
    )
    samples: list[tuple[float, float, float]] = []
    probes: list[dict[str, float | int | None]] = []
    environment = benchmark_environment({"PYTHONDONTWRITEBYTECODE": "1"})
    with tempfile.TemporaryDirectory(prefix="vfy-ci-benchmark-") as temporary:
        prepared = Path(temporary) / "prepared"
        prepared.mkdir()
        create_project(prepared, args.modules)
        run(ci_command(prepared, export=True), prepared, environment)
        fingerprint = json.loads((prepared / ".vfy/baseline.json").read_text())["fingerprint"]
        total = 1 if args.validate_only else 1 + sampling.warmups + sampling.runs
        for index in range(total):
            root = Path(temporary) / f"sample-{index}"
            shutil.copytree(prepared, root)
            edit_project(root)
            git(root, "add", "src")
            git(root, "commit", "-qm", "PR edit")
            # Check parity before collecting timing samples.
            timings = checked_runs(root, environment, order=index, timed_run=index > 0)
            if index == 0 or index <= sampling.warmups:
                continue
            samples.append(timings)
            probe = run(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.benchmark_ci_selection",
                    "--probe-root",
                    str(root),
                    "--probe-fingerprint",
                    fingerprint,
                ],
                Path(__file__).resolve().parents[1],
                environment,
            )
            probes.append(json.loads(probe.stdout))
    if artifact_before is not None:
        require_unchanged_artifact(artifact_before)
    payload = {
        "schema": 1,
        "provenance": provenance,
        "modules": args.modules,
        "parity": "exact lines, arcs, contexts, and report fields against full pytest-cov",
        "selected": summarize([sample[0] for sample in samples]) if samples else None,
        "full": summarize([sample[1] for sample in samples]) if samples else None,
        "vfy_full": summarize([sample[2] for sample in samples]) if samples else None,
        "evidence_probes": probes,
        "memory_scope": "fresh evidence-loading process lifetime peak RSS; excludes pytest workers",
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
