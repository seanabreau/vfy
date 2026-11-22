#!/usr/bin/env python3
"""Measure selection evidence conversion and schema-7 persistence."""

from __future__ import annotations

import argparse
import json
import tempfile
from contextlib import chdir
from pathlib import Path
from typing import TYPE_CHECKING

from benchmarks.benchmark_harness import (
    add_sampling_arguments,
    sampling_config,
    summarize,
    timed_call,
)
from benchmarks.benchmark_provenance import (
    artifact_provenance,
    build_provenance,
    require_clean,
    require_revision,
    require_unchanged_artifact,
)
from vfy.native import CoverageData, build_profile, plan_test_selection

if TYPE_CHECKING:
    from vfy.native_types import SelectionRecord


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=128)
    parser.add_argument("--contexts", type=int, default=64)
    parser.add_argument("--arcs", type=int, default=16)
    add_sampling_arguments(parser, warmups=6, runs=30)
    parser.add_argument("--diagnostic", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--validate-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.diagnostic:
        args.mode = "diagnostic"
    return args


def context_arc_values(index: int, arcs: int) -> list[tuple[int, int]]:
    """Give each context distinct arcs; every fourth context reaches line 1."""
    anchor = index + 2
    if index % 4 == 0:
        tail = [(anchor + offset, anchor + offset + 1) for offset in range(arcs - 2)]
        return [(-1, 1), (1, anchor), *tail][:arcs]
    return [
        (-anchor, anchor),
        *((anchor + offset, anchor + offset + 1) for offset in range(arcs - 1)),
    ]


def create_data(root: Path, files: int, contexts: int, arcs: int) -> CoverageData:
    if min(files, contexts, arcs) < 1:
        raise ValueError("selection and persistence dimensions must be positive")
    context_arcs = [
        (f"test_{index:04d}|run", context_arc_values(index, arcs)) for index in range(contexts)
    ]
    arc_values = sorted({arc for _, values in context_arcs for arc in values})
    lines = list(range(1, contexts + arcs + 1))
    records = []
    for index in range(files):
        path = root / f"module_{index:05d}.py"
        path.write_text("\n".join("value = 1" for _ in lines) + "\n", encoding="utf-8")
        records.append((str(path), lines, arc_values, [], context_arcs))
    return CoverageData.from_records(True, records, {}, {"has_arcs": "1"})


def expected_tests(contexts: int) -> list[str]:
    return [f"test_{index:04d}" for index in range(contexts)]


def expected_selected_tests(contexts: int) -> list[str]:
    return [f"test_{index:04d}" for index in range(0, contexts, 4)]


def expected_arc_records(
    root: Path, files: int, contexts: int, arcs: int
) -> list[tuple[str, str, list[tuple[int, int]]]]:
    return [
        (
            f"test_{context:04d}|run",
            str(root / f"module_{file:05d}.py"),
            sorted(set(context_arc_values(context, arcs))),
        )
        for context in range(contexts)
        for file in range(files)
    ]


def selection_result(data: CoverageData, history: Path) -> SelectionRecord:
    return plan_test_selection(
        data,
        str(history),
        source_roots=[str(history.parent)],
        patch="+++ b/module_00000.py\n@@ -1,0 +1,1 @@\n+value = 2\n",
        mode="aggressive",
    )


def validate_selection(
    result: SelectionRecord,
    expected_all: list[str],
    expected_selected: list[str],
    changed_path: Path,
) -> None:
    if list(result["all_tests"]) != expected_all:
        raise RuntimeError("selection benchmark changed the complete test inventory")
    if list(result["selected"]) != expected_selected or result["fallback_full"] is not False:
        raise RuntimeError("selection benchmark changed the arc-dependent selected tests")
    if result["affected_files"] != [str(changed_path.resolve())]:
        raise RuntimeError("selection benchmark did not analyze the changed fixture file")


def measure_selection(
    data: CoverageData,
    history: Path,
    expected_all: list[str],
    expected_selected: list[str],
) -> float:
    with chdir(history.parent):
        elapsed, result = timed_call(lambda: selection_result(data, history))
    validate_selection(result, expected_all, expected_selected, history.parent / "module_00000.py")
    return elapsed


def measure_persistence(data: CoverageData, destination: Path) -> float:
    elapsed, _ = timed_call(lambda: data.write(str(destination)))
    return elapsed


def measure_read(
    destination: Path, expected: list[tuple[str, str, list[tuple[int, int]]]]
) -> float:
    elapsed, loaded = timed_call(lambda: CoverageData.read(str(destination)))
    if loaded.arc_records() != expected:
        raise RuntimeError("schema-7 read benchmark changed arc evidence")
    return elapsed


def main() -> int:
    args = parse_args()
    sampling = sampling_config(args)
    artifact_before = (
        artifact_provenance(strict=True)
        if sampling.publishable and not args.validate_only
        else None
    )
    repository = Path(__file__).resolve().parents[1]
    starting_state = require_clean(repository) if sampling.publishable else None
    with tempfile.TemporaryDirectory(prefix="vfy-selection-persistence-") as temporary:
        root = Path(temporary).resolve()
        data = create_data(root, args.files, args.contexts, args.arcs)
        expected_arcs = expected_arc_records(root, args.files, args.contexts, args.arcs)
        if data.arc_records() != expected_arcs:
            raise RuntimeError("benchmark fixture differs from expected arc evidence")
        expected_all = expected_tests(args.contexts)
        expected_selected = expected_selected_tests(args.contexts)
        history = root / "history.json"
        destinations = [root / "first.coverage", root / "second.coverage"]
        with chdir(history.parent):
            validate_selection(
                selection_result(data, history),
                expected_all,
                expected_selected,
                root / "module_00000.py",
            )
        if args.validate_only:
            data.write(str(root / "validation.coverage"))
            measure_read(root / "validation.coverage", expected_arcs)
            result = {
                "benchmark": "selection-persistence",
                "validation_only": True,
                "methodology": {"validation": "selection and schema-7 arc round-trip"},
                "provenance": build_provenance(
                    repository,
                    workload={
                        "files": args.files,
                        "contexts": args.contexts,
                        "arcs": args.arcs,
                    },
                    workers=1,
                    cache="validation only",
                    packages=("coverage",),
                    publication=False,
                ),
            }
            rendered = json.dumps(result, indent=2) + "\n"
            if args.output:
                args.output.write_text(rendered, encoding="utf-8")
            else:
                print(rendered, end="")
            return 0
        for index in range(sampling.warmups):
            measure_selection(data, history, expected_all, expected_selected)
            destination = destinations[index % len(destinations)]
            measure_persistence(data, destination)
            measure_read(destination, expected_arcs)
        selection_samples = []
        persistence_samples = []
        read_samples = []
        for index in range(sampling.runs):
            selection_samples.append(
                measure_selection(data, history, expected_all, expected_selected)
            )
            destination = destinations[index % len(destinations)]
            persistence_samples.append(measure_persistence(data, destination))
            read_samples.append(measure_read(destination, expected_arcs))
    result = {
        "benchmark": "selection-persistence",
        "methodology": {
            "files": args.files,
            "contexts": args.contexts,
            "arcs_per_partition": args.arcs,
            **sampling.as_dict(),
            "validation": (
                "an independently expected, arc-dependent selection is checked before timing "
                "and after every sample; persisted arc records are checked after timed reads"
            ),
        },
        "environment": {"rust_profile": build_profile()},
        "selection": summarize(selection_samples),
        "persistence": summarize(persistence_samples),
        "read": summarize(read_samples),
    }
    if starting_state is not None:
        require_revision(repository, starting_state.revision)
    if artifact_before is not None:
        require_unchanged_artifact(artifact_before)
    result["provenance"] = build_provenance(
        repository,
        workload={"files": args.files, "contexts": args.contexts, "arcs": args.arcs},
        workers=1,
        cache="fresh destination files per sample",
        packages=("coverage",),
        publication=sampling.publishable,
    )
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
