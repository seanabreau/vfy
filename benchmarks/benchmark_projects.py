"""Parity-gated pytest-cov benchmarks against real project checkouts.

Run the project's pytest command with each coverage plug-in in a separate
process. Retain commands, checkout revision, environment, and raw samples.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypedDict, cast

from coverage import CoverageData

# Direct execution starts with benchmarks/ on sys.path rather than the checkout
# root. Use one package-qualified import path in both direct and module execution.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import process_tree

from benchmarks.benchmark_harness import (
    benchmark_environment,
    rotated_orders,
    run_checked,
    summarize,
    timed as timed_process,
)
from benchmarks.benchmark_provenance import (
    JSONValue,
    git_value,
    prepared_project_provenance,
    require_clean,
    require_revision,
)
from benchmarks.observer_plugins import OUTCOME_PLUGIN, stage_plugins
from benchmarks.project_manifest import PROJECTS, ProjectManifest as Project


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", choices=sorted(PROJECTS), required=True)
    parser.add_argument(
        "--phase",
        choices=("parallel", "serial"),
        help="run one project phase (defaults to the phases in the project manifest)",
    )
    parser.add_argument("--root", type=Path, required=True, help="project checkout")
    parser.add_argument("--python", default=sys.executable, help="prepared project interpreter")
    parser.add_argument("--preparation-manifest", type=Path)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--runs", type=int, default=15)
    parser.add_argument(
        "--mode",
        choices=("publication", "diagnostic"),
        default="publication",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--vfy-revision", help="revision of an installed build from another checkout"
    )
    parser.add_argument("--artifacts", type=Path, help="retain parity reports, data and logs")
    parser.add_argument(
        "--resources",
        action="store_true",
        help="record Linux child peak RSS for each measured serial command",
    )
    parser.add_argument(
        "--tree-resources",
        action="store_true",
        help="record sampled simultaneous process-tree PSS for each serial command",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="run the semantic parity gate without collecting timing samples",
    )
    parser.add_argument(
        "--tests",
        nargs="+",
        help="test paths relative to the checkout (defaults to the project suite)",
    )
    parser.add_argument(
        "--pytest-args",
        default="",
        help="extra pytest arguments appended after the project defaults",
    )
    parser.add_argument(
        "--cov-config",
        type=Path,
        help="coverage config passed to both plug-ins (use /dev/null for CLI-only config)",
    )
    parser.add_argument(
        "--report",
        action="append",
        help="coverage report format for timed runs; repeat to replace the project defaults",
    )
    parser.add_argument(
        "--context",
        help="coverage context mode passed only to the two coverage plug-ins",
    )
    parser.add_argument(
        "--coverage-subprocess",
        action="store_true",
        help="derive a shared TOML config enabling subprocess coverage with project settings",
    )
    parser.add_argument(
        "--source",
        help="replace the project's coverage source with this package or path",
    )
    parser.add_argument(
        "--scope",
        help="qualify the manifest's suite scope when tests are selected or excluded",
    )
    parser.add_argument("--vfy-engine", choices=("pytest", "native"), default="pytest")
    parser.add_argument(
        "--build-manifest",
        type=Path,
        help="original retained release manifest for independent coverage",
    )
    return parser.parse_args()


def canonical_file(filename: str, root: Path) -> str:
    path = Path(filename)
    if not path.is_absolute():
        path = root / path
    try:
        path = path.resolve().relative_to(root.resolve())
    except ValueError:
        path = Path(os.path.normpath(filename))
    return path.as_posix()


class GeneratedFiles:
    """Restore reviewed test-generated files without touching other checkout state."""

    def __init__(self, root: Path, filenames: tuple[str, ...]) -> None:
        root = root.resolve()
        self._root = root
        paths: list[Path] = []
        for filename in filenames:
            relative = Path(filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"generated file escapes project root: {filename}")
            path = root / relative
            if not path.parent.resolve().is_relative_to(root):
                raise ValueError(f"generated file escapes project root: {filename}")
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError(f"generated path is not a regular file: {filename}")
            paths.append(path)
        self._snapshots = tuple(
            (
                path,
                path.read_bytes() if path.exists() else None,
                stat.S_IMODE(path.stat().st_mode) if path.exists() else None,
            )
            for path in paths
        )

    def restore(self) -> None:
        for path, _contents, _mode in self._snapshots:
            if not path.parent.resolve().is_relative_to(self._root):
                raise RuntimeError(f"generated file parent escaped project root: {path}")
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise RuntimeError(f"generated path became non-regular: {path}")
        for path, contents, mode in self._snapshots:
            if contents is None:
                path.unlink(missing_ok=True)
            else:
                if mode is None:
                    raise RuntimeError(f"generated file mode is missing: {path}")
                path.write_bytes(contents)
                path.chmod(mode)

    def metadata(self) -> list[dict[str, object]]:
        return [
            {
                "path": path.relative_to(self._root).as_posix(),
                "initial_state": "absent" if contents is None else "present",
                "sha256": None if contents is None else hashlib.sha256(contents).hexdigest(),
            }
            for path, contents, _mode in self._snapshots
        ]


def load_json(path: Path, root: Path) -> dict[str, dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    files = payload.get("files")
    if not isinstance(files, dict):
        raise RuntimeError(f"coverage JSON has no file mapping: {path}")
    normalized: dict[str, dict[str, object]] = {}
    original_names: dict[str, str] = {}
    for filename, value in files.items():
        if not isinstance(filename, str) or not isinstance(value, dict):
            raise RuntimeError(f"invalid coverage JSON entry in {path}")
        canonical = canonical_file(filename, root)
        if canonical in normalized:
            raise RuntimeError(
                f"coverage report filename collision after checkout normalization: "
                f"{original_names[canonical]!r} and {filename!r}"
            )
        original_names[canonical] = filename
        normalized[canonical] = value
    return normalized


def comparable_entries(value: object) -> set[int] | set[tuple[int, ...]]:
    if not isinstance(value, list):
        return set()
    if all(isinstance(entry, int) for entry in value):
        return {entry for entry in value if isinstance(entry, int)}
    branches: set[tuple[int, ...]] = set()
    for entry in value:
        if not isinstance(entry, list) or not all(isinstance(line, int) for line in entry):
            raise RuntimeError("coverage report contains a malformed line or branch")
        branches.add(tuple(line for line in entry if isinstance(line, int)))
    return branches


def command(
    python: str,
    plugin: str,
    project: Project,
    *,
    reports: tuple[str, ...],
    cov_config: Path | None,
    extra_args: tuple[str, ...],
    tests: tuple[str, ...],
    phase: str = "parallel",
    outcome_path: Path | None = None,
    append_coverage: bool = False,
    coverage_context: str | None = None,
    source_override: str | None = None,
) -> list[str]:
    project_plugins: list[str] = [part for name in project.plugins for part in ("-p", name)]
    phase_args = project.parallel_phase if phase == "parallel" else project.serial_phase
    args: list[str] = [
        python,
        "-m",
        "pytest",
        "-q",
        *project_plugins,
        *(["-p", OUTCOME_PLUGIN] if outcome_path is not None else []),
        "-p",
        plugin,
        *project.coverage_args,
    ]
    if phase == "parallel" and project.workers != 1 and "xdist.plugin" in project.plugins:
        args.extend(["-n", str(project.workers)])
    elif phase == "serial" and "xdist.plugin" in project.plugins:
        args.extend(["-n", str(project.serial_workers)])
    if cov_config is not None:
        args.append(f"--cov-config={cov_config}")
    if source_override is not None:
        args.extend(("--cov-reset", f"--cov={source_override}"))
    if coverage_context is not None:
        args.append(f"--cov-context={coverage_context}")
    if append_coverage:
        args.append("--cov-append")
    args.extend(
        [
            *(f"--cov-report={report}" for report in reports),
            *phase_args,
            *extra_args,
            *tests,
        ]
    )
    return args


def parity_report_arguments(
    project: Project, name: str, output_root: Path, requested_reports: tuple[str, ...] | None = None
) -> tuple[tuple[str, ...], Path | None]:
    """Build the machine-readable reports required by the parity gate.

    The JSON report remains the cross-project gate. XML is added when the
    project manifest requests it, with a separate output path for each engine.
    The gate compares JSON, requested XML, coverage data, and test outcomes;
    terminal text is outside its contract.
    """

    reports = [f"json:{output_root / f'.vfy-benchmark-{name}.json'}"]
    xml_output: Path | None = None
    formats = project.reports if requested_reports is None else requested_reports
    if any(report.partition(":")[0] == "xml" for report in formats):
        xml_output = output_root / f".vfy-benchmark-{name}.xml"
        reports.append(f"xml:{xml_output}")
    return tuple(reports), xml_output


def run(
    args: list[str],
    *,
    root: Path,
    environment: dict[str, str],
    capture_output: bool = False,
    log_path: Path | None = None,
    timeout: float = 1800,
) -> None:
    try:
        process = run_checked(args, cwd=root, env=environment, timeout=timeout)
    except RuntimeError as error:
        if log_path is not None:
            log_path.write_text(str(error), encoding="utf-8")
        raise
    except subprocess.TimeoutExpired as error:
        if log_path is not None:
            log_path.write_text(
                "".join(
                    output.decode("utf-8", errors="replace")
                    if isinstance(output, bytes)
                    else output or ""
                    for output in (error.stdout, error.stderr)
                ),
                encoding="utf-8",
            )
        raise
    if log_path is not None:
        log_path.write_text((process.stdout or "") + (process.stderr or ""), encoding="utf-8")


def timed(args: list[str], *, root: Path, environment: dict[str, str]) -> float:
    return timed_process(args, cwd=root, env=environment)


def plain_command(
    python: str,
    project: Project,
    *,
    phase: str,
    extra_args: tuple[str, ...],
    tests: tuple[str, ...],
    outcome_path: Path | None = None,
) -> list[str]:
    """Build the uninstrumented command for one recorded project phase."""

    args: list[str] = [
        python,
        "-m",
        "pytest",
        "-q",
        *[part for name in project.plugins for part in ("-p", name)],
        *(["-p", OUTCOME_PLUGIN] if outcome_path is not None else []),
    ]
    if phase == "parallel" and project.workers != 1 and "xdist.plugin" in project.plugins:
        args.extend(["-n", str(project.workers)])
    elif phase == "serial" and "xdist.plugin" in project.plugins:
        args.extend(["-n", str(project.serial_workers)])
    args.extend(
        [
            *(project.parallel_phase if phase == "parallel" else project.serial_phase),
            *extra_args,
            *tests,
        ]
    )
    return args


def sequence_commands(
    python: str,
    project: Project,
    *,
    plugin: str | None,
    reports: tuple[str, ...],
    cov_config: Path | None,
    extra_args: tuple[str, ...],
    tests: tuple[str, ...],
    phases: tuple[str, ...] | None = None,
    outcome_path: Path | None = None,
    coverage_context: str | None = None,
    source_override: str | None = None,
) -> list[tuple[str, list[str]]]:
    """Build the complete developer command sequence for one tool."""

    commands: list[tuple[str, list[str]]] = []
    selected_phases = phases or project.phases
    for index, phase in enumerate(selected_phases):
        if plugin is None:
            command_args = plain_command(
                python,
                project,
                phase=phase,
                extra_args=extra_args,
                tests=tests,
                outcome_path=outcome_path,
            )
        else:
            command_args = command(
                python,
                plugin,
                project,
                reports=reports,
                cov_config=cov_config,
                extra_args=extra_args,
                tests=tests,
                phase=phase,
                append_coverage=index > 0,
                outcome_path=outcome_path,
                coverage_context=coverage_context,
                source_override=source_override,
            )
        commands.append((phase, command_args))
    return commands


def timed_sequence(
    commands: list[tuple[str, list[str]]],
    *,
    root: Path,
    environment: dict[str, str],
    coverage_file: Path | None = None,
    outcome_file: Path | None = None,
) -> tuple[float, dict[str, float]]:
    """Time one complete developer sequence and retain each phase duration."""

    if coverage_file is not None:
        coverage_file.unlink(missing_ok=True)
    if outcome_file is not None:
        outcome_file.unlink(missing_ok=True)
    child_environment = dict(environment)
    if outcome_file is not None:
        child_environment["VFY_BENCHMARK_OUTCOMES"] = str(outcome_file)
    started = time.perf_counter_ns()
    steps: dict[str, float] = {}
    for phase, command_args in commands:
        step_started = time.perf_counter_ns()
        run(command_args, root=root, environment=child_environment)
        steps[phase] = (time.perf_counter_ns() - step_started) / 1_000_000
    return (time.perf_counter_ns() - started) / 1_000_000, steps


def resource_sequence(
    commands: list[tuple[str, list[str]]],
    *,
    root: Path,
    environment: dict[str, str],
    record_path: Path,
    coverage_file: Path | None = None,
    outcome_file: Path | None = None,
    tree: bool = False,
) -> tuple[float, dict[str, float], dict[str, object]]:
    """Measure one serial child with the existing Linux kernel resource collector."""

    if len(commands) != 1 or commands[0][0] != "serial":
        raise ValueError("resource measurement requires one serial pytest command")
    if sys.platform != "linux":
        raise RuntimeError("resource measurement requires Linux")
    if coverage_file is not None:
        coverage_file.unlink(missing_ok=True)
    if outcome_file is not None:
        outcome_file.unlink(missing_ok=True)
    child_environment = dict(environment)
    if outcome_file is not None:
        child_environment["VFY_BENCHMARK_OUTCOMES"] = str(outcome_file)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    collector = (
        Path(__file__).resolve().parents[1]
        / "validation/pytest_runner/benchmark_child_resources.py"
    )
    argv = [
        sys.executable,
        str(collector),
        *(["--tree", "--sample-interval-ms", "20"] if tree else []),
        "--record",
        str(record_path),
        "--stdout",
        str(record_path.with_suffix(".stdout")),
        "--stderr",
        str(record_path.with_suffix(".stderr")),
        "--",
        *commands[0][1],
    ]
    run(argv, root=root, environment=child_environment)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise RuntimeError("resource record is not an object")
    milliseconds = record.get("child_wall_ms")
    peak_bytes = record.get("peak_rss_bytes")
    method = "linux-process-tree-smaps-rollup" if tree else "linux-single-child-rusage"
    if (
        record.get("method") != method
        or record.get("child_command") != commands[0][1]
        or record.get("child_exit_status") != 0
        or type(milliseconds) not in (int, float)
        or milliseconds <= 0
        or type(peak_bytes) is not int
        or peak_bytes <= 0
    ):
        raise RuntimeError("resource record does not describe the completed serial command")
    if tree and (
        type(record.get("peak_tree_pss_bytes")) is not int
        or record["peak_tree_pss_bytes"] <= 0
        or type(record.get("peak_tree_rss_bytes")) is not int
        or record["peak_tree_rss_bytes"] <= 0
        or type(record.get("snapshot_count")) is not int
        or record["snapshot_count"] < 1
    ):
        raise RuntimeError("tree resource record has no valid process-tree snapshots")
    return float(milliseconds), {"serial": float(milliseconds)}, cast(dict[str, object], record)


class ContextMeasurements(TypedDict):
    lines: list[int]
    arcs: list[tuple[int, int]]


class FileMeasurements(ContextMeasurements):
    tracer: str | None
    contexts: dict[int, list[str]]


class CoverageSnapshot(TypedDict):
    has_arcs: bool
    contexts: list[str]
    by_context: dict[str, dict[str, ContextMeasurements]]
    files: dict[str, FileMeasurements]


def validate_sample(
    name: str,
    *,
    root: Path,
    outcome_file: Path,
    expected_outcomes: list[tuple[str, str, str, str | None]],
    coverage_file: Path | None,
    expected_data: CoverageSnapshot,
    stage: str,
) -> None:
    """Reject a timed command whose observable result differs from the parity gate."""

    if sorted(outcome_records(outcome_file)) != expected_outcomes:
        raise RuntimeError(f"{name}: {stage} changed test identities or outcomes")
    if coverage_file is not None:
        actual_data = normalized_data(coverage_file, root)
        if actual_data != expected_data:
            difference = first_schema7_difference(expected_data, actual_data)
            raise RuntimeError(f"{name}: {stage} changed schema-7 coverage data: {difference}")


def first_schema7_difference(expected: object, actual: object, path: str = "schema7") -> str:
    """Identify the first changed field of a normalized schema-7 snapshot."""

    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(expected.keys() | actual.keys(), key=str):
            field = f"{path}[{key!r}]"
            if key not in expected:
                return f"{field}: only in observed data"
            if key not in actual:
                return f"{field}: missing from observed data"
            if expected[key] != actual[key]:
                return first_schema7_difference(expected[key], actual[key], field)
    elif isinstance(expected, list) and isinstance(actual, list):
        for index, (left, right) in enumerate(zip(expected, actual, strict=False)):
            if left != right:
                return first_schema7_difference(left, right, f"{path}[{index}]")
    return f"{path}: expected {str(expected)[:160]!r}, observed {str(actual)[:160]!r}"


def retain_parity_snapshot(
    artifacts: Path,
    data: CoverageSnapshot,
    outcomes: list[tuple[str, str, str, str | None]],
) -> Path:
    """Keep gate evidence separate from raw files reused by timed commands."""

    snapshot = artifacts / "parity-snapshot"
    suffix = 2
    while snapshot.exists():
        snapshot = artifacts / f"parity-snapshot-{suffix}"
        suffix += 1
    snapshot.mkdir(exist_ok=False)
    for path in artifacts.iterdir():
        if path.is_file():
            shutil.copy2(path, snapshot / path.name)
    (snapshot / "schema7.json").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (snapshot / "outcomes.json").write_text(json.dumps(outcomes, indent=2) + "\n", encoding="utf-8")
    return snapshot


def outcome_records(path: Path) -> list[tuple[str, str, str, str | None]]:
    """Read and validate full setup/call/teardown outcomes."""

    subtest_marker = "::[subtest]"
    if not path.is_file():
        raise RuntimeError(f"pytest outcome evidence is missing: {path}")
    records: list[tuple[str, str, str, str | None]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, dict) or not isinstance(value.get("nodeid"), str):
            raise RuntimeError(f"invalid pytest outcome record in {path}")
        when = value.get("when")
        outcome = value.get("outcome")
        if when not in {"setup", "call", "teardown"} or outcome not in {
            "passed",
            "failed",
            "skipped",
        }:
            raise RuntimeError(f"pytest outcome is missing in {path}")
        wasxfail = value.get("wasxfail")
        if wasxfail is not None and not isinstance(wasxfail, str):
            raise RuntimeError(f"invalid xfail marker in {path}")
        records.append((value["nodeid"], when, outcome, wasxfail))
    if not records:
        raise RuntimeError(f"pytest produced no executed test outcomes: {path}")
    per_node: dict[str, dict[str, tuple[str, str | None]]] = {}
    for nodeid, when, outcome, wasxfail in records:
        phases = per_node.setdefault(nodeid, {})
        if when in phases:
            raise RuntimeError(f"duplicate pytest {when} evidence for {nodeid!r} in {path}")
        phases[when] = (outcome, wasxfail)
    incomplete = sorted(
        nodeid
        for nodeid, phases in per_node.items()
        if (
            (subtest_marker in nodeid and set(phases) != {"call"})
            or (
                subtest_marker not in nodeid
                and set(phases) != {"setup", "call", "teardown"}
                and not (set(phases) == {"setup", "teardown"} and phases["setup"][0] == "skipped")
            )
        )
    )
    if incomplete:
        raise RuntimeError(
            f"pytest protocol did not produce setup/call/teardown evidence for {incomplete[:3]!r}"
        )
    invalid_skip = sorted(
        nodeid
        for nodeid, phases in per_node.items()
        if phases.get("setup", (None, None))[0] == "skipped" and "call" in phases
    )
    if invalid_skip:
        raise RuntimeError(
            f"pytest protocol contains a call after skipped setup: {invalid_skip[:3]!r}"
        )
    return sorted(records)


def normalized_data(path: Path, root: Path) -> CoverageSnapshot:
    """Use the independent schema reader, including arcs and context attribution."""

    if not path.is_file():
        raise RuntimeError(f"coverage data is missing: {path}")
    data = CoverageData(str(path))
    data.read()
    filenames = sorted(data.measured_files())
    contexts = sorted(data.measured_contexts())
    file_contexts = {filename: data.contexts_by_lineno(filename) for filename in filenames}
    context_files: dict[str, set[str]] = {context: set() for context in contexts}
    for filename, line_contexts in file_contexts.items():
        for names in line_contexts.values():
            for context in names:
                context_files[context].add(filename)
        # Entry/exit-only arcs have no positive line for contexts_by_lineno.
        # Preserve their attribution with the exhaustive query for this file.
        if any(start <= 0 and end <= 0 for start, end in data.arcs(filename) or ()):
            for context in contexts:
                context_files[context].add(filename)
    by_context: dict[str, dict[str, ContextMeasurements]] = {}
    for context in contexts:
        data.set_query_context(context)
        by_context[context] = {
            canonical_file(filename, root): {
                "lines": sorted(data.lines(filename) or ()),
                "arcs": sorted(data.arcs(filename) or ()),
            }
            for filename in sorted(context_files[context])
        }
        by_context[context] = {
            filename: values
            for filename, values in by_context[context].items()
            if values["lines"] or values["arcs"]
        }
    data.set_query_contexts(None)
    return {
        "has_arcs": data.has_arcs(),
        "contexts": contexts,
        "by_context": by_context,
        "files": {
            canonical_file(filename, root): {
                "lines": sorted(data.lines(filename) or ()),
                "arcs": sorted(data.arcs(filename) or ()),
                "tracer": data.file_tracer(filename),
                "contexts": {
                    line: sorted(contexts) for line, contexts in file_contexts[filename].items()
                },
            }
            for filename in filenames
        },
    }


# Keep the cross-engine gate in one transaction so a failed phase cannot be
# mistaken for a partially validated report.
def validate_parity(  # noqa: C901
    root: Path,
    project: Project,
    *,
    python: str,
    cov_config: Path | None,
    extra_args: tuple[str, ...],
    tests: tuple[str, ...],
    environment: dict[str, str],
    artifacts: Path | None = None,
    phases: tuple[str, ...] | None = None,
    restore_generated_files: Callable[[], None] | None = None,
    reports: tuple[str, ...] | None = None,
    coverage_context: str | None = None,
    source_override: str | None = None,
) -> tuple[CoverageSnapshot, list[tuple[str, str, str, str | None]]]:
    output_root = artifacts or root
    output_root.mkdir(parents=True, exist_ok=True)
    outputs = {
        "pytest-cov": output_root / ".vfy-benchmark-pytest-cov.json",
        "vfy": output_root / ".vfy-benchmark-vfy.json",
    }
    report_arguments = {
        name: parity_report_arguments(project, name, output_root, reports) for name in outputs
    }
    xml_outputs = {
        name: xml_output
        for name, (_reports, xml_output) in report_arguments.items()
        if xml_output is not None
    }
    files: dict[str, dict[str, dict[str, object]]] = {}
    data: dict[str, CoverageSnapshot] = {}
    totals: dict[str, object] = {}
    outcomes: dict[str, list[tuple[str, str, str, str | None]]] = {}
    xml_reports: dict[str, str] = {}
    observer_temporary = (
        tempfile.TemporaryDirectory(prefix="vfy-benchmark-observers-")
        if artifacts is None
        else None
    )
    try:
        if artifacts is None:
            assert observer_temporary is not None
            observer_parent = Path(observer_temporary.name)
        else:
            observer_parent = artifacts
        observer_root = stage_plugins(observer_parent / "observer-plugins")
        for name, plugin in (
            ("pytest-cov", "pytest_cov.plugin"),
            ("vfy", "vfy.pytest_plugin"),
        ):
            if restore_generated_files is not None:
                restore_generated_files()
            output = outputs[name]
            xml_output = xml_outputs.get(name)
            data_file = output_root / f".vfy-benchmark-{name}"
            data_file.unlink(missing_ok=True)
            name_outcomes: list[tuple[str, str, str, str | None]] = []
            selected_phases = phases or project.phases
            for index, phase in enumerate(selected_phases):
                # Coverage append looks for shards with the database's filename prefix.
                # Regenerate the reports after the combine so they cannot match that prefix.
                output.unlink(missing_ok=True)
                if xml_output is not None:
                    xml_output.unlink(missing_ok=True)
                outcome_path = output_root / f"{name}-{phase}-outcomes.jsonl"
                outcome_path.unlink(missing_ok=True)
                env = {
                    **environment,
                    "COVERAGE_FILE": str(data_file),
                    "VFY_BENCHMARK_OUTCOMES": str(outcome_path),
                }
                python_path = str(observer_root)
                existing_python_path = env.get("PYTHONPATH")
                env["PYTHONPATH"] = (
                    python_path
                    if not existing_python_path
                    else python_path + os.pathsep + existing_python_path
                )
                run(
                    command(
                        python,
                        plugin,
                        project,
                        reports=report_arguments[name][0],
                        cov_config=cov_config,
                        extra_args=extra_args,
                        tests=tests,
                        phase=phase,
                        outcome_path=outcome_path,
                        coverage_context=coverage_context,
                        source_override=source_override,
                        append_coverage=index > 0,
                    ),
                    root=root,
                    environment=env,
                    capture_output=True,
                    log_path=artifacts / f"{name}-{phase}.log" if artifacts else None,
                )
                name_outcomes.extend(outcome_records(outcome_path))
            if xml_output is not None:
                try:
                    xml_reports[name] = xml_output.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as error:
                    raise RuntimeError(
                        f"{name}: requested XML report is missing or unreadable: {xml_output}"
                    ) from error
            files[name] = load_json(output, root)
            data[name] = normalized_data(data_file, root)
            totals[name] = json.loads(output.read_text(encoding="utf-8"))["totals"]
            outcomes[name] = sorted(name_outcomes)
            if not any(
                filename.startswith(project.package_path) and value.get("executed_lines")
                for filename, value in files[name].items()
            ):
                raise RuntimeError(f"{name}: no executed package files in {project.package_path}")
    finally:
        if observer_temporary is not None:
            observer_temporary.cleanup()
        if restore_generated_files is not None:
            restore_generated_files()
        if artifacts is None:
            for output in outputs.values():
                output.unlink(missing_ok=True)
            for output in xml_outputs.values():
                output.unlink(missing_ok=True)

    reference = files["pytest-cov"]
    actual = files["vfy"]
    if reference.keys() != actual.keys():
        reference_files = set(reference)
        actual_files = set(actual)
        raise RuntimeError(
            "coverage measured-file sets differ: "
            f"pytest-cov only ({len(reference_files - actual_files)})="
            f"{sorted(reference_files - actual_files)[:20]!r}, "
            f"vfy only ({len(actual_files - reference_files)})="
            f"{sorted(actual_files - reference_files)[:20]!r}"
        )
    for filename in reference:
        for field in (
            "executed_lines",
            "missing_lines",
            "excluded_lines",
            "executed_branches",
            "missing_branches",
        ):
            expected = reference[filename].get(field)
            observed = actual[filename].get(field)
            if expected != observed:
                expected_set = comparable_entries(expected)
                observed_set = comparable_entries(observed)
                raise RuntimeError(
                    f"coverage {field} differs in {filename}: "
                    f"pytest-cov only={sorted(expected_set - observed_set)!r}, "
                    f"vfy only={sorted(observed_set - expected_set)!r}"
                )

    if data["pytest-cov"] != data["vfy"]:
        if artifacts is not None:
            for name, value in data.items():
                (artifacts / f"{name}-normalized.json").write_text(
                    json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
        raise RuntimeError("schema-7 lines, arcs or contexts differ; inspect retained data files")
    if totals["pytest-cov"] != totals["vfy"]:
        raise RuntimeError(f"report totals differ: {totals!r}")
    if xml_outputs:
        if xml_reports.keys() != outputs.keys():
            raise RuntimeError("requested XML reports are missing for one or more engines")
        # Loading project report normalizers must not import a candidate extension.
        from benchmarks.benchmark_report import assert_xml_report_parity  # noqa: PLC0415

        assert_xml_report_parity(xml_reports["pytest-cov"], xml_reports["vfy"])
    if outcomes["pytest-cov"] != outcomes["vfy"]:
        raise RuntimeError(
            "pytest executed test identities or outcomes differ: "
            "pytest-cov only="
            f"{sorted(set(outcomes['pytest-cov']) - set(outcomes['vfy']))[:10]!r}, "
            f"vfy only={sorted(set(outcomes['vfy']) - set(outcomes['pytest-cov']))[:10]!r}"
        )
    if any(outcome == "failed" for _nodeid, _when, outcome, _wasxfail in outcomes["pytest-cov"]):
        raise RuntimeError("full-suite validation contains a non-passing test outcome")
    return data["pytest-cov"], outcomes["pytest-cov"]


def selected_interpreter_metadata(
    interpreter: Path,
    *,
    root: Path,
    environment: dict[str, str],
) -> dict[str, object]:
    """Read package and interpreter identity from the executable under test."""

    script = (
        "import hashlib, importlib.metadata as m, json, platform, sys; "
        "from pathlib import Path; import vfy._native as n; "
        "names=('vfy','pytest-cov'); "
        "native=Path(n.__file__).resolve(); "
        "print(json.dumps({'executable': sys.executable, 'prefix': sys.prefix, "
        "'base_prefix': sys.base_prefix, 'python': platform.python_version(), "
        "'implementation': platform.python_implementation(), "
        "'vfy': m.version('vfy'), 'pytest_cov': m.version('pytest-cov'), "
        "'extension_path': str(native), "
        "'extension_sha256': hashlib.sha256(native.read_bytes()).hexdigest(), "
        "'build_profile': __import__('vfy.native', "
        "fromlist=['build_profile']).build_profile()}))"
    )
    completed = run_checked([str(interpreter), "-c", script], cwd=root, env=environment, timeout=30)
    value = cast(dict[str, object], json.loads(completed.stdout))
    if not isinstance(value, dict):
        raise RuntimeError("selected interpreter metadata is not an object")
    expected_prefix = interpreter.parent.parent
    actual_prefix = Path(str(value.get("prefix", "")))
    if actual_prefix.resolve() != expected_prefix.resolve():
        raise RuntimeError(
            f"selected interpreter escaped its environment: {value.get('prefix')!r} != "
            f"{str(expected_prefix)!r}"
        )
    return value


def preparation_metadata(
    interpreter: Path,
    checkout: Path,
    *,
    manifest_path: Path | None,
    build_manifest: Path | None = None,
) -> dict[str, object]:
    """Load and verify the manifest and wheel evidence made during preparation."""

    if manifest_path is None:
        destination = interpreter.parent.parent.parent
        candidates = sorted(destination.glob("artifacts/manifest.json"))
        manifest_path = candidates[0] if candidates else destination / "artifacts/manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"preparation manifest is missing: {manifest_path}")
    value = cast(dict[str, object], json.loads(manifest_path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise RuntimeError(f"preparation manifest is not an object: {manifest_path}")
    checkout_revision = git_value(checkout, "rev-parse", "HEAD")
    if value.get("checked_out_revision") != checkout_revision:
        raise RuntimeError("preparation manifest checkout revision does not match the project")
    build_value = value.get("vfy_build_manifest")
    if build_value is not None and not isinstance(build_value, str):
        raise RuntimeError("preparation manifest has an invalid vfy build manifest path")
    build_path = build_manifest or (
        Path(build_value)
        if build_value is not None
        else manifest_path.parent.parent / "vfy-build.json"
    )
    if not build_path.is_file():
        raise RuntimeError(f"vfy build manifest is missing: {build_path}")
    wheel = cast(dict[str, object], json.loads(build_path.read_text(encoding="utf-8")))
    if not isinstance(wheel, dict) or not wheel.get("wheel_sha256"):
        raise RuntimeError(f"vfy build manifest is incomplete: {build_path}")
    wheel_path_value = wheel.get("wheel_path")
    wheel_hash = wheel.get("wheel_sha256")
    if not isinstance(wheel_path_value, str) or not isinstance(wheel_hash, str):
        raise RuntimeError(f"vfy build manifest has no wheel binding: {build_path}")
    wheel_path = Path(wheel_path_value)
    if not wheel_path.is_file() or (
        hashlib.sha256(wheel_path.read_bytes()).hexdigest() != wheel_hash
    ):
        raise RuntimeError("prepared vfy wheel does not match its build manifest")
    source_root_value = wheel.get("source_root")
    source_inputs = wheel.get("source_inputs") if build_manifest is None else {}
    if not isinstance(source_root_value, str) or not isinstance(source_inputs, dict):
        raise RuntimeError("vfy build manifest has no source input bindings")
    source_root = Path(source_root_value).resolve()
    for relative, digest in source_inputs.items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise RuntimeError("vfy build manifest has malformed source input bindings")
        source_file = (source_root / relative).resolve()
        if not source_file.is_file() or (
            hashlib.sha256(source_file.read_bytes()).hexdigest() != digest
        ):
            raise RuntimeError(f"vfy build input changed after preparation: {relative}")
    value["manifest"] = str(manifest_path)
    value["vfy_wheel"] = wheel
    return value


def verify_prepared_lock(prepared: Mapping[str, object]) -> Path:
    """Verify the selected resolved lock, including historical relative paths."""
    value = prepared.get("dependency_lock")
    if not isinstance(value, str):
        raise RuntimeError("prepared resolved dependency lock is missing")
    lock = Path(value)
    if not lock.is_absolute():
        lock = Path(__file__).resolve().parents[1] / lock
    if (
        not lock.is_file()
        or prepared.get("dependency_lock_sha256") != hashlib.sha256(lock.read_bytes()).hexdigest()
    ):
        raise RuntimeError("prepared resolved dependency lock differs from its recorded digest")
    return lock


def workload_scope(
    project: Project,
    *,
    selected_tests: bool,
    extra_args: tuple[str, ...],
    qualification: str | None,
) -> str:
    """Keep test selection visible in every published workload label."""

    scope = "selected tests" if selected_tests else project.scope
    if qualification:
        return f"{scope}; {qualification}"
    if selected_tests or any(arg in {"-k", "-m", "--deselect"} for arg in extra_args):
        return f"{scope}; pytest selection: {shlex.join(extra_args)}"
    return scope


def write_partial_result(
    output: Path,
    metadata: dict[str, object],
    *,
    warmups_requested: int,
    warmups_completed: int,
    runs_requested: int,
    samples: dict[str, list[float]],
    step_samples: dict[str, dict[str, list[float]]],
) -> Path:
    """Replace the partial-results file with completed samples, marked incomplete."""
    path = output.with_suffix(".partial.json")
    payload = {
        **metadata,
        "status": "incomplete",
        "diagnostic": True,
        "publication_ready": False,
        "sampling": {
            "warmups_requested": warmups_requested,
            "warmups_completed": warmups_completed,
            "measured_runs_requested_per_tool": runs_requested,
            "completed_measured_runs_by_tool": {
                name: len(values) for name, values in samples.items()
            },
        },
        "raw_samples_ms": {name: list(values) for name, values in samples.items()},
        "step_samples_ms": {
            name: {phase: list(values) for phase, values in phases.items()}
            for name, phases in step_samples.items()
        },
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _independent_coverage_command(
    name: str,
    arguments: list[str],
    destination: Path,
    *,
    preflight: bool,
) -> list[str]:
    """Give the selected serial frontend its own output paths."""
    argv = list(arguments)
    # Each attempt owns its outputs; preserve the selected report formats.
    for index, argument in enumerate(argv):
        if argument.startswith("--cov-report="):
            report = argument.partition("=")[2].partition(":")[0]
            if report in {"json", "xml"}:
                argv[index] = f"--cov-report={report}:{destination / ('coverage.' + report)}"
    if name == "vfy-independent":
        index = argv.index("vfy.pytest_plugin")
        if argv[index - 1] != "-p" or argv[1:3] != ["-m", "pytest"]:
            raise ValueError("independent coverage requires the vfy pytest-plugin command")
        del argv[index - 1 : index + 1]
        prefix = [
            argv[0],
            "-m",
            "vfy",
            "test",
            "--engine=native",
            "--workers=1",
            "--history",
            str(destination / "history.json"),
        ]
        if preflight:
            prefix.append(f"--native-result-json={destination / 'independent.json'}")
        argv = [*prefix, *argv[3:]]
    argv += [f"--junitxml={destination / 'junit.xml'}", "--basetemp", str(destination / "tmp")]
    return argv


def _validate_coverage_reports(
    name: str,
    root: Path,
    destination: Path,
    reference_report: Path,
) -> None:
    """Compare coverage file entries, totals, and requested XML after the child exits."""
    expected_totals = json.loads(reference_report.read_text())["totals"]
    report = destination / "coverage.json"
    if (
        load_json(report, root) != load_json(reference_report, root)
        or json.loads(report.read_text())["totals"] != expected_totals
    ):
        raise RuntimeError(f"{name}: JSON coverage report changed")
    reference_xml = reference_report.with_suffix(".xml")
    if reference_xml.is_file():
        # The report module imports the native extension; load it only for requested XML.
        from benchmarks.benchmark_report import assert_xml_report_parity  # noqa: PLC0415

        assert_xml_report_parity(
            reference_xml.read_text(), (destination / "coverage.xml").read_text()
        )


def _independent_coverage_proof(
    destination: Path,
    root: Path,
    python: Path,
    reference_report: Path,
    expected_outcomes: list[tuple[str, str, str, str | None]],
) -> dict[str, object]:
    """Bind an actual independent execution to the public phase inventory."""
    # The experiment parser is needed only for an owned-engine preflight.
    from validation.pytest_runner.benchmark_native_xdist import (  # noqa: PLC0415
        Route,
        independent_execution,
    )

    raw = json.loads((destination / "independent.json").read_text())
    if raw.get("internal_errors", []) != []:
        raise RuntimeError("independent coverage preflight has internal errors")
    execution = independent_execution(
        destination,
        root,
        python,
        destination / "tmp",
        Route("native", 1, False),
    )
    # Coverage attribution requires unique executable node IDs.
    # Reject duplicates rather than collapsing them into this set.
    observed = {case["nodeid"] for case in execution["cases"]}
    expected = {nodeid.split("::[subtest]", 1)[0] for nodeid, *_rest in expected_outcomes}
    if observed != expected or len(execution["cases"]) != len(expected):
        raise RuntimeError("independent engine inventory differs from public phases")
    path = destination / "independent.json"
    return {
        "passed": True,
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "report_sha256": hashlib.sha256(reference_report.read_bytes()).hexdigest(),
    }


def independent_coverage_attempt(
    name: str,
    commands: list[tuple[str, list[str]]],
    *,
    root: Path,
    environment: dict[str, str],
    destination: Path,
    expected_data: CoverageSnapshot,
    expected_outcomes: list[tuple[str, str, str, str | None]],
    reference_report: Path,
    expected_junit: list[dict[str, object]] | None,
    preflight: bool,
    execution_proof: dict[str, object] | None,
    records: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Retain one opt-in serial command and reject changed public observations."""
    # Import lazily so routes outside the independent engine skip the experiment helpers.
    from validation.pytest_runner.check_native_xdist_adoption import junit_records  # noqa: PLC0415

    if len(commands) != 1 or commands[0][0] != "serial":
        raise ValueError("independent coverage requires one serial command")
    if not preflight:
        if execution_proof is None or (
            execution_proof.get("passed") is not True
            or execution_proof.get("preflight_passed") is not True
        ):
            raise ValueError("independent coverage requires successful full preflight proof")
        proof_path = Path(str(execution_proof["path"]))
        if hashlib.sha256(proof_path.read_bytes()).hexdigest() != execution_proof["sha256"]:
            raise ValueError("independent coverage preflight proof changed")
        if (
            hashlib.sha256(reference_report.read_bytes()).hexdigest()
            != execution_proof["report_sha256"]
        ):
            raise ValueError("independent coverage reference report changed")
    destination.mkdir(parents=True, exist_ok=False)
    argv = _independent_coverage_command(name, commands[0][1], destination, preflight=preflight)
    env = {
        **environment,
        "COVERAGE_FILE": str(destination / ".coverage"),
        "VFY_BENCHMARK_OUTCOMES": str(destination / "outcomes.jsonl"),
    }
    record: dict[str, object] = {
        "command": argv,
        "route": name,
        "preflight": preflight,
        "status": "running",
        "directory": str(destination),
    }
    if records is not None:
        records.append(record)
    (destination / "command.json").write_text(json.dumps(record, indent=2) + "\n")
    try:
        started = time.perf_counter_ns()
        completed = process_tree.run_captured(argv, cwd=root, env=env, timeout=1800)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        (destination / "stdout.log").write_text(completed.stdout or "")
        (destination / "stderr.log").write_text(completed.stderr or "")
        record.update(milliseconds=elapsed, exitstatus=completed.returncode)
        if completed.returncode != 0:
            raise RuntimeError(f"{name}: coverage command exited {completed.returncode}")
        cases = cast(
            list[dict[str, object]],
            junit_records(destination / "junit.xml", root, destination / "tmp"),
        )
        junit_bytes = (json.dumps(cases, sort_keys=True) + "\n").encode()
        record.update(junit_cases=len(cases), junit_sha256=hashlib.sha256(junit_bytes).hexdigest())
        if expected_junit is None:
            reference = destination / "junit-reference.json"
            reference.write_bytes(junit_bytes)
            record["junit_reference"] = str(reference)
        if not cases or (expected_junit is not None and cases != expected_junit):
            raise RuntimeError(f"{name}: JUnit testcase multiset changed")
        validate_sample(
            name,
            root=root,
            outcome_file=destination / "outcomes.jsonl",
            expected_outcomes=expected_outcomes,
            coverage_file=None if name == "plain pytest" else destination / ".coverage",
            expected_data=expected_data,
            stage="preflight" if preflight else "sample",
        )
        if name != "plain pytest":
            _validate_coverage_reports(name, root, destination, reference_report)
        if name == "vfy-independent" and preflight:
            record["execution_proof"] = _independent_coverage_proof(
                destination,
                root,
                Path(argv[0]),
                reference_report,
                expected_outcomes,
            )
        record["status"] = "passed"
    except BaseException as error:
        if isinstance(error, subprocess.TimeoutExpired):
            for stream, output in (("stdout", error.stdout), ("stderr", error.stderr)):
                text = (
                    output.decode("utf-8", errors="replace")
                    if isinstance(output, bytes)
                    else output or ""
                )
                (destination / f"{stream}.log").write_text(text)
        record.update(status="failed", error=str(error))
        raise
    finally:
        (destination / "validation.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def independent_coverage_artifact(
    python: Path,
    root: Path,
    environment: dict[str, str],
    manifest: Path,
) -> dict[str, object]:
    """Verify the retained release and every installed vfy payload outside timers."""
    # This verifier imports normalized_data from this module, so defer it until initialization.
    from validation.pytest_runner.benchmark_vfy_coverage import (  # noqa: PLC0415
        interpreter_details,
        verify_build_identity,
        verify_installed_distribution_files,
    )

    build = json.loads(manifest.read_text())
    if (
        build.get("status") != "passed"
        or build.get("source_clean") is not True
        or build.get("source_unchanged") is not True
    ):
        raise RuntimeError("independent coverage requires an unchanged successful release build")
    wheel = Path(build["wheel_path"])
    installed = interpreter_details(python, root, environment)
    installed["build_identity"] = verify_build_identity(manifest, installed, wheel, retained=True)
    installed["build_manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    verify_installed_distribution_files("independent coverage", "vfy", installed, wheel)
    return installed


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="vfy-benchmark-observers-") as temporary:
        return _run_project_benchmark(parse_args(), Path(temporary))


def _run_project_benchmark(args: argparse.Namespace, observer_temporary: Path) -> int:  # noqa: C901 - one transaction owns all parity evidence
    independent = getattr(args, "vfy_engine", "pytest") == "native"
    build_manifest = getattr(args, "build_manifest", None)
    if args.warmups < 0 or args.runs < 1:
        raise SystemExit("project benchmarks require nonnegative warmups and positive runs")
    if (
        not args.validate_only
        and args.mode == "publication"
        and (args.warmups < 3 or args.runs < 15)
    ):
        raise SystemExit("published project benchmarks require at least 3 warmups and 15 runs")
    if not args.validate_only and args.output is None:
        raise SystemExit("published project benchmarks require --output")
    root = args.root.resolve()
    project = PROJECTS[args.project]
    phases = (args.phase,) if args.phase else project.phases
    if args.resources and args.tree_resources:
        raise SystemExit("--resources and --tree-resources are mutually exclusive")
    if (args.resources or args.tree_resources) and (
        phases != ("serial",) or args.artifacts is None
    ):
        raise SystemExit("resource measurement requires --phase serial and --artifacts")
    if independent and (
        phases != ("serial",)
        or args.artifacts is None
        or build_manifest is None
        or args.context != "test"
        or args.resources
        or args.tree_resources
        or "xdist.plugin" in project.plugins
    ):
        raise SystemExit(
            "independent coverage requires --phase serial, --context=test, "
            "--artifacts, --build-manifest and no xdist/resources"
        )
    if not independent and build_manifest is not None:
        raise SystemExit("--build-manifest is only supported for independent coverage")
    if (
        independent
        and args.output is not None
        and (args.output.exists() or args.output.with_suffix(".partial.json").exists())
    ):
        raise SystemExit("independent coverage output already exists")
    generated_files = GeneratedFiles(root, project.generated_files)
    cov_config = args.cov_config
    if cov_config is None and project.key == "rich":
        cov_config = Path(__file__).resolve().parent / "configs/rich.coveragerc"
    extra_args = tuple(shlex.split(args.pytest_args))
    if independent and any(arg.startswith("--cov-context") for arg in extra_args):
        raise SystemExit("set independent coverage contexts only with --context=test")
    reports = tuple(args.report) if args.report else project.reports
    if independent and any(
        arg == "-n" or arg.startswith(("--numprocesses", "--dist", "-n")) or "xdist" in arg
        for arg in extra_args
    ):
        raise SystemExit("independent coverage requires serial execution without xdist overrides")
    if independent and (
        sum(report.partition(":")[0] == "json" for report in reports) != 1
        or any(
            report.partition(":")[0] not in {"json", "xml", "term", "term-missing"}
            for report in reports
        )
    ):
        raise SystemExit(
            "independent coverage requires one JSON report and only JSON/XML/terminal formats"
        )
    tests = tuple(args.tests) if args.tests else project.tests
    environment = benchmark_environment()
    environment.update(project.environment)
    if project.plugins:
        # Nested pytest sessions in upstream plugin tests inherit this contract
        # while autoload remains disabled for the top-level command.
        environment["PYTEST_PLUGINS"] = ",".join(project.plugins)
    interpreter_path = Path(args.python).absolute()
    if not interpreter_path.is_file():
        raise SystemExit(f"selected project interpreter does not exist: {interpreter_path}")
    environment["PATH"] = str(interpreter_path.parent) + os.pathsep + environment.get("PATH", "")
    environment["VIRTUAL_ENV"] = str(interpreter_path.parent.parent)
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    artifacts: Path | None = cast(Path, args.artifacts).resolve() if args.artifacts else None
    if artifacts is not None:
        artifacts.mkdir(parents=True, exist_ok=not independent)
    observer_root = stage_plugins((artifacts or observer_temporary) / "observer-plugins")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(observer_root), environment.get("PYTHONPATH")) if value
    )
    coverage_overlay: dict[str, JSONValue] | None = None
    if getattr(args, "coverage_subprocess", False):
        if artifacts is None:
            raise SystemExit("--coverage-subprocess requires --artifacts")
        from benchmarks.coverage_config_overlay import (  # noqa: PLC0415 - optional tomlkit
            prepare_subprocess_config,
        )

        coverage_overlay = prepare_subprocess_config(
            interpreter_path,
            root,
            artifacts / "coverage-subprocess.toml",
            cov_config,
            environment=environment,
        )
        cov_config = Path(str(coverage_overlay["config_path"]))
    selected_metadata = selected_interpreter_metadata(
        interpreter_path, root=root, environment=environment
    )
    prepared_metadata = preparation_metadata(
        interpreter_path,
        root,
        manifest_path=args.preparation_manifest,
        build_manifest=build_manifest if independent else None,
    )
    installed_before: dict[str, object] | None = None
    if independent:
        assert build_manifest is not None
        assert artifacts is not None
        if (
            prepared_metadata.get("project") != project.key
            or prepared_metadata.get("checked_out_revision") != project.revision
            or prepared_metadata.get("revision") != project.revision
        ):
            raise RuntimeError("prepared checkout is not the pinned independent coverage project")
        verify_prepared_lock(prepared_metadata)
        installed_before = independent_coverage_artifact(
            interpreter_path, root, environment, build_manifest
        )
        (artifacts / "artifact-before.json").write_text(
            json.dumps(installed_before, indent=2) + "\n"
        )
    build = prepared_metadata["vfy_wheel"]
    if not isinstance(build, dict):
        raise RuntimeError("vfy build metadata is malformed")
    prepared_revision = build.get("source_revision")
    if not isinstance(prepared_revision, str) or len(prepared_revision) != 40:
        raise RuntimeError("prepared vfy build has no full source revision")
    if args.vfy_revision is not None and args.vfy_revision != prepared_revision:
        raise RuntimeError("prepared vfy build revision does not match --vfy-revision")
    vfy_revision = prepared_revision
    if selected_metadata.get("extension_sha256") != build.get("extension_sha256"):
        raise RuntimeError("selected vfy extension does not match its build manifest")
    if selected_metadata.get("build_profile") != build.get("native_profile"):
        raise RuntimeError("selected vfy profile does not match its build manifest")
    scope = workload_scope(
        project,
        selected_tests=bool(args.tests),
        extra_args=extra_args,
        qualification=args.scope,
    )
    methodology = {
        "mode": args.mode,
        "phases": phases,
        "warmups": args.warmups,
        "measured_runs": args.runs,
        "sampling": {"warmups": args.warmups, "measured_runs": args.runs},
        "validation": (
            "independent pytest-cov/vfy JSON reports, manifest-requested XML reports, "
            "schema-7 data, and lifecycle outcomes match before timing"
        ),
        "timed": (
            "child project commands only; preparation, gates, and evidence parsing are "
            "outside timers"
        ),
        "cache": "declared test-generated files restored before every tool run",
        "generated_files": generated_files.metadata(),
        "reports": reports,
        "coverage_context": args.context,
        "source_override": args.source,
        **({"coverage_config_overlay": coverage_overlay} if coverage_overlay is not None else {}),
        **(
            {
                "vfy_engine": "native",
                "engine_proof": "full preflight; hash checked before each timed command",
                "per_sample_native_result_json": False,
                "control_subtracted_scope": (
                    "complete owned workflow minus plain stock pytest; "
                    "not isolated tracing overhead"
                ),
                "per_sample_phase_junit_json_schema7_validation": True,
            }
            if independent
            else {}
        ),
    }
    if args.resources or args.tree_resources:
        methodology["resources"] = {
            "method": (
                "linux-process-tree-smaps-rollup"
                if args.tree_resources
                else "linux-single-child-rusage"
            ),
            "rss_scope": (
                "sampled sum of simultaneous live child and descendant PSS"
                if args.tree_resources
                else "largest individual process peak, not concurrent process-tree RSS"
            ),
            "sampling": "same measured commands and semantic checks as wall timing",
        }
    provenance = prepared_project_provenance(
        Path(__file__).resolve().parents[1],
        workload={
            "project": project.key,
            "scope": scope,
            "workflow": "project-parity",
            "phases": list(phases),
            **(
                {"coverage_config_overlay": coverage_overlay}
                if coverage_overlay is not None
                else {}
            ),
        },
        workers=(
            1
            if independent
            else project.serial_workers
            if phases == ("serial",)
            else project.workers
            if isinstance(project.workers, int)
            else None
        ),
        cache="prepared checkout; semantic gate before timing",
        packages=("pytest", "pytest-cov"),
        selected=selected_metadata,
        prepared=prepared_metadata,
        source_revision=vfy_revision,
        upstream_revision=project.revision,
        sampling={"mode": args.mode, "warmups": args.warmups, "measured_runs": args.runs},
        publication=args.mode == "publication",
        build_manifest=build_manifest if independent else None,
        validation=(
            "independent pytest-cov/vfy JSON reports, manifest-requested XML reports, "
            "schema-7 data, and lifecycle outcomes matched"
        ),
    )
    provenance["generated_files"] = generated_files.metadata()
    parity_commands = {
        name: [
            {"phase": phase, "argv": command_args}
            for phase, command_args in sequence_commands(
                args.python,
                project,
                plugin=plugin,
                reports=parity_report_arguments(project, name, artifacts or root, reports)[0],
                cov_config=cov_config,
                extra_args=extra_args,
                tests=tests,
                phases=phases,
                coverage_context=args.context,
                source_override=args.source,
            )
        ]
        for name, plugin in (
            ("pytest-cov", "pytest_cov.plugin"),
            ("vfy", "vfy.pytest_plugin"),
        )
    }
    result = {
        **(
            {"parity_scope": "legacy plugin reference only; owned coverage preflight pending"}
            if independent
            else {}
        ),
        "schema": 3,
        "benchmark": "projects",
        "workload": {
            "project": project.key,
            "scope": scope,
            "workflow": "project-parity",
            "phases": phases,
        },
        "project": project.name,
        "commit": git_value(root, "rev-parse", "HEAD"),
        "vfy_commit": vfy_revision,
        "commands": parity_commands,
        "python": selected_metadata["python"],
        "scope": scope,
        "tests": tests,
        "phases": phases,
        "interpreter": selected_metadata,
        "preparation": prepared_metadata,
        "methodology": methodology,
        "provenance": provenance,
        "mode": args.mode,
        "warmups": args.warmups,
        "runs": args.runs,
    }
    try:
        expected_data, expected_outcomes = validate_parity(
            root,
            project,
            python=args.python,
            cov_config=cov_config,
            extra_args=extra_args,
            tests=tests,
            environment=environment,
            artifacts=artifacts,
            phases=phases,
            restore_generated_files=generated_files.restore,
            reports=reports,
            coverage_context=args.context,
            source_override=args.source,
        )
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        result.update(parity="failed", error=str(error))
        cast(dict[str, object], provenance["validation"]).update(
            {"status": "failed", "evidence": str(error)}
        )
        raise
    else:
        result["parity"] = "exact"
    finally:
        if artifacts is not None:
            (artifacts / "parity.json").write_text(
                json.dumps(result, indent=2) + "\n", encoding="utf-8"
            )
        if independent:
            assert artifacts is not None
            assert build_manifest is not None
            after = independent_coverage_artifact(
                interpreter_path, root, environment, build_manifest
            )
            (artifacts / "artifact-after-reference.json").write_text(
                json.dumps(after, indent=2) + "\n"
            )
            if after != installed_before:
                raise RuntimeError("installed artifact changed during coverage reference gate")
    if artifacts is not None:
        retain_parity_snapshot(artifacts, expected_data, expected_outcomes)
    if args.validate_only and not independent:
        print(json.dumps(result))
        return 0

    outcome_files = {
        name: root / f".vfy-benchmark-{name}-outcomes.jsonl"
        for name in (
            "pytest-cov",
            "vfy",
            "plain pytest",
        )
    }

    commands = {
        "pytest-cov": sequence_commands(
            args.python,
            project,
            plugin="pytest_cov.plugin",
            reports=reports,
            cov_config=cov_config,
            extra_args=extra_args,
            tests=tests,
            phases=phases,
            outcome_path=outcome_files["pytest-cov"],
            coverage_context=args.context,
            source_override=args.source,
        ),
        "vfy": sequence_commands(
            args.python,
            project,
            plugin="vfy.pytest_plugin",
            reports=reports,
            cov_config=cov_config,
            extra_args=extra_args,
            tests=tests,
            phases=phases,
            outcome_path=outcome_files["vfy"],
            coverage_context=args.context,
            source_override=args.source,
        ),
        "plain pytest": sequence_commands(
            args.python,
            project,
            plugin=None,
            reports=(),
            cov_config=None,
            extra_args=extra_args,
            tests=tests,
            phases=phases,
            outcome_path=outcome_files["plain pytest"],
        ),
    }
    environments = {
        name: {**environment, "COVERAGE_FILE": str(root / f".vfy-benchmark-{name}")}
        for name in ("pytest-cov", "vfy")
    }
    environments["plain pytest"] = environment
    coverage_files = {name: root / f".vfy-benchmark-{name}" for name in ("pytest-cov", "vfy")}

    attempts: list[dict[str, object]] = []
    observed_commands: dict[str, list[dict[str, object]]] = {}
    expected_junit: list[dict[str, object]] | None = None
    execution_proof: dict[str, object] | None = None
    reference_report = (artifacts or root) / ".vfy-benchmark-pytest-cov.json"
    if independent:
        assert artifacts is not None
        assert build_manifest is not None
        commands["vfy-independent"] = commands.pop("vfy")
        # Preserve the historical tool order with only the candidate label changed.
        commands = {
            name: commands[name] for name in ("pytest-cov", "vfy-independent", "plain pytest")
        }
        preflight: dict[str, object] = {"status": "running"}
        try:
            for name in commands:
                generated_files.restore()
                attempt = independent_coverage_attempt(
                    name,
                    commands[name],
                    root=root,
                    environment=environment,
                    destination=artifacts / "independent-preflight" / name.replace(" ", "-"),
                    expected_data=expected_data,
                    expected_outcomes=expected_outcomes,
                    reference_report=reference_report,
                    expected_junit=expected_junit,
                    preflight=True,
                    execution_proof=None,
                    records=attempts,
                )
                observed_commands[name] = [{"phase": "serial", "argv": attempt["command"]}]
                if expected_junit is None:
                    # Import lazily so routes without expected JUnit skip the experiment helpers.
                    from validation.pytest_runner.benchmark_vfy_coverage import (  # noqa: PLC0415
                        is_object,
                    )

                    reference_bytes = Path(str(attempt["junit_reference"])).read_bytes()
                    if hashlib.sha256(reference_bytes).hexdigest() != attempt["junit_sha256"]:
                        raise RuntimeError("stock JUnit reference changed")
                    reference: object = json.loads(reference_bytes)
                    if not isinstance(reference, list):
                        raise RuntimeError("stock JUnit reference is not a list")
                    expected_junit = []
                    for case in reference:
                        if not is_object(case):
                            raise RuntimeError("stock JUnit reference contains an invalid case")
                        expected_junit.append(case)
                if name == "vfy-independent":
                    execution_proof = cast(dict[str, object], attempt["execution_proof"])
            preflight["status"] = "passed"
        except BaseException as error:
            preflight.update(status="failed", error=str(error))
            raise
        finally:
            guard_stage = "restore generated files"
            try:
                generated_files.restore()
                guard_stage = "installed artifact"
                after = independent_coverage_artifact(
                    interpreter_path, root, environment, build_manifest
                )
                (artifacts / "artifact-after-preflight.json").write_text(
                    json.dumps(after, indent=2) + "\n"
                )
                if after != installed_before:
                    raise RuntimeError("installed artifact changed during preflight")
                guard_stage = "prepared dependency lock"
                verify_prepared_lock(prepared_metadata)
                guard_stage = "controller revision and clean source"
                repository = Path(__file__).resolve().parents[1]
                require_revision(
                    repository, cast(str, cast(dict[str, object], provenance["source"])["revision"])
                )
                if args.mode == "publication":
                    require_clean(repository)
            except BaseException as guard_error:
                preflight["status"] = "failed"
                preflight["guard_error"] = {
                    "stage": guard_stage,
                    "type": type(guard_error).__name__,
                    "message": str(guard_error),
                }
                # Preserve the original command error; control exceptions still propagate.
                if "error" not in preflight or not isinstance(guard_error, Exception):
                    raise
            finally:
                if preflight["status"] == "passed" and execution_proof is not None:
                    execution_proof["preflight_passed"] = True
                preflight["attempts"] = list(attempts)
                (artifacts / "independent-preflight.json").write_text(
                    json.dumps(preflight, indent=2) + "\n"
                )
        assert execution_proof is not None
        assert execution_proof.get("preflight_passed") is True
        result["baseline_plugin_commands"] = result["commands"]
        result["commands"] = observed_commands
        result["parity_scope"] = "full independent coverage preflight"
        result["independent_preflight"] = preflight
        if args.validate_only:
            print(json.dumps(result))
            return 0

    assert args.output is not None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    partial_metadata: dict[str, object] = {
        "schema": 1,
        "benchmark": "projects",
        "workload": result["workload"],
        "project": project.name,
        "project_key": project.key,
        "project_root": str(root),
        "project_commit": result["commit"],
        "vfy_commit": vfy_revision,
        "mode": "diagnostic",
        "requested_mode": args.mode,
        "methodology": {
            **methodology,
            "mode": "diagnostic",
            "requested_mode": args.mode,
        },
        "parity": {
            "status": result["parity"],
            "commands": result["commands"],
            "artifact": str(artifacts / "parity.json") if artifacts else None,
        },
        "commands": {
            name: [{"phase": phase, "argv": command_args} for phase, command_args in command_list]
            for name, command_list in commands.items()
        },
        "environment": {
            "python": selected_metadata["python"],
            "implementation": selected_metadata["implementation"],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "vfy": selected_metadata["vfy"],
            "pytest_cov": selected_metadata["pytest_cov"],
        },
        "preparation": prepared_metadata,
    }

    if independent:
        partial_metadata["attempts"] = attempts
        partial_metadata["commands"] = observed_commands
        partial_metadata["independent_preflight"] = result["independent_preflight"]
    samples = {name: [] for name in commands}
    resource_samples: dict[str, list[dict[str, object]]] = {name: [] for name in commands}
    rss_samples: dict[str, list[int]] = {name: [] for name in commands}
    tree_pss_samples: dict[str, list[int]] = {name: [] for name in commands}
    tree_rss_samples: dict[str, list[int]] = {name: [] for name in commands}
    step_samples: dict[str, dict[str, list[float]]] = {
        name: {phase: [] for phase, _command_args in command_list}
        for name, command_list in commands.items()
    }
    warmups_completed = 0

    def checkpoint_partial() -> None:
        write_partial_result(
            args.output,
            partial_metadata,
            warmups_requested=args.warmups,
            warmups_completed=warmups_completed,
            runs_requested=args.runs,
            samples=samples,
            step_samples=step_samples,
        )

    checkpoint_partial()
    try:
        for warmup_index in range(args.warmups):
            for name in commands:
                print(
                    f"{project.key}: warmup {warmup_index + 1}/{args.warmups}: {name}",
                    file=sys.stderr,
                    flush=True,
                )
                generated_files.restore()
                if independent:
                    assert artifacts is not None
                    attempt = independent_coverage_attempt(
                        name,
                        commands[name],
                        root=root,
                        environment=environment,
                        destination=artifacts
                        / "warmup"
                        / str(warmup_index + 1)
                        / name.replace(" ", "-"),
                        expected_data=expected_data,
                        expected_outcomes=expected_outcomes,
                        reference_report=reference_report,
                        expected_junit=expected_junit,
                        preflight=False,
                        execution_proof=execution_proof,
                        records=attempts,
                    )
                    observed_commands[name] = [{"phase": "serial", "argv": attempt["command"]}]
                else:
                    timed_sequence(
                        commands[name],
                        root=root,
                        environment=environments[name],
                        coverage_file=coverage_files.get(name),
                        outcome_file=outcome_files[name],
                    )
                if not independent:
                    validate_sample(
                        name,
                        root=root,
                        outcome_file=outcome_files[name],
                        expected_outcomes=expected_outcomes,
                        coverage_file=coverage_files.get(name),
                        expected_data=expected_data,
                        stage="warmup",
                    )
            warmups_completed += 1
            checkpoint_partial()

        names = tuple(commands)
        for sample_index, order in enumerate(rotated_orders(names, args.runs), start=1):
            for name in order:
                print(
                    f"{project.key}: sample {sample_index}/{args.runs}: {name}",
                    file=sys.stderr,
                    flush=True,
                )
                generated_files.restore()
                record_path: Path | None = None
                resource_record: dict[str, object] | None = None
                if args.resources or args.tree_resources:
                    assert artifacts is not None
                    slug = name.replace(" ", "-")
                    record_path = artifacts / "resources" / f"sample-{sample_index}-{slug}.json"
                    elapsed, steps, resource_record = resource_sequence(
                        commands[name],
                        root=root,
                        environment=environments[name],
                        coverage_file=coverage_files.get(name),
                        outcome_file=outcome_files[name],
                        record_path=record_path,
                        tree=args.tree_resources,
                    )
                elif independent:
                    assert artifacts is not None
                    attempt = independent_coverage_attempt(
                        name,
                        commands[name],
                        root=root,
                        environment=environment,
                        destination=artifacts
                        / "sample"
                        / str(sample_index)
                        / name.replace(" ", "-"),
                        expected_data=expected_data,
                        expected_outcomes=expected_outcomes,
                        reference_report=reference_report,
                        expected_junit=expected_junit,
                        preflight=False,
                        execution_proof=execution_proof,
                        records=attempts,
                    )
                    observed_commands[name] = [{"phase": "serial", "argv": attempt["command"]}]
                    elapsed = cast(float, attempt["milliseconds"])
                    steps = {"serial": elapsed}
                else:
                    elapsed, steps = timed_sequence(
                        commands[name],
                        root=root,
                        environment=environments[name],
                        coverage_file=coverage_files.get(name),
                        outcome_file=outcome_files[name],
                    )
                if not independent:
                    validate_sample(
                        name,
                        root=root,
                        outcome_file=outcome_files[name],
                        expected_outcomes=expected_outcomes,
                        coverage_file=coverage_files.get(name),
                        expected_data=expected_data,
                        stage="sample",
                    )
                samples[name].append(elapsed)
                if args.resources or args.tree_resources:
                    assert record_path is not None
                    assert resource_record is not None
                    peak_bytes = resource_record["peak_rss_bytes"]
                    assert isinstance(peak_bytes, int)
                    rss_samples[name].append(peak_bytes)
                    if args.tree_resources:
                        tree_pss = resource_record["peak_tree_pss_bytes"]
                        tree_rss = resource_record["peak_tree_rss_bytes"]
                        assert isinstance(tree_pss, int)
                        assert isinstance(tree_rss, int)
                        tree_pss_samples[name].append(tree_pss)
                        tree_rss_samples[name].append(tree_rss)
                    resource_samples[name].append(
                        {
                            **resource_record,
                            "path": str(record_path),
                            "sha256": hashlib.sha256(record_path.read_bytes()).hexdigest(),
                            "validation": {
                                "outcomes": "passed",
                                "coverage_data": (
                                    "passed" if name != "plain pytest" else "not applicable"
                                ),
                            },
                        }
                    )
                for phase, value in steps.items():
                    step_samples[name][phase].append(value)
                checkpoint_partial()
    finally:
        generated_files.restore()
        if independent:
            checkpoint_partial()
            assert artifacts is not None
            assert build_manifest is not None
            after = independent_coverage_artifact(
                interpreter_path, root, environment, build_manifest
            )
            (artifacts / "artifact-after.json").write_text(json.dumps(after, indent=2) + "\n")
            if after != installed_before:
                raise RuntimeError("installed artifact changed during coverage cohort")
            verify_prepared_lock(prepared_metadata)
            repository = Path(__file__).resolve().parents[1]
            require_revision(
                repository, cast(str, cast(dict[str, object], provenance["source"])["revision"])
            )
            if args.mode == "publication":
                require_clean(repository)

    result = {
        "schema": 3,
        "benchmark": "projects",
        "workload": {
            "project": project.key,
            "scope": scope,
            "workflow": "project-parity",
            "phases": phases,
        },
        "project": {
            "name": project.name,
            "key": args.project,
            "url": project.url,
            "commit": git_value(root, "rev-parse", "HEAD"),
            "root": "$PROJECT",
            "scope": scope,
            "phases": phases,
            "vfy_commit": vfy_revision,
            "dependency_lock": prepared_metadata.get("dependency_lock", project.dependency_lock),
            "dependency_lock_sha256": prepared_metadata.get("dependency_lock_sha256"),
        },
        "commands": {
            name: [
                {
                    "phase": phase,
                    "argv": [
                        "$PYTHON" if index == 0 else part.replace(str(root), "$PROJECT")
                        for index, part in enumerate(command_args)
                    ],
                }
                for phase, command_args in command_list
            ]
            for name, command_list in commands.items()
        },
        "environment": {
            "python": selected_metadata["python"],
            "implementation": selected_metadata["implementation"],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "vfy": selected_metadata["vfy"],
            "pytest_cov": selected_metadata["pytest_cov"],
        },
        "preparation": prepared_metadata,
        "methodology": methodology,
        "provenance": provenance,
        "mode": args.mode,
        "warmups": args.warmups,
        "runs": args.runs,
        **(
            {
                "commands": observed_commands,
                "attempts": attempts,
                "independent_preflight": result["independent_preflight"],
            }
            if independent
            else {}
        ),
        **{name: summarize(values) for name, values in samples.items()},
        "steps": {
            name: {phase: summarize(values) for phase, values in phases.items()}
            for name, phases in step_samples.items()
        },
    }
    if args.resources or args.tree_resources:
        result["resources"] = {
            name: {
                "median_peak_rss_bytes": statistics.median(rss_samples[name]),
                "samples_peak_rss_bytes": rss_samples[name],
                **(
                    {
                        "median_peak_tree_pss_bytes": statistics.median(tree_pss_samples[name]),
                        "samples_peak_tree_pss_bytes": tree_pss_samples[name],
                        "median_peak_tree_rss_bytes": statistics.median(tree_rss_samples[name]),
                        "samples_peak_tree_rss_bytes": tree_rss_samples[name],
                    }
                    if args.tree_resources
                    else {}
                ),
                "records": records,
            }
            for name, records in resource_samples.items()
        }
        assert artifacts is not None
        parity_path = artifacts / "parity.json"
        result["resource_parity_artifact"] = {
            "path": str(parity_path),
            "sha256": hashlib.sha256(parity_path.read_bytes()).hexdigest(),
        }
    candidate_name = "vfy-independent" if independent else "vfy"
    result["speedup_vs_pytest_cov"] = (
        result["pytest-cov"]["median_ms"] / result[candidate_name]["median_ms"]
    )
    result["overhead_percent"] = {
        name: (result[name]["median_ms"] / result["plain pytest"]["median_ms"] - 1) * 100
        for name in ("pytest-cov", candidate_name)
    }
    plain_median = result["plain pytest"]["median_ms"]
    result["overhead_ms"] = {
        name: result[name]["median_ms"] - plain_median for name in ("pytest-cov", candidate_name)
    }
    vfy_overhead = result["overhead_ms"][candidate_name]
    result["overhead_speedup_vs_pytest_cov"] = (
        result["overhead_ms"]["pytest-cov"] / vfy_overhead if vfy_overhead > 0 else None
    )
    if independent:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, indent=2) + "\n")
    else:
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".partial.json").unlink(missing_ok=True)
    print(json.dumps({"project": project.name, "speedup": result["speedup_vs_pytest_cov"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
