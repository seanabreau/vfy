#!/usr/bin/env python3
"""Measure the Rust-data report boundary against forced Python materialization."""

from __future__ import annotations

import argparse
import gc
import importlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Literal, cast

from coverage import CoverageData

from benchmarks.benchmark_harness import add_sampling_arguments, sampling_config, summarize
from benchmarks.benchmark_provenance import (
    artifact_provenance,
    build_provenance,
    hardware_details,
    require_clean,
    require_revision,
    require_unchanged_artifact,
    source_state,
)
from vfy.data import CoverageStore
from vfy.native import (
    CoverageData as ExtensionCoverageData,
    ReportRow,
    analysis_cache_stats,
    analyze_file,
    build_profile,
    clear_analysis_cache,
    parser_backend,
)
from vfy.reports import build_rows

RESOURCE = None if sys.platform == "win32" else importlib.import_module("resource")

RICH_SELECTED_CONTEXTS = ("test_0001", "test_0002", "test_0003")
RICH_CONTEXT_PATTERN = r"^test_000[123]$"


def source_functions(count: int) -> str:
    return "".join(
        f"def function_{index}(value):\n"
        f"    if value:\n        return {index}\n    return -{index}\n\n"
        for index in range(count)
    )


SOURCE = source_functions(32)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=500)
    parser.add_argument("--source-profile", choices=("uniform", "skewed"), default="uniform")
    parser.add_argument("--partial-cache-probe", action="store_true")
    add_sampling_arguments(parser, warmups=6, runs=30)
    parser.add_argument("--diagnostic", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--validate-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--capacity-files", type=int, default=4_224)
    parser.add_argument("--large-files", type=int, default=16)
    parser.add_argument("--large-file-functions", type=int, default=1_024)
    parser.add_argument("--rich-files", type=int, default=256)
    parser.add_argument("--rich-contexts", type=int, default=128)
    parser.add_argument("--semantic-decisions", type=int, default=4_096)
    parser.add_argument("--snapshot-copies", type=int, default=100)
    parser.add_argument(
        "--rich-reports-only",
        action="store_true",
        help="measure only the context-rich selected and all-file report queries",
    )
    parser.add_argument(
        "--cache-probe",
        choices=("entry-capacity", "large-files", "report-pressure"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.diagnostic:
        args.mode = "diagnostic"
    return args


def create_fixture(
    root: Path, file_count: int, *, source_profile: Literal["uniform", "skewed"] = "uniform"
) -> Path:
    measured = {}
    large_source = source_functions(1_024) if source_profile == "skewed" else SOURCE
    for index in range(file_count):
        source = root / f"module_{index:05d}.py"
        contents = large_source if source_profile == "skewed" and index < 8 else SOURCE
        source.write_text(f"{contents}# module {index}\n", encoding="utf-8")
        measured[str(source)] = {1, 2, 3}
    filename = root / ".coverage"
    data = CoverageData(str(filename))
    data.add_lines(measured)
    data.write()
    return filename


def revise_source_comment(path: Path, original: str, revision: int) -> None:
    footer = "# module 0\n"
    if not original.endswith(footer):
        raise RuntimeError("partial-cache source is missing its final module comment")
    path.write_text(
        f"{original.removesuffix(footer)}# module 0 revision {revision}\n", encoding="utf-8"
    )


def create_rich_store(
    root: Path, *, file_count: int, context_count: int, semantic_decisions: int
) -> tuple[CoverageStore, list[str]]:
    if file_count < 1:
        raise ValueError("rich evidence requires at least one file")
    if context_count < 4:
        raise ValueError(
            "rich evidence requires at least four contexts so test_0001 through test_0003 exist"
        )
    if semantic_decisions < 1:
        raise ValueError("rich evidence requires at least one semantic decision")
    paths = []
    measured = {}
    for index in range(file_count):
        path = root / f"rich_{index:05d}.py"
        path.write_text(f"value = {index}\n", encoding="utf-8")
        rendered = str(path)
        paths.append(rendered)
        measured[rendered] = {1}
    semantic_path = root / ".coverage-semantic"
    semantic_seed = CoverageData(str(semantic_path))
    semantic_seed.add_lines(measured)
    semantic_seed.write()
    with closing(sqlite3.connect(semantic_path)) as connection, connection:
        connection.executescript(
            "CREATE TABLE vfy_decision (key text primary key, path text, line integer, "
            "end_line integer, kind text, source_hash text, expression text, logic text, "
            "condition_lines text);"
            "CREATE TABLE vfy_condition (decision text, index_no integer, start_line integer, "
            "start_col integer, end_line integer, end_col integer, "
            "unique (decision, index_no));"
            "CREATE TABLE vfy_observation (context text, decision text, vector text, "
            "outcome integer, unique (context, decision, vector, outcome));"
        )
        definitions = [
            (
                f"decision-{index:05d}",
                paths[index % len(paths)],
                1,
                1,
                "if",
                f"hash-{index % len(paths):05d}",
                "value",
                "c0",
                "1",
            )
            for index in range(semantic_decisions)
        ]
        connection.executemany(
            "INSERT INTO vfy_decision VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", definitions
        )
        connection.executemany(
            "INSERT INTO vfy_condition VALUES (?, 0, 1, 0, 1, 5)",
            ((definition[0],) for definition in definitions),
        )
        connection.executemany(
            "INSERT INTO vfy_observation VALUES (?, ?, '1', 1)",
            (
                (f"test_{index % context_count:04d}", definition[0])
                for index, definition in enumerate(definitions)
            ),
        )
    semantic = ExtensionCoverageData.read(str(semantic_path))
    context_lines = [(f"test_{index:04d}", [1]) for index in range(context_count)]
    files = [(path, [1], [], context_lines, []) for path in paths]
    native = ExtensionCoverageData.from_records(
        False,
        files,
        {},
        {"has_arcs": "0"},
        semantic,
    )
    return CoverageStore(rust_data=native), paths


type RowSignature = tuple[
    str,
    set[int],
    set[int],
    set[int],
    set[tuple[int, int]],
    set[tuple[int, int]],
    dict[int, list[str]],
]
type CacheProbe = Literal["entry-capacity", "large-files", "report-pressure"]


def row_signature(rows: list[ReportRow]) -> list[RowSignature]:
    return [
        (
            row.path,
            row.executable_lines,
            row.covered_lines,
            row.excluded_lines,
            row.possible_arcs,
            row.covered_arcs,
            row.contexts_by_line,
        )
        for row in rows
    ]


def execute(data_file: Path, *, materialize: bool, selected_path: str | None = None) -> float:
    started = time.perf_counter_ns()
    store = CoverageStore.read(data_file)
    if materialize:
        _ = store.files
    rows = build_rows(store, include=[] if selected_path is None else [selected_path])
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    if not rows:
        raise RuntimeError("benchmark unexpectedly produced no report rows")
    return elapsed


def execute_analysis(
    store: CoverageStore, *, cold: bool, expected_signature: list[RowSignature] | None = None
) -> tuple[float, tuple[int, int, int, int]]:
    if cold:
        clear_analysis_cache()
    before_stats = analysis_cache_stats() if expected_signature is not None else None
    started = time.perf_counter_ns()
    rows = build_rows(store)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    if not rows:
        raise RuntimeError("benchmark unexpectedly produced no report rows")
    if expected_signature is not None and row_signature(rows) != expected_signature:
        raise RuntimeError("one-source-changed report rows differ from the validated fixture")
    stats = analysis_cache_stats()
    if before_stats is not None and expected_signature is not None:
        hits = stats[0] - before_stats[0]
        misses = stats[1] - before_stats[1]
        expected_hits = len(expected_signature) - 1
        if (hits, misses) != (expected_hits, 1):
            raise RuntimeError(
                "one-source-changed report must reanalyze exactly one source and reuse "
                f"the other {expected_hits}; cache hits={hits}, misses={misses}"
            )
    return elapsed, stats


def measure_analysis(
    store: CoverageStore,
    *,
    warmups: int,
    runs: int,
    changed_source: Path | None,
    expected_signature: list[RowSignature],
) -> tuple[dict[str, list[float]], list[dict[str, tuple[int, int, int, int]]]]:
    original_source = ""
    if changed_source is not None:
        execute_analysis(store, cold=False)
        original_source = changed_source.read_text(encoding="utf-8")
        revise_source_comment(changed_source, original_source, 0)
        execute_analysis(store, cold=False, expected_signature=expected_signature)

    samples: dict[str, list[float]] = {"cold": [], "reused": []}
    if changed_source is not None:
        samples["one_source_changed"] = []
    cache_records: list[dict[str, tuple[int, int, int, int]]] = []
    comment_revision = 0
    for _ in range(warmups):
        execute_analysis(store, cold=True)
        execute_analysis(store, cold=False)
        if changed_source is not None:
            comment_revision += 1
            revise_source_comment(changed_source, original_source, comment_revision)
            execute_analysis(store, cold=False, expected_signature=expected_signature)
    for _ in range(runs):
        cold_elapsed, cold_stats = execute_analysis(store, cold=True)
        reused_elapsed, reused_stats = execute_analysis(store, cold=False)
        samples["cold"].append(cold_elapsed)
        samples["reused"].append(reused_elapsed)
        cache_records.append({"cold": cold_stats, "reused": reused_stats})
        if changed_source is not None:
            comment_revision += 1
            revise_source_comment(changed_source, original_source, comment_revision)
            changed_elapsed, changed_stats = execute_analysis(
                store, cold=False, expected_signature=expected_signature
            )
            samples["one_source_changed"].append(changed_elapsed)
            cache_records[-1]["one_source_changed"] = changed_stats
    return samples, cache_records


def execute_snapshot(store: CoverageStore, copies: int) -> float:
    native = store.rust_data
    if native is None:
        raise RuntimeError("snapshot benchmark requires Rust-owned data")
    snapshot = native
    started = time.perf_counter_ns()
    for _ in range(copies):
        snapshot = native.copy()
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    if snapshot.line_records() != native.line_records():
        raise RuntimeError("coverage snapshot changed evidence")
    return elapsed


def expected_rich_signature(paths: list[str]) -> list[RowSignature]:
    contexts = {1: list(RICH_SELECTED_CONTEXTS)}
    return [(path, {1}, {1}, set(), set(), set(), contexts) for path in sorted(paths)]


def execute_rich_report(
    store: CoverageStore,
    selected_path: str | None,
    expected_signature: list[RowSignature],
) -> float:
    started = time.perf_counter_ns()
    rows = build_rows(
        store,
        include=[] if selected_path is None else [selected_path],
        contexts=[RICH_CONTEXT_PATTERN],
    )
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    if row_signature(rows) != expected_signature:
        raise RuntimeError("rich report benchmark produced unexpected selected evidence")
    return elapsed


def peak_rss_bytes() -> int | None:
    if RESOURCE is None:
        return None
    value = RESOURCE.getrusage(RESOURCE.RUSAGE_SELF).ru_maxrss
    return value if platform.system() == "Darwin" else value * 1_024


def current_rss_bytes() -> int | None:
    if sys.platform.startswith("linux"):
        resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    if platform.system() == "Darwin":
        return (
            int(
                subprocess.run(
                    ("ps", "-o", "rss=", "-p", str(os.getpid())),
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
            * 1_024
        )
    return None


def memory_observation(before_current: int | None) -> dict[str, int | None]:
    gc.collect()
    current = current_rss_bytes()
    return {
        "current_rss_bytes": current,
        "current_rss_growth_bytes": (
            None if before_current is None or current is None else max(0, current - before_current)
        ),
        "process_peak_rss_bytes": peak_rss_bytes(),
    }


def execute_cache_probe(
    root: Path,
    kind: CacheProbe,
    *,
    capacity_files: int,
    large_files: int,
    large_file_functions: int,
) -> dict[str, object]:
    probe = root / "cache_probe.py"
    if kind == "entry-capacity":
        clear_analysis_cache()
        before_current = current_rss_bytes()
        for index in range(capacity_files):
            probe.write_text(f"value = {index}\n", encoding="utf-8")
            analyze_file(str(probe), [], False)
        return {
            "analyzed_files": capacity_files,
            "cache_stats": analysis_cache_stats(),
            **memory_observation(before_current),
        }
    if kind == "large-files":
        large_source = "".join(
            f"def large_{index}(value):\n"
            f"    if value:\n        return {index}\n"
            f"    return -{index}\n"
            for index in range(large_file_functions)
        )
        clear_analysis_cache()
        before_current = current_rss_bytes()
        for index in range(large_files):
            probe.write_text(f"# large file {index}\n{large_source}", encoding="utf-8")
            analyze_file(str(probe), [], True)
        return {
            "analyzed_files": large_files,
            "functions_per_file": large_file_functions,
            "source_bytes_per_file": len(large_source.encode()),
            "cache_stats": analysis_cache_stats(),
            **memory_observation(before_current),
        }

    data_file = create_fixture(root, capacity_files, source_profile="uniform")
    rust_store = CoverageStore.read(data_file)
    materialized = CoverageStore.read(data_file)
    _ = materialized.files
    rust_rows = build_rows(rust_store)
    expected_signature = row_signature(rust_rows)
    if expected_signature != row_signature(build_rows(materialized)):
        raise RuntimeError("cache-pressure report paths disagree")
    clear_analysis_cache()
    before_current = current_rss_bytes()
    passes = []
    for _ in range(3):
        started = time.perf_counter_ns()
        rows = build_rows(rust_store)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        if row_signature(rows) != expected_signature:
            raise RuntimeError("cache-pressure report changed between passes")
        passes.append(
            {
                "elapsed_ms": elapsed_ms,
                "cache_stats": analysis_cache_stats(),
            }
        )
    return {
        "report_files": capacity_files,
        "timed_stage": "build_rows only; row signatures are validated after each timer stops",
        "passes": passes,
        **memory_observation(before_current),
    }


def isolated_cache_probe(args: argparse.Namespace, kind: CacheProbe) -> dict[str, object]:
    command = [
        sys.executable,
        "-m",
        "benchmarks.benchmark_rust_boundary",
        "--cache-probe",
        kind,
        "--capacity-files",
        str(args.capacity_files),
        "--large-files",
        str(args.large_files),
        "--large-file-functions",
        str(args.large_file_functions),
    ]
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    return cast("dict[str, object]", json.loads(completed.stdout))


def measure_rich_evidence(
    root: Path, args: argparse.Namespace, *, include_snapshots: bool = True
) -> dict[str, object]:
    rust_store, paths = create_rich_store(
        root,
        file_count=args.rich_files,
        context_count=args.rich_contexts,
        semantic_decisions=args.semantic_decisions,
    )
    selected_path = paths[0]
    native = rust_store.rust_data
    if native is None:
        raise RuntimeError("rich fixture did not retain Rust-owned data")
    materialized = CoverageStore(rust_data=native.copy())
    _ = materialized.files
    rust_rows = build_rows(
        rust_store,
        include=[selected_path],
        contexts=[RICH_CONTEXT_PATTERN],
    )
    materialized_rows = build_rows(
        materialized,
        include=[selected_path],
        contexts=[RICH_CONTEXT_PATTERN],
    )
    selected_signature = expected_rich_signature([selected_path])
    all_file_signature = expected_rich_signature(paths)
    if row_signature(rust_rows) != selected_signature:
        raise RuntimeError("rich Rust selected-file report disagrees with expected evidence")
    if row_signature(materialized_rows) != selected_signature:
        raise RuntimeError(
            "rich materialized selected-file report disagrees with expected evidence"
        )
    if row_signature(build_rows(rust_store, contexts=[RICH_CONTEXT_PATTERN])) != all_file_signature:
        raise RuntimeError("rich Rust all-file report disagrees with expected evidence")
    if (
        row_signature(build_rows(materialized, contexts=[RICH_CONTEXT_PATTERN]))
        != all_file_signature
    ):
        raise RuntimeError("rich materialized all-file report disagrees with expected evidence")

    for _ in range(args.warmups):
        if include_snapshots:
            execute_snapshot(rust_store, args.snapshot_copies)
        execute_rich_report(rust_store, selected_path, selected_signature)
        execute_rich_report(materialized, selected_path, selected_signature)
        execute_rich_report(rust_store, None, all_file_signature)
        execute_rich_report(materialized, None, all_file_signature)
    snapshot_samples: list[float] = []
    report_samples = {"rust_handle": [], "python_materialized": []}
    all_file_samples = {"rust_handle": [], "python_materialized": []}
    for index in range(args.runs):
        if include_snapshots:
            snapshot_samples.append(execute_snapshot(rust_store, args.snapshot_copies))
        order = (("rust_handle", rust_store), ("python_materialized", materialized))
        if index % 2:
            order = tuple(reversed(order))
        for name, store in order:
            report_samples[name].append(
                execute_rich_report(store, selected_path, selected_signature)
            )
            all_file_samples[name].append(execute_rich_report(store, None, all_file_signature))
    result = {
        "files": args.rich_files,
        "contexts": args.rich_contexts,
        "semantic_decisions": args.semantic_decisions,
        "selected_contexts": list(RICH_SELECTED_CONTEXTS),
        "context_pattern": RICH_CONTEXT_PATTERN,
        "selected_filtered_report": {
            "rust_handle": summarize(report_samples["rust_handle"]),
            "python_materialized": summarize(report_samples["python_materialized"]),
        },
        "all_files_filtered_report": {
            "rust_handle": summarize(all_file_samples["rust_handle"]),
            "python_materialized": summarize(all_file_samples["python_materialized"]),
        },
    }
    if include_snapshots:
        result["snapshot_copies_per_sample"] = args.snapshot_copies
        result["snapshot_copy"] = summarize(snapshot_samples)
    return result


def rich_report_result(args: argparse.Namespace, revision: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="vfy-boundary-rich-") as temporary:
        rich_evidence = measure_rich_evidence(
            Path(temporary).resolve(), args, include_snapshots=False
        )
    return {
        "benchmark": "rust-boundary",
        "methodology": {
            "warmups": args.warmups,
            "measured_runs": args.runs,
            "stage": "context-filtered Rust report-row construction",
            "order": "alternating",
            "validation": (
                "selected and all-file report rows must match independently derived paths, "
                "covered lines, and exact context attribution before timing and after every "
                "timed query"
            ),
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "rust_profile": build_profile(),
            "parser": parser_backend(),
        },
        "provenance": {
            "revision": revision,
            "tracked_worktree_clean": True,
        },
        "rich_evidence": rich_evidence,
    }


def write_result(args: argparse.Namespace, result: Mapping[str, object], revision: str) -> None:
    require_revision(Path(__file__).resolve().parents[1], revision)
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


def main() -> int:
    args = parse_args()
    sampling = sampling_config(args)
    if args.cache_probe:
        with tempfile.TemporaryDirectory(prefix="vfy-cache-probe-") as temporary:
            result = execute_cache_probe(
                Path(temporary).resolve(),
                args.cache_probe,
                capacity_files=args.capacity_files,
                large_files=args.large_files,
                large_file_functions=args.large_file_functions,
            )
        print(json.dumps(result))
        return 0
    artifact_before = (
        artifact_provenance(strict=True)
        if sampling.publishable and (not args.validate_only or args.rich_reports_only)
        else None
    )
    repository = Path(__file__).resolve().parents[1]
    starting_state = require_clean(repository) if sampling.publishable else source_state(repository)
    if sampling.publishable and build_profile() != "release":
        raise SystemExit("publication benchmarks require a release-mode vfy build")
    revision = starting_state.revision
    if args.rich_reports_only:
        result = rich_report_result(args, revision)
        if artifact_before is not None:
            require_unchanged_artifact(artifact_before)
        result["provenance"] = build_provenance(
            repository,
            workload={"rich_reports_only": True, "files": args.rich_files},
            workers=1,
            cache="analysis cache retained between report passes",
            publication=sampling.publishable,
        )
        write_result(args, result, revision)
        return 0
    with tempfile.TemporaryDirectory(prefix="vfy-boundary-") as temporary:
        data_file = create_fixture(
            Path(temporary).resolve(), args.files, source_profile=args.source_profile
        )
        rust_store = CoverageStore.read(data_file)
        materialized = CoverageStore.read(data_file)
        _ = materialized.files
        expected_signature = row_signature(build_rows(rust_store))
        if expected_signature != row_signature(build_rows(materialized)):
            raise RuntimeError("Rust and materialized report paths disagree")
        selected_path = str(Path(temporary).resolve() / "module_00000.py")
        if row_signature(build_rows(rust_store, include=[selected_path])) != row_signature(
            build_rows(materialized, include=[selected_path])
        ):
            raise RuntimeError("selected Rust and materialized report paths disagree")
        rich_evidence = measure_rich_evidence(Path(temporary).resolve(), args)

        if args.validate_only:
            if args.partial_cache_probe:
                source_path = Path(selected_path)
                original_source = source_path.read_text(encoding="utf-8")
                execute_analysis(rust_store, cold=False)
                revise_source_comment(source_path, original_source, 1)
                execute_analysis(rust_store, cold=False, expected_signature=expected_signature)
            result: dict[str, object] = {
                "benchmark": "rust-boundary",
                "validation_only": True,
                "methodology": {
                    "mode": sampling.mode.value,
                    "validation": (
                        "Rust and materialized report rows agree for full and selected files"
                    ),
                },
                "environment": {
                    **hardware_details(),
                    "rust_profile": build_profile(),
                    "parser": parser_backend(),
                },
                "provenance": build_provenance(
                    repository,
                    workload={
                        "files": args.files,
                        "source_profile": args.source_profile,
                        "partial_cache_probe": args.partial_cache_probe,
                        "rich_files": args.rich_files,
                    },
                    workers=1,
                    cache="validation-only; no timing samples",
                    packages=("coverage",),
                    publication=False,
                ),
            }
            write_result(args, result, revision)
            return 0

        for index in range(sampling.warmups):
            for materialize in (False, True) if index % 2 == 0 else (True, False):
                execute(data_file, materialize=materialize)
                execute(data_file, materialize=materialize, selected_path=selected_path)

        samples = {"rust_handle": [], "python_materialized": []}
        selected_samples = {"rust_handle": [], "python_materialized": []}
        for index in range(sampling.runs):
            order = (False, True) if index % 2 == 0 else (True, False)
            for materialize in order:
                key = "python_materialized" if materialize else "rust_handle"
                samples[key].append(execute(data_file, materialize=materialize))
                selected_samples[key].append(
                    execute(data_file, materialize=materialize, selected_path=selected_path)
                )

        analysis_samples, analysis_cache_records = measure_analysis(
            rust_store,
            warmups=sampling.warmups,
            runs=sampling.runs,
            changed_source=Path(selected_path) if args.partial_cache_probe else None,
            expected_signature=expected_signature,
        )

        cache_retention = {
            "measurement": (
                "each workload runs in a fresh subprocess; current RSS is retained memory "
                "after garbage collection and peak RSS is process lifetime peak"
            ),
            "entry_capacity_workload": isolated_cache_probe(args, "entry-capacity"),
            "large_file_workload": isolated_cache_probe(args, "large-files"),
            "report_pressure_workload": isolated_cache_probe(args, "report-pressure"),
        }

    rust_result = summarize(samples["rust_handle"])
    materialized_result = summarize(samples["python_materialized"])
    selected_rust_result = summarize(selected_samples["rust_handle"])
    selected_materialized_result = summarize(selected_samples["python_materialized"])
    result = {
        "benchmark": "rust-boundary",
        "methodology": {
            "fixture_files": args.files,
            "source_profile": args.source_profile,
            "partial_cache_probe": args.partial_cache_probe,
            **sampling.as_dict(),
            "stage": "schema read through Rust report-row construction",
            "order": "alternating",
            "validation": (
                "all report-row semantic fields must agree before timing for full and "
                "single-file reports"
            ),
        },
        "environment": {
            **hardware_details(),
            "rust_profile": build_profile(),
            "parser": parser_backend(),
        },
        "rust_handle": rust_result,
        "python_materialized": materialized_result,
        "speedup": materialized_result["median_ms"] / rust_result["median_ms"],
        "selected_file": {
            "rust_handle": selected_rust_result,
            "python_materialized": selected_materialized_result,
            "speedup": (
                selected_materialized_result["median_ms"] / selected_rust_result["median_ms"]
            ),
        },
        "rich_evidence": rich_evidence,
        "source_analysis": {
            "cold": summarize(analysis_samples["cold"]),
            "reused": summarize(analysis_samples["reused"]),
            **(
                {"one_source_changed": summarize(analysis_samples["one_source_changed"])}
                if args.partial_cache_probe
                else {}
            ),
            "cache_stats": analysis_cache_records,
            "retained_memory": cache_retention,
        },
    }
    if artifact_before is not None:
        require_unchanged_artifact(artifact_before)
    result["provenance"] = build_provenance(
        repository,
        workload={
            "files": args.files,
            "source_profile": args.source_profile,
            "partial_cache_probe": args.partial_cache_probe,
            "rich_files": args.rich_files,
        },
        workers=1,
        cache="analysis cache and report rows retained between passes",
        packages=("coverage",),
        publication=sampling.publishable,
    )
    provenance = result["provenance"]
    provenance["starting_source"] = starting_state.as_dict()
    provenance["ending_source"] = source_state(repository).as_dict()
    write_result(args, result, revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
