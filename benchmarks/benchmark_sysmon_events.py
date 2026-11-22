#!/usr/bin/env python3
"""Parity-gated diagnostic for repeated ``sys.monitoring`` branch events."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import coverage

import vfy
from benchmarks.benchmark_harness import (
    add_sampling_arguments,
    rotated_orders,
    sampling_config,
    summarize,
)
from benchmarks.benchmark_provenance import (
    artifact_provenance,
    build_provenance,
    hardware_details,
    require_clean,
    require_revision,
    require_unchanged_artifact,
    source_state,
)
from vfy import Coverage
from vfy.native import build_profile, coverage_uses_sysmon_branch_arcs, parser_backend

SOURCE = """\
def run(count):
    total = 0
    for value in range(count):
        if value % 2:
            total += value
        else:
            total -= value
    return total


run({iterations})
"""

SHORT_CALLS = """\
def choose(value):
    if value % 2:
        return value
    return -value


for value in range({iterations}):
    choose(value)
"""

CONTEXTS = """\
def choose(value):
    if value % 2:
        return value
    return -value
"""

type CollectionSignature = tuple[
    tuple[str, ...],
    bool,
    tuple[str, ...],
    tuple[int, ...],
    tuple[tuple[int, int], ...],
    tuple[tuple[str, tuple[int, ...], tuple[tuple[int, int], ...]], ...],
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=100_000)
    parser.add_argument("--contexts", type=int, default=1_000)
    parser.add_argument(
        "--workload",
        choices=("all", "repeated-branches", "short-calls", "frequent-contexts"),
        default="all",
    )
    add_sampling_arguments(parser)
    parser.add_argument("--diagnostic", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--validate-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.diagnostic:
        args.mode = "diagnostic"
    return args


def collect(
    collector_type: type[Coverage | coverage.Coverage] | None,
    source: Path,
    workload: str,
    context_count: int,
    coverage_core: str | None = None,
) -> tuple[float, CollectionSignature | None]:
    # Compile a fresh code object for every sample.  sys.monitoring retains
    # disabled locations on a code object after a collector stops; a fresh
    # object models a new test process without requiring a global restart.
    code = compile(source.read_text(encoding="utf-8"), str(source), "exec")
    previous_core = os.environ.get("COVERAGE_CORE")
    if coverage_core is not None:
        os.environ["COVERAGE_CORE"] = coverage_core
    collector = None
    if collector_type is not None:
        collector = collector_type(
            branch=True,
            source=[str(source.parent)],
            data_file=None,
            config_file=False,
        )
    started = time.perf_counter_ns()
    namespace: dict[str, object] = {}
    if collector is None:
        exec(code, namespace)
    else:
        collector.start()
        exec(code, namespace)
        if workload == "frequent-contexts":
            choose = cast(Callable[[int], object], namespace["choose"])
            for index in range(context_count):
                collector.switch_context(f"test_{index:05d}|run")
                choose(index)
        collector.stop()
    if collector is None and workload == "frequent-contexts":
        choose = cast(Callable[[int], object], namespace["choose"])
        for index in range(context_count):
            choose(index)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    if coverage_core is not None:
        if previous_core is None:
            os.environ.pop("COVERAGE_CORE", None)
        else:
            os.environ["COVERAGE_CORE"] = previous_core
    if collector is None:
        return elapsed, None
    data = collector.get_data()
    files = tuple(sorted(data.measured_files()))
    if len(files) != 1:
        raise RuntimeError(f"expected one measured file, received {files!r}")
    measured_path = files[0]
    lines = tuple(sorted(int(line) for line in (data.lines(measured_path) or [])))
    arcs = tuple(sorted((int(start), int(end)) for start, end in (data.arcs(measured_path) or [])))
    contexts = tuple(sorted(data.measured_contexts()))
    context_evidence = []
    for context in contexts:
        data.set_query_contexts([f"^{re.escape(context)}$"])
        context_evidence.append(
            (
                context,
                tuple(sorted(int(line) for line in (data.lines(measured_path) or []))),
                tuple(
                    sorted(
                        (int(start), int(end)) for start, end in (data.arcs(measured_path) or [])
                    )
                ),
            )
        )
    data.set_query_contexts(None)
    return elapsed, (
        files,
        data.has_arcs(),
        contexts,
        lines,
        arcs,
        tuple(context_evidence),
    )


def expected_context_signature(path: str, context_count: int) -> CollectionSignature:
    contexts = ("", *(f"test_{index:05d}|run" for index in range(context_count)))
    module_arcs = ((1, -1), (1, 1))
    true_arcs = ((2, 2), (2, 3), (3, -1), (3, 3))
    false_arcs = ((2, 2), (2, 4), (4, -1), (4, 4))
    context_evidence = [("", (1,), module_arcs)]
    for index, context in enumerate(contexts[1:]):
        context_evidence.append(
            (context, (2, 3), true_arcs) if index % 2 else (context, (2, 4), false_arcs)
        )
    return (
        (path,),
        True,
        contexts,
        (1, 2, 3, 4),
        tuple(sorted(set(module_arcs + true_arcs + false_arcs))),
        tuple(context_evidence),
    )


def normalize_direct_context_signature(signature: CollectionSignature) -> CollectionSignature:
    normalized_contexts = []
    for context, lines, arcs in signature[5]:
        normalized_arcs = {(start, end) for start, end in arcs if start != end}
        if lines:
            normalized_arcs.add((-1, min(lines)))
        normalized_contexts.append((context, lines, tuple(sorted(normalized_arcs))))
    arcs = tuple(
        sorted(
            {arc for _context, _lines, context_arcs in normalized_contexts for arc in context_arcs}
        )
    )
    return (*signature[:4], arcs, tuple(normalized_contexts))


def validate_first_results(
    workload: str,
    results: dict[str, CollectionSignature | None],
    context_count: int,
) -> None:
    vfy_result = results["vfy"]
    if vfy_result is None:
        raise RuntimeError("vfy did not return coverage evidence")
    comparable_vfy = vfy_result
    if workload == "frequent-contexts":
        expected = expected_context_signature(vfy_result[0][0], context_count)
        if vfy_result != expected:
            raise RuntimeError("vfy context evidence differs from the independent oracle")
        comparable_vfy = normalize_direct_context_signature(vfy_result)
    if results["coverage.py"] != comparable_vfy:
        raise RuntimeError(f"Coverage.py and vfy collected different {workload} evidence")


def main() -> int:
    args = parse_args()
    sampling = sampling_config(args)
    artifact_before = artifact_provenance(strict=True) if sampling.publishable else None
    if (
        sys.implementation.name != "cpython"
        or not coverage_uses_sysmon_branch_arcs()
        or not hasattr(sys.monitoring.events, "BRANCH_LEFT")
        or not hasattr(sys.monitoring.events, "BRANCH_RIGHT")
    ):
        raise RuntimeError("this diagnostic requires CPython 3.14+ branch monitoring events")
    repository = Path(__file__).resolve().parents[1]
    starting_state = require_clean(repository) if sampling.publishable else source_state(repository)
    revision = starting_state.revision

    with tempfile.TemporaryDirectory(prefix="vfy-sysmon-events-") as temporary:
        root = Path(temporary).resolve()
        workloads = (
            ("repeated-branches", SOURCE.format(iterations=args.iterations)),
            ("short-calls", SHORT_CALLS.format(iterations=args.iterations)),
            ("frequent-contexts", CONTEXTS),
        )
        if args.workload != "all":
            workloads = tuple(item for item in workloads if item[0] == args.workload)
        workload_results: dict[str, object] = {}
        for workload, source_text in workloads:
            commands = (
                (
                    "coverage.py",
                    coverage.Coverage,
                    "ctrace" if workload == "frequent-contexts" else "sysmon",
                ),
                ("vfy", Coverage, None),
                ("plain python", None, None),
            )
            workload_root = root / workload
            workload_root.mkdir()
            source = workload_root / "subject.py"
            source.write_text(source_text, encoding="utf-8")
            first_results = {
                name: collect(collector_type, source, workload, args.contexts, core)[1]
                for name, collector_type, core in commands[:2]
            }
            validate_first_results(workload, first_results, args.contexts)
            if args.validate_only:
                workload_results[workload] = {
                    "validation_only": True,
                    "reference_tools": [name for name, _collector_type, _core in commands[:2]],
                }
                continue
            command_by_name = {
                name: (collector_type, core) for name, collector_type, core in commands
            }
            for order in rotated_orders(tuple(command_by_name), sampling.warmups):
                for name in order:
                    collector_type, core = command_by_name[name]
                    collect(collector_type, source, workload, args.contexts, core)

            samples = {name: [] for name, _collector_type, _core in commands}
            for order in rotated_orders(tuple(command_by_name), sampling.runs):
                for name in order:
                    collector_type, core = command_by_name[name]
                    elapsed, signature = collect(
                        collector_type, source, workload, args.contexts, core
                    )
                    if signature is not None and signature != first_results[name]:
                        raise RuntimeError(f"{name} changed its {workload} evidence")
                    samples[name].append(elapsed)
            timings = {name: summarize(values) for name, values in samples.items()}
            workload_results[workload] = {
                "timings": timings,
                "speedup": (timings["coverage.py"]["median_ms"] / timings["vfy"]["median_ms"]),
                "overhead_speedup": (
                    timings["coverage.py"]["median_ms"] - timings["plain python"]["median_ms"]
                )
                / (timings["vfy"]["median_ms"] - timings["plain python"]["median_ms"]),
            }

    result = {
        "benchmark": "sysmon-events",
        "methodology": {
            "stage": "in-process direct branch collection across execution shapes",
            "iterations": args.iterations,
            "contexts": args.contexts,
            "workloads": [name for name, _source in workloads],
            **sampling.as_dict(),
            "order": "rotating",
            "parity": (
                "exact files, mode, contexts, executed lines, and arcs before and during timing"
            ),
            "reference_cores": {
                "repeated-branches": "sysmon",
                "short-calls": "sysmon",
                "frequent-contexts": "ctrace plus an independent expected signature",
            },
        },
        "environment": {
            **hardware_details(),
            "implementation": sys.implementation.name,
            "vfy": vfy.__version__,
            "vfy_rust_profile": build_profile(),
            "vfy_parser": parser_backend(),
            "sysmon_branch_arcs": coverage_uses_sysmon_branch_arcs(),
        },
        "workloads": workload_results,
    }
    require_revision(repository, revision)
    if artifact_before is not None:
        require_unchanged_artifact(artifact_before)
    result["provenance"] = build_provenance(
        repository,
        workload={"iterations": args.iterations, "contexts": args.contexts},
        workers=1,
        cache="fresh code objects per sample",
        packages=("coverage",),
        publication=sampling.publishable,
    )
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
