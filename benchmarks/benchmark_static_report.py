#!/usr/bin/env python3
"""Measure source analysis and report generation from prepared project data.

The project checkout, interpreter, dependency lock, and coverage database are
prepared before this command starts.  The timed commands never run tests or
install packages.  A Coverage.py database is loaded once by both engines and
must have identical files, lines, arcs, and contexts before samples begin.
Publication also binds the installed extension to the verified build manifest
before any timer starts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from coverage import Coverage, CoverageData

from benchmarks.benchmark_harness import (
    BenchmarkMode,
    SamplingConfig,
    add_sampling_arguments,
    benchmark_environment,
    publishable_command,
    rotated_orders,
    run_checked,
    sampling_config,
    summarize,
    timed,
    timed_call,
)
from benchmarks.benchmark_projects import (
    canonical_file,
    load_json,
    normalized_data,
    preparation_metadata,
    selected_interpreter_metadata,
)
from benchmarks.benchmark_provenance import (
    JSONValue,
    prepared_project_provenance,
    require_clean,
    require_revision,
    source_state,
)
from benchmarks.project_manifest import ProjectManifest, manifest
from vfy.analysis import FileAnalysis, analyze_source
from vfy.data import CoverageStore
from vfy.native import clear_analysis_cache


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="project key from project_manifest")
    parser.add_argument(
        "--prepared",
        type=Path,
        required=True,
        help="preparation destination containing project/ and the selected environment",
    )
    parser.add_argument("--root", type=Path, required=True, help="prepared project checkout")
    parser.add_argument(
        "--python",
        type=Path,
        required=True,
        help="selected interpreter from the preparation",
    )
    parser.add_argument(
        "--preparation-manifest",
        type=Path,
        required=True,
        help="manifest.json written by prepare_projects.py",
    )
    parser.add_argument(
        "--data-file",
        type=Path,
        required=True,
        help="validated schema-7 Coverage.py database",
    )
    parser.add_argument(
        "--source",
        action="append",
        type=Path,
        help="source directory relative to --root (defaults to the project manifest path)",
    )
    add_sampling_arguments(parser)
    parser.add_argument("--diagnostic", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--validate-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--artifacts",
        type=Path,
        help="directory for exact per-sample report outputs and source signatures",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.diagnostic:
        args.mode = BenchmarkMode.DIAGNOSTIC.value
    return args


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_digest(paths: Sequence[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def _json_sha256(value: object) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _load_project_manifest(project_key: str) -> ProjectManifest:
    return manifest(project_key)


def _resolve_under(root: Path, value: Path) -> Path:
    resolved = value if value.is_absolute() else root / value
    return resolved.resolve()


def _preparation_record(
    args: argparse.Namespace, project: ProjectManifest, *, selected_python: Path, root: Path
) -> tuple[dict[str, object], dict[str, object]]:
    prepared = args.prepared.resolve()
    manifest_path = args.preparation_manifest.resolve()
    metadata = preparation_metadata(selected_python, root, manifest_path=manifest_path)
    if metadata.get("project") != project.key:
        raise RuntimeError(
            "preparation project mismatch: "
            f"expected {project.key!r}, got {metadata.get('project')!r}"
        )
    if metadata.get("revision") != project.revision:
        raise RuntimeError("preparation manifest revision does not match project_manifest")
    recorded_python = metadata.get("python")
    if not isinstance(recorded_python, str) or Path(recorded_python).absolute() != selected_python:
        raise RuntimeError("selected interpreter does not match the preparation manifest")
    if Path(root).resolve() != (prepared / "project").resolve():
        raise RuntimeError("prepared project must be the preparation destination's project/")
    selected = selected_interpreter_metadata(
        selected_python,
        root=root,
        environment=benchmark_environment(),
    )
    return metadata, selected


def _native_data_snapshot(store: CoverageStore, root: Path) -> dict[str, object]:
    native = store.rust_data
    if native is None:
        raise RuntimeError("static-report requires an unmaterialized Rust coverage store")
    filenames = sorted(native.measured_files())
    contexts = sorted(native.measured_contexts())
    lines_by_context: dict[tuple[str, str], set[int]] = {}
    arcs_by_context: dict[tuple[str, str], set[tuple[int, int]]] = {}
    for context, filename, lines in native.line_records():
        lines_by_context.setdefault((context, filename), set()).update(lines)
    for context, filename, arcs in native.arc_records():
        arcs_by_context.setdefault((context, filename), set()).update(arcs)

    context_files: dict[str, set[str]] = {context: set() for context in contexts}
    for context, filename in (*lines_by_context, *arcs_by_context):
        context_files.setdefault(context, set()).add(filename)

    def context_lines(context: str, filename: str) -> set[int]:
        lines = set(lines_by_context.get((context, filename), set()))
        lines.update(
            line
            for arc in arcs_by_context.get((context, filename), set())
            for line in arc
            if line > 0
        )
        return lines

    by_context: dict[str, dict[str, dict[str, object]]] = {}
    for context in contexts:
        by_context[context] = {
            canonical_file(filename, root): {
                "lines": sorted(context_lines(context, filename)),
                "arcs": sorted(arcs_by_context.get((context, filename), set())),
            }
            for filename in sorted(context_files.get(context, set()))
        }

    tracers = dict(native.tracer_records())
    contexts_by_file_line: dict[str, dict[int, set[str]]] = {}
    for (context, filename), lines in lines_by_context.items():
        for line in lines:
            contexts_by_file_line.setdefault(filename, {}).setdefault(line, set()).add(context)
    for (context, filename), arcs in arcs_by_context.items():
        for line in (line for arc in arcs for line in arc if line > 0):
            contexts_by_file_line.setdefault(filename, {}).setdefault(line, set()).add(context)
    file_values: dict[str, dict[str, object]] = {}
    for filename in filenames:
        path = canonical_file(filename, root)
        file_values[path] = {
            "lines": sorted(set(store.lines(filename) or set())),
            "arcs": sorted(set(store.arcs(filename) or set())),
            "tracer": tracers.get(filename, ""),
            "contexts": {
                line: sorted(values)
                for line, values in sorted(contexts_by_file_line.get(filename, {}).items())
            },
        }
    return {
        "has_arcs": native.has_arcs,
        "contexts": contexts,
        "by_context": by_context,
        "files": file_values,
    }


def _load_database(
    data_file: Path, root: Path
) -> tuple[CoverageData, CoverageStore, dict[str, object]]:
    if not data_file.is_file():
        raise RuntimeError(f"coverage database is missing: {data_file}")
    reference = CoverageData(str(data_file))
    reference.read()
    store = CoverageStore.read(data_file)
    if not reference.measured_files():
        raise RuntimeError("coverage database contains no measured files")
    if normalized_data(data_file, root) != _native_data_snapshot(store, root):
        raise RuntimeError(
            "Coverage.py and vfy schema-7 files, lines, arcs, tracers, or contexts differ"
        )
    return (
        reference,
        store,
        {
            "path": str(data_file),
            "sha256": _sha256(data_file),
            "files": len(reference.measured_files()),
            "contexts": len(reference.measured_contexts()),
            "has_arcs": reference.has_arcs(),
            "validation": "Coverage.py and vfy exact schema-7 files, lines, arcs, and contexts",
        },
    )


def _source_paths(
    root: Path,
    source_values: Sequence[Path] | None,
    project: ProjectManifest,
    store: CoverageStore,
) -> tuple[Path, ...]:
    values = source_values or (Path(project.package_path),)
    roots = tuple(_resolve_under(root, value) for value in values)
    if any(not path.is_dir() for path in roots):
        missing = [str(path) for path in roots if not path.is_dir()]
        raise RuntimeError(f"source directory is missing: {', '.join(missing)}")
    files: list[Path] = []
    for filename in store.measured_files():
        path = _resolve_under(root, Path(filename))
        if (
            path.is_file()
            and path.suffix == ".py"
            and any(path == source or source in path.parents for source in roots)
        ):
            files.append(path)
    if not files:
        raise RuntimeError("validated coverage database has no measured files under the source")
    return tuple(sorted(set(files)))


def _analysis_signature(value: FileAnalysis, *, has_arcs: bool) -> tuple[object, ...]:
    return (
        tuple(sorted(value.executable)),
        tuple(sorted(value.excluded)),
        tuple(sorted(value.possible_arcs)) if has_arcs else (),
        tuple(sorted(value.no_branch)),
        value.source_missing,
        value.source_error_kind,
        value.source_error_detail,
    )


def _coverage_source_signatures(
    paths: Iterable[Path],
    *,
    data_file: Path,
    source_roots: Sequence[Path],
    has_arcs: bool,
    config_file: Path | None,
) -> tuple[dict[str, tuple[object, ...]], dict[str, object]]:
    coverage = Coverage(
        data_file=str(data_file),
        source=[str(path) for path in source_roots],
        branch=has_arcs,
        config_file=str(config_file) if config_file is not None else False,
    )
    coverage.load()
    patterns = {
        "exclude": tuple(coverage.config.exclude_list),
        "partial": tuple(coverage.config.partial_list) + tuple(coverage.config.partial_always_list),
        "config_file": str(config_file) if config_file is not None else None,
    }
    signatures: dict[str, tuple[object, ...]] = {}
    for path in paths:
        analysis = coverage._analyze(str(path))
        statements = set(analysis.statements)
        excluded = set(analysis.excluded)
        reporter = coverage._get_file_reporter(str(path))
        branch_lines = set(analysis.branch_stats())
        signatures[str(path)] = (
            tuple(sorted(statements)),
            tuple(sorted(excluded)),
            tuple(sorted(arc for arc in analysis.arc_possibilities if arc[0] in branch_lines)),
            tuple(sorted(reporter.no_branch_lines())),
        )
    return signatures, patterns


def _validate_source_oracle(
    vfy_signatures: Mapping[str, tuple[object, ...]],
    coverage_signatures: Mapping[str, tuple[object, ...]],
) -> None:
    if vfy_signatures.keys() != coverage_signatures.keys():
        raise RuntimeError("Coverage.py and vfy source file sets differ")
    for path, vfy in vfy_signatures.items():
        statements, excluded, arcs, no_branch = coverage_signatures[path]
        expected_executable = statements
        if vfy[0] != expected_executable:
            raise RuntimeError(f"source executable lines differ for {path}")
        if vfy[1] != excluded:
            raise RuntimeError(f"source excluded lines differ for {path}")
        if vfy[2] != arcs:
            raise RuntimeError(f"possible source arcs differ for {path}")
        if vfy[3] != no_branch:
            raise RuntimeError(f"partial-branch lines differ for {path}")


def _analyze_sources(
    paths: Iterable[Path],
    *,
    has_arcs: bool,
    excludes: Sequence[str],
    partials: Sequence[str],
) -> dict[str, FileAnalysis]:
    exclude_patterns = list(excludes)
    partial_patterns = list(partials)
    return {
        str(path): analyze_source(
            str(path), excludes=exclude_patterns, partials=partial_patterns, collect_arcs=has_arcs
        )
        for path in paths
    }


def _pattern_values(patterns: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = patterns.get(name)
    if not isinstance(value, tuple):
        raise RuntimeError(f"analysis configuration has no {name} patterns")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RuntimeError(f"analysis configuration has invalid {name} patterns")
        result.append(item)
    return tuple(result)


def _report_semantics(path: Path, root: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"report is not a JSON object: {path}")
    totals = payload.get("totals")
    if not isinstance(totals, dict):
        raise RuntimeError(f"report is missing files or totals: {path}")
    return {"files": load_json(path, root), "totals": totals}


def _validate_report(path: Path, root: Path, expected: dict[str, object]) -> None:
    actual = _report_semantics(path, root)
    if actual != expected:
        raise RuntimeError(f"report semantics changed after timing: {path}")


def _record_source_signatures(
    path: Path,
    values: Mapping[str, tuple[object, ...]],
    *,
    oracle: Mapping[str, tuple[object, ...]],
    patterns: Mapping[str, object],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "files": len(values),
        "signature_sha256": _json_sha256(values),
        "coverage_oracle_signature_sha256": _json_sha256(oracle),
        "analysis_patterns": patterns,
        "signatures": dict(sorted(values.items())),
        "coverage_signatures": dict(sorted(oracle.items())),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sample_reports(
    *,
    args: argparse.Namespace,
    root: Path,
    raw: Path,
    data_file: Path,
    expected: dict[str, object],
    sampling: SamplingConfig,
) -> tuple[dict[str, list[float]], dict[str, object], list[str]]:
    report_data_file = raw / "validated.coverage"
    shutil.copyfile(data_file, report_data_file)
    commands = {
        "coverage.py": [
            sys.executable,
            "-m",
            "coverage",
            "json",
            "--data-file",
            str(report_data_file),
            "-o",
            str(raw / "current-coverage.json"),
        ],
        "vfy": [
            sys.executable,
            "-m",
            "vfy",
            "coverage",
            "json",
            "--data-file",
            str(report_data_file),
            "-o",
            str(raw / "current-vfy.json"),
        ],
    }
    raw_paths: list[str] = []
    for order in rotated_orders(tuple(commands), sampling.warmups):
        for name in order:
            output = Path(commands[name][-1])
            output.unlink(missing_ok=True)
            run_checked(commands[name], cwd=root, env=benchmark_environment(), timeout=600)
            _validate_report(output, root, expected)

    samples: dict[str, list[float]] = {name: [] for name in commands}
    for index, order in enumerate(rotated_orders(tuple(commands), sampling.runs)):
        for name in order:
            output = Path(commands[name][-1])
            output.unlink(missing_ok=True)
            elapsed = timed(commands[name], cwd=root, env=benchmark_environment(), timeout=600)
            samples[name].append(float(elapsed))
            _validate_report(output, root, expected)
            if args.artifacts is not None:
                retained = args.artifacts / "raw" / f"report-{index:03d}-{name}.json"
                retained.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(output, retained)
                raw_paths.append(str(retained))

    result: dict[str, object] = {name: summarize(values) for name, values in samples.items()}
    coverage_summary = result["coverage.py"]
    vfy_summary = result["vfy"]
    if not isinstance(coverage_summary, dict) or not isinstance(vfy_summary, dict):
        raise RuntimeError("timing summaries are malformed")
    coverage_median = coverage_summary.get("median_ms")
    vfy_median = vfy_summary.get("median_ms")
    if not isinstance(coverage_median, int | float) or not isinstance(vfy_median, int | float):
        raise RuntimeError("timing summaries have no numeric medians")
    result["speedup"] = float(coverage_median) / float(vfy_median)
    result["commands"] = {
        name: publishable_command(command, cwd=root) for name, command in commands.items()
    }
    return samples, result, raw_paths


def _initial_report_parity(root: Path, data_file: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="vfy-static-report-gate-") as temporary:
        raw = Path(temporary).resolve()
        report_data_file = raw / "validated.coverage"
        shutil.copyfile(data_file, report_data_file)
        commands = {
            "coverage.py": [
                sys.executable,
                "-m",
                "coverage",
                "json",
                "--data-file",
                str(report_data_file),
                "-o",
                str(raw / "coverage.json"),
            ],
            "vfy": [
                sys.executable,
                "-m",
                "vfy",
                "coverage",
                "json",
                "--data-file",
                str(report_data_file),
                "-o",
                str(raw / "vfy.json"),
            ],
        }
        expected: dict[str, object] | None = None
        for name, command in commands.items():
            run_checked(command, cwd=root, env=benchmark_environment(), timeout=600)
            value = _report_semantics(Path(command[-1]), root)
            if name == "coverage.py":
                expected = value
            elif value != expected:
                raise RuntimeError("Coverage.py and vfy report semantics differ")
        assert expected is not None
        return expected


def _prepared_provenance(
    *,
    repository: Path,
    project: ProjectManifest,
    source_files: Sequence[Path],
    database: Mapping[str, object],
    sampling: SamplingConfig,
    selected: Mapping[str, object],
    preparation: Mapping[str, object],
) -> dict[str, object]:
    database_sha256 = database.get("sha256")
    if not isinstance(database_sha256, str):
        raise RuntimeError("validated database has no SHA-256 provenance")
    build = preparation.get("vfy_wheel")
    if not isinstance(build, dict):
        raise RuntimeError("preparation metadata has no vfy build record")
    vfy_revision = build.get("source_revision")
    if not isinstance(vfy_revision, str) or len(vfy_revision) != 40:
        raise RuntimeError("prepared vfy build has no full source revision")
    workload: dict[str, JSONValue] = {
        "project": project.key,
        "source_files": len(source_files),
        "database_sha256": database_sha256,
        "stages": ["source-analysis", "json-report"],
    }
    return prepared_project_provenance(
        repository,
        workload=workload,
        workers=1,
        cache="analysis cache cleared before every source-analysis sample",
        packages=("coverage",),
        selected=selected,
        prepared=preparation,
        source_revision=vfy_revision,
        upstream_revision=project.revision,
        sampling=sampling.as_dict(),
        publication=sampling.publishable,
        validation=(
            "Coverage.py and vfy schema-7 data, source-analysis signatures, and normalized "
            "reports matched before timing"
        ),
    )


def _recheck_prepared_artifact(
    args: argparse.Namespace,
    project: ProjectManifest,
    *,
    selected_python: Path,
    root: Path,
    preparation: Mapping[str, object],
    selected: Mapping[str, object],
) -> None:
    preparation_after, selected_after = _preparation_record(
        args, project, selected_python=selected_python, root=root
    )
    if preparation_after.get("vfy_wheel") != preparation.get("vfy_wheel"):
        raise RuntimeError("prepared vfy build changed during the static benchmark")
    if selected_after.get("extension_sha256") != selected.get("extension_sha256"):
        raise RuntimeError("installed vfy extension changed during the static benchmark")


def _run(args: argparse.Namespace) -> dict[str, object]:
    sampling = sampling_config(args)
    root = args.root.resolve()
    selected_python = args.python.absolute()
    project = _load_project_manifest(args.project)
    preparation, selected = _preparation_record(
        args, project, selected_python=selected_python, root=root
    )
    if Path(sys.executable).absolute() != selected_python:
        raise RuntimeError("benchmark controller must run under the selected prepared interpreter")
    source_state_before = (
        require_clean(Path(__file__).resolve().parents[1])
        if sampling.publishable
        else source_state(Path(__file__).resolve().parents[1])
    )
    prepared_state = require_clean(root)
    reference, store, database = _load_database(args.data_file.resolve(), root)
    source_files = _source_paths(root, args.source, project, store)
    source_values = args.source or (Path(project.package_path),)
    source_roots = tuple(_resolve_under(root, value) for value in source_values)
    source_digest_before = _source_digest(source_files, root)
    database_digest_before = str(database["sha256"])
    source_has_arcs = reference.has_arcs()
    coverage_config = root / ".coveragerc"
    coverage_source_signatures, source_patterns = _coverage_source_signatures(
        source_files,
        data_file=args.data_file.resolve(),
        source_roots=source_roots,
        has_arcs=source_has_arcs,
        config_file=coverage_config if coverage_config.is_file() else None,
    )
    source_analyses = _analyze_sources(
        source_files,
        has_arcs=source_has_arcs,
        excludes=_pattern_values(source_patterns, "exclude"),
        partials=_pattern_values(source_patterns, "partial"),
    )
    source_signatures = {
        path: _analysis_signature(analysis, has_arcs=source_has_arcs)
        for path, analysis in source_analyses.items()
    }
    if args.artifacts is not None:
        _record_source_signatures(
            args.artifacts / "raw" / "source-analysis-signatures.json",
            source_signatures,
            oracle=coverage_source_signatures,
            patterns=source_patterns,
        )
    _validate_source_oracle(source_signatures, coverage_source_signatures)
    expected_report = _initial_report_parity(root, args.data_file.resolve())

    if args.validate_only:
        result = {
            "schema": 1,
            "benchmark": "static-report",
            "project": args.project,
            "source_files": len(source_files),
            "database": database,
            "preparation": preparation,
            "validation": {
                "database": "exact Coverage.py/vfy schema-7 parity",
                "report": "Coverage.py and vfy normalized JSON reports match",
                "source_analysis": "Coverage.py statements, exclusions, arcs, and partials match",
                "source_analysis_signature_sha256": _json_sha256(source_signatures),
                "analysis_patterns": source_patterns,
                "test_execution": "none",
            },
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return result

    if args.output is None:
        raise SystemExit("publication and diagnostic runs require --output")
    if sampling.publishable and args.artifacts is None:
        raise SystemExit("publication runs require --artifacts for retained raw reports")

    repository = Path(__file__).resolve().parents[1]
    provenance = _prepared_provenance(
        repository=repository,
        project=project,
        source_files=source_files,
        database=database,
        sampling=sampling,
        selected=selected,
        preparation=preparation,
    )

    artifact_root = args.artifacts.resolve() if args.artifacts else Path(tempfile.mkdtemp())
    artifact_root.mkdir(parents=True, exist_ok=True)
    raw_root = artifact_root / ".working"
    raw_root.mkdir(parents=True, exist_ok=True)
    source_samples: list[float] = []
    source_sample_signatures: list[str] = []

    def measure_source() -> dict[str, tuple[object, ...]]:
        clear_analysis_cache()
        elapsed, analyses = timed_call(
            lambda: _analyze_sources(
                source_files,
                has_arcs=source_has_arcs,
                excludes=_pattern_values(source_patterns, "exclude"),
                partials=_pattern_values(source_patterns, "partial"),
            )
        )
        value = {
            path: _analysis_signature(analysis, has_arcs=source_has_arcs)
            for path, analysis in analyses.items()
        }
        source_samples.append(elapsed)
        source_sample_signatures.append(_json_sha256(value))
        if value != source_signatures:
            raise RuntimeError("source analysis semantics changed after timing")
        return value

    for _ in range(sampling.warmups):
        measure_source()
    for _ in range(sampling.runs):
        measure_source()

    _, report_result, raw_paths = _sample_reports(
        args=args,
        root=root,
        raw=raw_root,
        data_file=args.data_file.resolve(),
        expected=expected_report,
        sampling=sampling,
    )

    if _source_digest(source_files, root) != source_digest_before:
        raise RuntimeError("prepared source changed during the static benchmark")
    require_clean(root)
    require_revision(root, prepared_state.revision)
    if _sha256(args.data_file.resolve()) != database_digest_before:
        raise RuntimeError("validated coverage database changed during the static benchmark")

    _recheck_prepared_artifact(
        args,
        project,
        selected_python=selected_python,
        root=root,
        preparation=preparation,
        selected=selected,
    )

    require_revision(repository, source_state_before.revision)
    result = {
        "schema": 1,
        "benchmark": "static-report",
        "project": {
            "key": args.project,
            "repository": project.repository,
            "revision": project.revision,
            "root": "$PROJECT",
            "source": [str(path.relative_to(root)) for path in source_files],
        },
        "methodology": {
            **sampling.as_dict(),
            "order": "rotating tool order",
            "stages": [
                "Source analysis through the vfy Python API over prepared files",
                "Coverage.py and vfy JSON report generation from the same database",
            ],
            "test_execution": "none; the database was prepared and validated before timing",
            "validation": (
                "exact source-analysis signatures and normalized report files/totals are checked "
                "after every timer"
            ),
            "publication_provenance": (
                "the prepared build manifest is verified before timing; its wheel and installed "
                "extension are rechecked after measurement"
            ),
        },
        "preparation": preparation,
        "database": database,
        "source_analysis": {
            "files": len(source_files),
            "analysis_patterns": source_patterns,
            "timing": summarize(source_samples[sampling.warmups :]),
            "signature_sha256": _json_sha256(source_signatures),
            "coverage_oracle_signature_sha256": _json_sha256(coverage_source_signatures),
            "sample_signature_sha256": source_sample_signatures[sampling.warmups :],
        },
        "reports": report_result,
        "raw_artifacts": raw_paths,
        "provenance": provenance,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"project": args.project, "source_files": len(source_files)}))
    return result


def main() -> int:
    args = parse_args()
    try:
        _run(args)
    except (RuntimeError, ValueError, OSError) as error:
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(
                    {
                        "benchmark": "static-report",
                        "project": args.project,
                        "validation": {"status": "failed", "error": str(error)},
                        "raw_artifacts": str(args.artifacts) if args.artifacts else None,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
