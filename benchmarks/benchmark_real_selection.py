"""Compare affected-test reruns with pytest-testmon on a pinned real project.

Both tools build their own history on the complete upstream suite, then run
after the same reviewed source edit. vfy's selected coverage and outcomes
are checked against an independent full pytest-cov oracle; testmon's executed
outcomes are checked against that oracle before any time is reported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast
from xml.etree import ElementTree as ET

from benchmarks.benchmark_harness import (
    BenchmarkMode,
    SamplingConfig,
    rotated_orders,
    run_checked,
    summarize,
    timed_process,
)
from benchmarks.benchmark_projects import (
    CoverageSnapshot,
    GeneratedFiles,
    load_json,
    normalized_data,
    outcome_records,
)
from benchmarks.benchmark_provenance import prepared_project_provenance
from benchmarks.benchmark_resources import tree_resource_methodology, tree_resource_process
from benchmarks.benchmark_workflows import (
    _canonical_outcomes,
    _oracle,
    apply_patch,
    clear_measured_caches,
    pytest_config_for,
    reset_checkout,
    validate_prepared_root,
    workflow_environment,
)
from benchmarks.observer_plugins import OUTCOME_PLUGIN, stage_plugins
from benchmarks.project_manifest import EditPatch, ProjectManifest, manifest
from benchmarks.rich_workload import prepare_rich_workload

SelectionEngine = Literal["native", "pytest-native"]

# This changes executable code while preserving Emoji.__repr__ for every name.
RICH_EDIT = EditPatch(
    name="rich-real-selection-edit",
    path="rich/emoji.py",
    before='return f"<emoji {self.name!r}>"',
    after='return "<emoji " + repr(self.name) + ">"',
    kind="local",
    tests=("tests/test_emoji.py::test_str_repr",),
    rationale="Preserves the representation while changing the executed string expression.",
)
PLATFORMDIRS_EDIT = EditPatch(
    name="platformdirs-real-selection-edit",
    path="src/platformdirs/unix.py",
    before="return prefix + value",
    after='return prefix + value + ""',
    kind="local",
    tests=("tests/test_unix.py::test_user_media_dir_from_user_dirs_file",),
    rationale=(
        "Changes the executed expression in _resolve_user_dirs_value while "
        "preserving the resulting typed string."
    ),
)
DOTENV_EDIT = EditPatch(
    name="dotenv-real-selection-edit",
    path="src/dotenv/__init__.py",
    before='return " ".join(command).strip()',
    after='return " ".join(command).strip() + ""',
    kind="local",
    tests=("tests/test_utils.py::test_to_cli_string",),
    rationale="Changes get_cli_string's executed expression without changing the command.",
)
SORTEDCONTAINERS_EDIT = EditPatch(
    name="sortedcontainers-real-selection-edit",
    path="src/sortedcontainers/sortedlist.py",
    before="        stop -= 1\n        left = self._loc(pos_left, idx_left)",
    after="        stop = stop - 1\n        left = self._loc(pos_left, idx_left)",
    kind="local",
    tests=("tests/test_coverage_sortedlist.py::test_index",),
    rationale="Rewrites SortedList.index's stop adjustment as an equivalent assignment.",
)
COLORAMA_EDIT = EditPatch(
    name="colorama-real-selection-edit",
    path="colorama/ansitowin32.py",
    before="params = tuple(int(p) for p in paramstring.split(';') if len(p) != 0)",
    after="params = tuple(int(p) for p in paramstring.split(';') if p)",
    kind="local",
    tests=("colorama/tests/ansitowin32_test.py::AnsiToWin32Test::testExtractParams",),
    rationale="Tests each parameter string by truthiness, which equals its non-zero length.",
)
SELECTION_EDITS = {
    "colorama": COLORAMA_EDIT,
    "dotenv": DOTENV_EDIT,
    "platformdirs": PLATFORMDIRS_EDIT,
    "rich": RICH_EDIT,
    "sortedcontainers": SORTEDCONTAINERS_EDIT,
}


# Projects whose full-suite lock omits pytest-testmon use a separate selection lock
# that adds it. The other selection projects' locks already include it.
TESTMON_SELECTION_LOCKS = frozenset({"platformdirs"})


def selection_project(key: str) -> ProjectManifest:
    project = manifest(key)
    if key in TESTMON_SELECTION_LOCKS:
        project = replace(
            project,
            dependency_lock=f"benchmarks/locks/{key}-selection.txt",
            requirements=(*project.requirements, "pytest-testmon==2.2.0"),
        )
    return replace(
        project,
        coverage_args=(*project.coverage_args, "--capture=no"),
        parallel_args=(*project.parallel_args, "--capture=no"),
    )


def measured_runtime_versions(python: Path, root: Path) -> dict[str, str]:
    """Query the tested interpreter; generic provenance describes the controller."""

    code = (
        "import importlib.metadata,json; "
        "names=('vfy','pytest','pytest-cov','pytest-testmon','coverage'); "
        "print(json.dumps({name:importlib.metadata.version(name) for name in names}))"
    )
    output = run_checked([str(python), "-c", code], cwd=root, timeout=30).stdout
    versions = json.loads(output)
    if not isinstance(versions, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in versions.items()
    ):
        raise RuntimeError("measured interpreter package versions are invalid")
    return cast("dict[str, str]", versions)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", choices=tuple(SELECTION_EDITS), required=True)
    parser.add_argument("--engine", choices=("native", "pytest-native"), default="native")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--preparation-manifest", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--runs", type=int, default=15)
    parser.add_argument("--mode", choices=("publication", "diagnostic"), default="publication")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--tree-resources", action="store_true", help="sample Linux process-tree peak PSS"
    )
    args = parser.parse_args()
    if args.tree_resources and sys.platform != "linux":
        parser.error("--tree-resources requires Linux")
    return args


def build_testmon_command(python: Path, project: ProjectManifest, junit: Path) -> list[str]:
    return [
        str(python),
        "-m",
        "pytest",
        "-q",
        "--capture=no",
        "-p",
        "testmon.pytest_testmon",
        "--testmon",
        f"--junitxml={junit}",
        *project.tests,
    ]


def run_testmon(
    root: Path,
    python: Path,
    artifacts: Path,
    label: str,
    project: ProjectManifest,
    *,
    measure: bool,
    resource_path: Path | None = None,
) -> tuple[float, list[tuple[str, str, str, str | None]]]:
    outcome_path = artifacts / f"testmon-{label}-outcomes.jsonl"
    junit = artifacts / "testmon.xml"
    outcome_path.unlink(missing_ok=True)
    junit.unlink(missing_ok=True)
    environment = workflow_environment(
        project, python, root=root, observer_root=stage_plugins(artifacts / "observer-plugins")
    )
    environment["PYTEST_PLUGINS"] = ",".join((OUTCOME_PLUGIN, *project.plugins))
    environment["VFY_BENCHMARK_OUTCOMES"] = str(outcome_path)
    command = build_testmon_command(python, project, junit)
    clear_measured_caches(root)
    if resource_path is None:
        elapsed, completed = timed_process(command, cwd=root, env=environment)
    else:
        elapsed, completed, _ = tree_resource_process(
            command, cwd=root, env=environment, record_path=resource_path
        )
    if not measure:
        elapsed = 0.0
    (artifacts / f"testmon-{label}.log").write_text(
        (completed.stdout or "") + (completed.stderr or ""), encoding="utf-8"
    )
    (artifacts / f"testmon-{label}-command.json").write_text(
        json.dumps(command, indent=2) + "\n", encoding="utf-8"
    )
    if completed.returncode:
        raise RuntimeError(f"testmon {label} failed with exit {completed.returncode}")
    records = outcome_records(outcome_path) if outcome_path.is_file() else []
    check_native_junit(junit, records, tool="pytest-testmon")
    shutil.copy2(junit, artifacts / f"testmon-{label}.xml")
    return elapsed, records


def compare_outcomes(
    selected: list[tuple[str, str, str, str | None]],
    oracle: list[tuple[str, str, str, str | None]],
    *,
    tool: str = "pytest-testmon",
) -> list[str]:
    oracle_rows = _canonical_outcomes(oracle)
    selected_rows = _canonical_outcomes(selected)
    selected_nodes = {node for node, *_ in selected_rows}
    expected = [row for row in oracle_rows if row[0] in selected_nodes]
    if not selected_nodes or selected_rows != expected:
        raise RuntimeError(f"{tool} selected outcomes differ from the full pytest-cov oracle")
    if len(selected_nodes) >= len({node for node, *_ in oracle_rows}):
        raise RuntimeError(f"{tool} did not select a strict affected subset")
    return sorted(selected_nodes)


def required_affected_nodes(
    oracle: list[tuple[str, str, str, str | None]], targets: tuple[str, ...]
) -> set[str]:
    """Resolve reviewed file, function, or parametrized selectors in the full suite."""

    required = {
        node
        for node, *_ in _canonical_outcomes(oracle)
        if any(
            node == target.removeprefix("tests/")
            or node.startswith(
                (target.removeprefix("tests/") + "::", target.removeprefix("tests/") + "[")
            )
            for target in targets
        )
    }
    if not required:
        raise RuntimeError("reviewed edit targets did not collect tests")
    return required


def check_required_selection(selected: set[str], required: set[str], tool: str) -> None:
    if not required <= selected:
        raise RuntimeError(
            f"{tool} omitted reviewed affected tests: {sorted(required - selected)[:10]!r}"
        )


def check_coverage_stage(
    vfy_data: Path, oracle_data: Path, root: Path, *, label: str
) -> dict[str, object]:
    """Gate complete aggregate schema-7 data and disclose context differences."""

    vfy = normalized_data(vfy_data, root)
    oracle = normalized_data(oracle_data, root)

    def aggregate(snapshot: CoverageSnapshot) -> dict[str, object]:
        files = cast(dict[str, dict[str, object]], snapshot["files"])
        return {
            "has_arcs": snapshot["has_arcs"],
            "files": {
                name: {field: record[field] for field in ("lines", "arcs", "tracer")}
                for name, record in files.items()
            },
        }

    if aggregate(vfy) != aggregate(oracle):
        raise RuntimeError(f"vfy {label} aggregate coverage data differs from full pytest-cov")
    vfy_contexts = cast(dict[str, dict[str, object]], vfy["by_context"])
    oracle_contexts = cast(dict[str, dict[str, object]], oracle["by_context"])
    differences = [
        {"context": context, "file": filename}
        for context in sorted(set(vfy_contexts) | set(oracle_contexts))
        for filename in sorted(
            set(vfy_contexts.get(context, {})) | set(oracle_contexts.get(context, {}))
        )
        if vfy_contexts.get(context, {}).get(filename)
        != oracle_contexts.get(context, {}).get(filename)
    ]
    context_names_match = vfy["contexts"] == oracle["contexts"]
    return {
        "aggregate_schema7": "exact",
        "per_context_schema7": "exact" if context_names_match and not differences else "differs",
        "vfy_contexts": len(vfy["contexts"]),
        "oracle_contexts": len(oracle["contexts"]),
        "vfy_only_contexts": sorted(set(vfy["contexts"]) - set(oracle["contexts"]))[:10],
        "oracle_only_contexts": sorted(set(oracle["contexts"]) - set(vfy["contexts"]))[:10],
        "different_context_file_pairs": len(differences),
        "first_differences": differences[:10],
        "vfy_data": str(vfy_data),
        "oracle_data": str(oracle_data),
    }


def stash_testmon_data(root: Path, destination: Path) -> None:
    """Checkpoint testmon's SQLite WAL into a movable complete database."""

    source_path = root / ".testmondata"
    if not source_path.is_file():
        raise RuntimeError("pytest-testmon did not create its history database")
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{destination}{suffix}").unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{source_path}{suffix}").unlink(missing_ok=True)


def native_outcomes(
    path: Path, python: Path, root: Path, *, engine: SelectionEngine
) -> list[tuple[str, str, str, str | None]]:
    """Validate identity, then use the measured wheel's existing phase projection."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = {"pytest-native": "pytest-runner-native-v1", "native": "vfy-independent-v1"}[engine]
    if not isinstance(payload, dict) or payload.get("engine") != expected:
        raise RuntimeError(f"vfy {engine} did not write a valid execution record")
    # Keep xfail reasons and report outcomes aligned with the installed runner.
    # This projection and the complete phase gate run outside the command timer.
    code = (
        "import json,sys; from pathlib import Path; from vfy import native; "
        "result=native._pytest.NativeRunResult.merge_json_results("
        "[Path(sys.argv[1]).read_text()]); "
        "print('\\n'.join(json.dumps(dict(nodeid=r[0],when=r[1],outcome=r[2],wasxfail=r[5])) "
        "for r in result.pytest_reports()))"
    )
    projected = run_checked([str(python), "-c", code, str(path)], cwd=root, timeout=30)
    normalized = path.with_suffix(".phases.jsonl")
    normalized.write_text(projected.stdout, encoding="utf-8")
    return outcome_records(normalized)


def full_oracle(
    root: Path,
    python: Path,
    artifacts: Path,
    label: str,
    project: ProjectManifest,
    cov_config: Path,
    *,
    engine: SelectionEngine,
) -> tuple[list[tuple[str, str, str, str | None]], list[Path], list[Path]]:
    """Prove default discovery covers exactly the manifest scope before omitting targets."""

    records, reports, data = _oracle(
        root, project, python, project.tests, ("parallel",), artifacts, label, cov_config
    )
    if engine == "native":
        discovered, _, _ = _oracle(
            root, project, python, (), ("parallel",), artifacts, f"{label}-default", cov_config
        )
        if _canonical_outcomes(discovered) != _canonical_outcomes(records):
            raise RuntimeError("default discovery differs from the full manifest oracle")
    return records, reports, data


def check_native_junit(
    path: Path, records: list[tuple[str, str, str, str | None]], *, tool: str = "native"
) -> None:
    """Require the public JUnit report to account for every native case."""

    cases = list(ET.parse(path).getroot().iter("testcase"))
    nodeids = {node for node, *_ in records}
    if len(cases) != len(nodeids):
        raise RuntimeError(f"{tool} JUnit test count differs from execution record")
    if any(case.find("failure") is not None or case.find("error") is not None for case in cases):
        raise RuntimeError(f"{tool} JUnit contains a failing test")


def run_vfy(
    root: Path,
    python: Path,
    artifacts: Path,
    label: str,
    project: ProjectManifest,
    cov_config: Path,
    *,
    measure: bool,
    all_tests: bool,
    engine: SelectionEngine = "native",
    resource_path: Path | None = None,
) -> tuple[float, list[tuple[str, str, str, str | None]], Path, Path]:
    outcomes = artifacts / "vfy-outcomes.json"
    report = artifacts / "vfy-report.json"
    junit = artifacts / "vfy.xml"
    data_file = artifacts / "vfy.coverage"
    outcomes.unlink(missing_ok=True)
    report.unlink(missing_ok=True)
    junit.unlink(missing_ok=True)
    if all_tests:
        data_file.unlink(missing_ok=True)
    environment = workflow_environment(
        project, python, root=root, observer_root=stage_plugins(artifacts / "observer-plugins")
    )
    if not project.plugins:
        environment.pop("PYTEST_PLUGINS", None)
    environment.pop("PYTEST_NATIVE_RESULT", None)
    if engine == "pytest-native":
        environment["PYTEST_NATIVE_RESULT"] = str(outcomes)
    environment["COVERAGE_FILE"] = str(data_file)
    command = [
        str(python),
        "-m",
        "vfy",
        "test",
        f"--engine={engine}",
        "-j",
        "1",
        "--source",
        project.package_path.rstrip("/"),
        "--history",
        ".vfy/test-history.json",
        "--data-file",
        str(data_file),
        *(["--all"] if all_tests else []),
        *(project.tests if engine == "pytest-native" else ()),
        *([f"--native-result-json={outcomes}"] if engine == "native" else []),
        "-q",
        *project.coverage_args,
        "--cov-context=test",
        f"--cov-config={cov_config}",
        f"--cov-report=json:{report}",
        f"--junitxml={junit}",
    ]
    clear_measured_caches(root)
    if resource_path is None:
        elapsed, completed = timed_process(command, cwd=root, env=environment)
    else:
        elapsed, completed, _ = tree_resource_process(
            command, cwd=root, env=environment, record_path=resource_path
        )
    if not measure:
        elapsed = 0.0
    (artifacts / f"vfy-{label}.log").write_text(
        (completed.stdout or "") + (completed.stderr or ""), encoding="utf-8"
    )
    (artifacts / f"vfy-{label}-command.json").write_text(
        json.dumps(command, indent=2) + "\n", encoding="utf-8"
    )
    if completed.returncode:
        raise RuntimeError(f"vfy {label} failed with exit {completed.returncode}")
    records = native_outcomes(outcomes, python, root, engine=engine)
    check_native_junit(junit, records, tool=f"vfy {engine}")
    report_snapshot = artifacts / f"vfy-{label}-report.json"
    data_snapshot = artifacts / f"vfy-{label}.coverage"
    for source, snapshot in (
        (outcomes, artifacts / f"vfy-{label}-outcomes.json"),
        (outcomes.with_suffix(".phases.jsonl"), artifacts / f"vfy-{label}-outcomes.phases.jsonl"),
        (report, report_snapshot),
        (junit, artifacts / f"vfy-{label}.xml"),
        (data_file, data_snapshot),
    ):
        shutil.copy2(source, snapshot)
    return elapsed, records, report_snapshot, data_snapshot


def run_pair(
    root: Path,
    python: Path,
    artifacts: Path,
    revision: str,
    project: ProjectManifest,
    patch: EditPatch,
    cov_config: Path,
    generated_files: GeneratedFiles,
    *,
    order: tuple[str, ...],
    measure: bool,
    engine: SelectionEngine = "native",
    tree_resources: bool = False,
) -> dict[str, object]:
    artifacts.mkdir(parents=True, exist_ok=True)
    reset_checkout(root, revision)
    generated_files.restore()
    rich_stabilization, clock_fixture = (
        prepare_rich_workload(root) if project.key == "rich" else (None, None)
    )
    baseline_oracle, baseline_oracle_reports, baseline_oracle_data = full_oracle(
        root,
        python,
        artifacts,
        "baseline-oracle",
        project,
        cov_config,
        engine=engine,
    )
    _elapsed, baseline_records, baseline_report, baseline_data = run_vfy(
        root,
        python,
        artifacts,
        "baseline",
        project,
        cov_config,
        measure=False,
        all_tests=True,
        engine=engine,
    )
    if _canonical_outcomes(baseline_records) != _canonical_outcomes(baseline_oracle):
        raise RuntimeError(f"vfy {engine} baseline outcomes differ from full pytest-cov")
    if load_json(baseline_report, root) != load_json(baseline_oracle_reports[0], root):
        raise RuntimeError(f"vfy {engine} baseline coverage report differs from pytest-cov")
    baseline_coverage = check_coverage_stage(
        baseline_data, baseline_oracle_data[0], root, label="baseline"
    )
    _, testmon_baseline = run_testmon(root, python, artifacts, "baseline", project, measure=False)
    stashed_testmon_data = artifacts / "testmon.data"
    stash_testmon_data(root, stashed_testmon_data)
    shutil.copy2(stashed_testmon_data, artifacts / "testmon-baseline.data")
    if _canonical_outcomes(testmon_baseline) != _canonical_outcomes(baseline_oracle):
        raise RuntimeError(
            "pytest-testmon baseline outcomes differ from the full pytest-cov oracle"
        )
    apply_patch(root, patch)
    timings: dict[str, float] = {}
    vfy_records: list[tuple[str, str, str, str | None]] | None = None
    vfy_report: Path | None = None
    vfy_data: Path | None = None
    testmon_records: list[tuple[str, str, str, str | None]] | None = None
    for tool in order:
        if tool == "vfy":
            timings[tool], vfy_records, vfy_report, vfy_data = run_vfy(
                root,
                python,
                artifacts,
                "local-edit",
                project,
                cov_config,
                measure=measure,
                all_tests=False,
                engine=engine,
                resource_path=artifacts / "resources/vfy.json" if tree_resources else None,
            )
        else:
            shutil.copy2(stashed_testmon_data, root / ".testmondata")
            try:
                timings[tool], testmon_records = run_testmon(
                    root,
                    python,
                    artifacts,
                    "local-edit",
                    project,
                    measure=measure,
                    resource_path=artifacts / "resources/testmon.json" if tree_resources else None,
                )
            finally:
                stash_testmon_data(root, stashed_testmon_data)
            shutil.copy2(stashed_testmon_data, artifacts / "testmon-local-edit.data")
    if vfy_records is None or testmon_records is None or vfy_report is None or vfy_data is None:
        raise RuntimeError("both tools must complete the edit")
    oracle_records, oracle_reports, oracle_data = full_oracle(
        root,
        python,
        artifacts,
        "local-edit-oracle",
        project,
        cov_config,
        engine=engine,
    )
    testmon_nodes = compare_outcomes(testmon_records, oracle_records)
    vfy_nodes = set(compare_outcomes(vfy_records, oracle_records, tool="vfy"))
    required = required_affected_nodes(oracle_records, patch.tests)
    check_required_selection(set(testmon_nodes), required, "pytest-testmon")
    if not set(testmon_nodes) <= vfy_nodes:
        raise RuntimeError("vfy omitted tests selected by pytest-testmon")
    check_required_selection(vfy_nodes, required, "vfy")
    if load_json(vfy_report, root) != load_json(oracle_reports[0], root):
        raise RuntimeError("vfy affected coverage report differs from full pytest-cov")
    affected_coverage = check_coverage_stage(vfy_data, oracle_data[0], root, label="affected")
    result: dict[str, object] = {
        "vfy_engine": engine,
        "vfy_targets": list(project.tests) if engine == "pytest-native" else [],
        "default_discovery_validated": engine == "native",
        "order": order,
        "elapsed_ms": timings,
        "vfy_tests": sorted(vfy_nodes),
        "testmon_tests": testmon_nodes,
        "selection_relation": "exact" if vfy_nodes == set(testmon_nodes) else "vfy_conservative",
        "full_tests": sorted({node for node, *_ in _canonical_outcomes(oracle_records)}),
        "parity": (
            "exact selected outcomes and required targets; vfy JSON report and aggregate "
            "schema-7 coverage equal full pytest-cov; per-test context differences disclosed"
        ),
        "baseline_coverage": baseline_coverage,
        "affected_coverage": affected_coverage,
        "artifacts": str(artifacts),
        "clock_fixture": clock_fixture,
        "rich_stabilization": rich_stabilization,
    }
    if tree_resources:
        result["resources"] = {
            tool: json.loads((artifacts / "resources" / f"{tool}.json").read_text(encoding="utf-8"))
            for tool in ("vfy", "testmon")
        }
    return result


def main() -> int:
    args = parse_args()
    sampling = SamplingConfig(args.warmups, args.runs, BenchmarkMode(args.mode))
    project = selection_project(args.project)
    patch = SELECTION_EDITS[args.project]
    cov_config = Path(__file__).resolve().parent / (
        "configs/rich.coveragerc"
        if args.project == "rich"
        else "configs/project-coverage.coveragerc"
    )
    root = args.root.resolve()
    python = args.python.absolute()
    artifacts = args.artifacts.absolute()
    prepared = validate_prepared_root(project, root, python, args.preparation_manifest, artifacts)
    generated_files = GeneratedFiles(root, project.generated_files)
    if any(item["initial_state"] != "present" for item in generated_files.metadata()):
        raise RuntimeError("prepared project is missing a declared generated source file")
    selected = prepared.get("interpreter")
    wheel = prepared.get("vfy_build")
    if not isinstance(selected, dict) or not isinstance(wheel, dict):
        raise RuntimeError("prepared interpreter or wheel metadata is invalid")
    source_revision = wheel.get("source_revision")
    if not isinstance(source_revision, str):
        raise RuntimeError("prepared wheel has no source revision")
    upstream_sha256 = hashlib.sha256((root / patch.path).read_bytes()).hexdigest()
    # The prepared interpreter, not this controller, is the benchmark runtime.
    version = run_checked(
        [
            str(python),
            "-c",
            "import importlib.metadata; print(importlib.metadata.version('pytest-testmon'))",
        ],
        cwd=root,
        env=workflow_environment(project, python, root=root),
        timeout=30,
    ).stdout.strip()
    if not version:
        raise RuntimeError("prepared interpreter has no pytest-testmon")
    artifacts.mkdir(parents=True, exist_ok=True)
    revision = project.revision

    def sample(path: Path, order: tuple[str, ...], *, measure: bool) -> dict[str, object]:
        try:
            return run_pair(
                root,
                python,
                path,
                revision,
                project,
                patch,
                cov_config,
                generated_files,
                order=order,
                measure=measure,
                engine=args.engine,
                tree_resources=args.tree_resources and measure,
            )
        finally:
            reset_checkout(root, revision)
            generated_files.restore()

    validation = sample(artifacts / "validation", ("vfy", "testmon"), measure=False)
    samples = []
    if not args.validate_only:
        for index, order in enumerate(rotated_orders(("vfy", "testmon"), sampling.warmups)):
            samples.append(
                {
                    "kind": "warmup",
                    "index": index,
                    **sample(artifacts / f"warmup-{index:02d}", order, measure=True),
                }
            )
        for index, order in enumerate(rotated_orders(("vfy", "testmon"), sampling.runs)):
            samples.append(
                {
                    "kind": "measured",
                    "index": index,
                    **sample(artifacts / f"run-{index:02d}", order, measure=True),
                }
            )
    measured = [sample for sample in samples if sample["kind"] == "measured"]
    vfy_times = [cast(dict[str, float], sample["elapsed_ms"])["vfy"] for sample in measured]
    testmon_times = [cast(dict[str, float], sample["elapsed_ms"])["testmon"] for sample in measured]
    result = {
        "benchmark": "real-selection",
        "project": project.name,
        "vfy_engine": args.engine,
        "upstream_revision": revision,
        "prepared": prepared,
        "generated_files": generated_files.metadata(),
        "provenance": prepared_project_provenance(
            Path(__file__).resolve().parents[1],
            workload={
                "project": project.key,
                "workflow": "local-executable-edit-selection",
                "vfy_engine": args.engine,
            },
            workers=1,
            cache=(
                "separate vfy and pytest-testmon histories, reset each repetition; "
                "testmon database stashed outside checkout between tool runs"
            ),
            packages=("pytest", "pytest-cov", "pytest-testmon", "coverage"),
            selected=selected,
            prepared=prepared,
            source_revision=source_revision,
            upstream_revision=revision,
            sampling=sampling.as_dict(),
            publication=sampling.publishable and not args.validate_only,
            validation=(
                "full pytest-cov oracle, exact selected outcomes, JSON report, and aggregate "
                "schema-7 coverage; per-test context differences disclosed"
            ),
        ),
        "testmon_version": version,
        "measured_runtime_versions": measured_runtime_versions(python, root),
        "sampling": sampling.as_dict(),
        "resources": tree_resource_methodology() if args.tree_resources else None,
        "protocol": (
            "serial full-suite baselines, reviewed local executable edit, rotating affected reruns"
        ),
        "workload": {
            "tests": list(project.tests),
            "vfy_targets": list(project.tests) if args.engine == "pytest-native" else [],
            "default_discovery_gate": args.engine == "native",
            "workers": 1,
            "capture": f"no on {args.engine} vfy, pytest-testmon, and full pytest-cov oracle",
            "pytest_config": pytest_config_for(project),
            "coverage_config": str(cov_config),
            "vfy_operation": f"{args.engine} affected selection with coverage JSON and JUnit",
            "testmon_operation": "affected selection with pytest-testmon",
            "clock": "fixed LogRecord time in Rich logging tests"
            if project.key == "rich"
            else None,
            "rich_stabilization": (
                "reviewed deterministic upstream test patch, identical for all routes"
            )
            if project.key == "rich"
            else None,
        },
        "commands": {
            "testmon": build_testmon_command(python, project, artifacts / "validation/testmon.xml"),
            "vfy": "see per-phase command JSON",
        },
        "edit": {
            "path": patch.path,
            "before": patch.before,
            "after": patch.after,
            "upstream_sha256": upstream_sha256,
        },
        "validation": validation,
        "samples": samples,
        "vfy": summarize(vfy_times) if measured else None,
        "testmon": summarize(testmon_times) if measured else None,
        "speedup_vs_testmon": (
            summarize(testmon_times)["median_ms"] / summarize(vfy_times)["median_ms"]
            if measured
            else None
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
