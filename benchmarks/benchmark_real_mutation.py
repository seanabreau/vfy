"""Compare scoped mutations in pinned real projects with mutmut.

The vfy route defaults to the independent engine without a runtime pytest
dependency. Each tool runs the same upstream test target on an exact common mutation
catalog. Validation and independent killer proofs precede rotating cold
samples. Results apply only to the recorded source scope and test target.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import difflib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from benchmarks.benchmark_harness import BenchmarkMode, run_checked, summarize
from benchmarks.benchmark_mutation import (
    MUTMUT_REVISION,
    REPO_ROOT,
    VfySample,
    VfySignature,
    _KillerProofs,
    _require_signature_match,
    _test_isolation_arguments,
    _validated_mutmut_signature,
    _validated_vfy_signature,
    compare_fixture_outcomes,
    copy_project,
    measured_mutmut,
    measured_vfy,
    mutmut_catalog,
    repository_state,
    vfy_catalog,
)
from benchmarks.benchmark_projects import outcome_records
from benchmarks.benchmark_provenance import (
    artifact_provenance,
    build_provenance,
    require_unchanged_artifact,
)
from benchmarks.benchmark_resources import tree_resource_methodology
from benchmarks.benchmark_workflows import validate_prepared_root, workflow_environment
from benchmarks.datasette_mutation import _function_scope, _require_successful_target
from benchmarks.observer_plugins import OUTCOME_PLUGIN, stage_plugins
from benchmarks.project_manifest import ProjectManifest, manifest


@dataclass(frozen=True)
class RealMutationCase:
    project: str
    source: Path
    function: str
    test: str
    source_path: str
    all_function_bodies: bool = False
    imported_module: str | None = None


CASES = {
    "datasette": RealMutationCase(
        "datasette",
        Path("datasette/utils/__init__.py"),
        "truncate_url",
        "tests/test_utils.py::test_truncate_url",
        "datasette/",
    ),
    "requests": RealMutationCase(
        "requests",
        Path("src/requests/utils.py"),
        "unquote_unreserved",
        "tests/test_utils.py::test_unquote_unreserved",
        "src/",
    ),
    "requests-utils": RealMutationCase(
        "requests",
        Path("src/requests/utils.py"),
        "utils.py module",
        "tests/test_utils.py",
        "src/",
        imported_module="requests.utils",
    ),
    "packaging-utils": RealMutationCase(
        "packaging",
        Path("src/packaging/utils.py"),
        "utils.py module",
        "tests/test_utils.py",
        "src/",
        all_function_bodies=True,
    ),
}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", choices=sorted(CASES), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--preparation-manifest", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--mutmut", type=Path, required=True)
    parser.add_argument("--mutmut-python", type=Path, required=True)
    parser.add_argument("--mutmut-repository", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--vfy-route", choices=("native", "pytest-native"), default="native")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--test-isolation",
        choices=("default", "serial", "file"),
        default="default",
        help="vfy isolation selection; default omits the option and uses the engine default",
    )
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--mode", choices=("publication", "diagnostic"), default="publication")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--tree-resources", action="store_true", help="sample Linux process-tree peak PSS"
    )
    args = parser.parse_args()
    if args.tree_resources and sys.platform != "linux":
        parser.error("--tree-resources requires Linux")
    if args.workers < 1 or args.timeout <= 0 or args.warmups < 0 or args.runs < 1:
        parser.error("workers, timeout, and runs must be positive; warmups cannot be negative")
    if args.vfy_route == "native" and (args.workers != 1 or args.test_isolation == "file"):
        parser.error("independent mutation comparison requires one worker and serial isolation")
    if (
        args.mode == BenchmarkMode.PUBLICATION.value
        and not args.validate_only
        and (args.warmups < 3 or args.runs < 30)
    ):
        parser.error("real mutation publication requires 3 warmups and 30 measured runs")
    return args


def _environment(project: ProjectManifest, python: Path, root: Path) -> dict[str, str]:
    env = workflow_environment(project, python, root=root)
    if project.key in {"requests", "packaging"}:
        # These pinned checkouts have no pytest.ini. Let pytest discover each
        # copied project's pyproject.toml on both mutation routes.
        env["PYTEST_ADDOPTS"] = ""
    # The scoped utility tests use no project plug-in fixtures. Native
    # mutation rejects explicitly requested third-party pytest plug-ins.
    env.pop("PYTEST_PLUGINS", None)
    source_root = root / "src"
    path_entries = [str(root)]
    if source_root.is_dir():
        path_entries.insert(0, str(source_root))
    env["PYTHONPATH"] = os.pathsep.join((*path_entries, env.get("PYTHONPATH", "")))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _prepare_copy(source: Path, destination: Path, case: RealMutationCase) -> None:
    copy_project(source, destination)
    configuration = (
        "\n[tool.mutmut]\n"
        f'source_paths = ["{case.source_path}"]\n'
        f'only_mutate = ["{case.source.as_posix()}"]\n'
        f'pytest_add_cli_args_test_selection = ["{case.test}"]\n'
        + ('also_copy = ["docs/"]\n' if case.project == "datasette" else "")
    )
    config_path = destination / "pyproject.toml"
    original = config_path.read_text(encoding="utf-8")
    if "[tool.mutmut]" in original:
        raise RuntimeError("upstream project already defines mutmut configuration")
    if case.project == "packaging":
        selection = '    "-m",\n    "not property",        # property based tests opt-in only\n'
        if original.count(selection) != 1:
            raise RuntimeError("Packaging pytest selection setting changed")
        original = original.replace(selection, "")
        testpaths = 'testpaths = ["tests"]'
        if original.count(testpaths) != 1:
            raise RuntimeError("Packaging pytest testpaths setting changed")
        original = original.replace(testpaths, 'testpaths = ["tests/test_utils.py"]')
        threshold = "fail_under = 100"
        if original.count(threshold) != 1:
            raise RuntimeError("Packaging coverage report threshold changed")
        original = original.replace(threshold, "fail_under = 0")
    config_path.write_text(original + configuration, encoding="utf-8")
    if case.project == "datasette":
        # mutmut calls pytest more than once in one interpreter. Datasette's
        # session fixture otherwise registers the same global plugin twice.
        conftest = destination / "tests" / "conftest.py"
        text = conftest.read_text(encoding="utf-8")
        original_registration = '    pm.register(TrackEventPlugin(), name="TrackEventPlugin")'
        if text.count(original_registration) != 1:
            raise RuntimeError("Datasette tracking fixture changed")
        text = text.replace(
            original_registration,
            '    pm.unregister(name="TrackEventPlugin")\n' + original_registration,
        )
        conftest.write_text(text, encoding="utf-8")


def _successful_target_records(
    records: list[tuple[str, str, str, str | None]], case: RealMutationCase
) -> list[tuple[str, str, str, str | None]]:
    if not case.all_function_bodies and case.imported_module is None:
        return _require_successful_target(records, case.test)
    selected = [
        row for row in records if row[0] == case.test or row[0].startswith(f"{case.test}::")
    ]
    if not selected or not any(row[1] == "call" and row[2] == "passed" for row in selected):
        raise RuntimeError("upstream test module has no passing test calls")
    if any(row[2] not in {"passed", "skipped"} for row in selected):
        raise RuntimeError("upstream test module has a failing test or fixture phase")
    return selected


def _portable_outcomes(
    records: list[tuple[str, str, str, str | None]], root: Path
) -> list[tuple[str, str, str, str | None]]:
    """Preserve parameter identity when upstream tests use ``__file__``."""

    prefix = str(root.resolve())
    return sorted(
        (
            (nodeid.replace(prefix, "<project>"), when, outcome, wasxfail)
            for nodeid, when, outcome, wasxfail in records
        ),
        key=lambda row: (row[0], row[1], row[2], row[3] or ""),
    )


def _function_body_ranges(
    root: Path, case: RealMutationCase
) -> tuple[list[tuple[int, int]], list[str]]:
    if not case.all_function_bodies and case.imported_module is None:
        raise RuntimeError("case has no function-body scope")
    imports: set[str] | None = None
    if case.imported_module is not None:
        test_tree = ast.parse((root / case.test).read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in test_tree.body
            if isinstance(node, ast.ImportFrom) and node.module == case.imported_module
            for alias in node.names
        }
    source = (root / case.source).read_text(encoding="utf-8")
    tree = ast.parse(source)
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line.encode("utf-8")))
    ranges: list[tuple[int, int]] = []
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if imports is not None and node.name not in imports:
            continue
        if not node.body or node.end_lineno is None or node.end_col_offset is None:
            raise RuntimeError(f"Python AST has no complete range for {node.name}")
        first_body = node.body[0]
        ranges.append(
            (
                starts[first_body.lineno - 1] + first_body.col_offset,
                starts[node.end_lineno - 1] + node.end_col_offset,
            )
        )
        names.append(node.name)
    names.sort()
    if not ranges:
        raise RuntimeError("test module imports no top-level source functions")
    return ranges, names


def _validate_upstream_test(
    project: ProjectManifest,
    case: RealMutationCase,
    root: Path,
    python: Path,
    artifacts: Path,
) -> list[tuple[str, str, str, str | None]]:
    artifacts.mkdir(parents=True, exist_ok=True)
    outcomes = artifacts / f"{case.project}-mutation-outcomes.jsonl"
    outcomes.unlink(missing_ok=True)
    env = _environment(project, python, root)
    env["VFY_BENCHMARK_OUTCOMES"] = str(outcomes)
    plugin_root = stage_plugins(artifacts / "observer-plugins")
    env["PYTHONPATH"] = str(plugin_root) + os.pathsep + env["PYTHONPATH"]
    env["PYTEST_PLUGINS"] = OUTCOME_PLUGIN
    result = run_checked(
        [str(python), "-m", "pytest", "-q", case.test],
        cwd=root,
        env=env,
        timeout=600,
    )
    records = _successful_target_records(outcome_records(outcomes), case)
    portable = _portable_outcomes(records, root)
    (artifacts / f"{case.project}-mutation-upstream.json").write_text(
        json.dumps(
            {
                "command": [str(python), "-m", "pytest", "-q", case.test],
                "stdout": result.stdout,
                "stderr": result.stderr,
                "outcomes": records,
                "comparison_outcomes": portable,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return portable


def _prove_killers(proofs: _KillerProofs, baseline: VfySample, root: Path) -> None:
    """Independently check every claimed protected body mutation."""

    proofs.set_reference_report(baseline, root)
    for mutation in baseline["mutations"]:
        if mutation.get("status") != "protected":
            continue
        if mutation.get("owner", {}).get("phase") != "body":
            raise RuntimeError("protected definition mutation has no independent proof route")
        killer = mutation.get("killer")
        if not isinstance(killer, str) or not killer:
            raise RuntimeError("protected mutation has no killer")
        proofs.prove(root, mutation, [killer], label="real parity")


def _sample_time(row: dict[str, object], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"mutation sample has no numeric {key}")
    return float(value)


def _check_vfy_round(
    expected: VfySignature,
    actual: VfySignature,
    report: VfySample,
    *,
    root: Path,
    label: str,
    proofs: _KillerProofs,
) -> None:
    """Permit only independently proved body-killer reranking."""

    if actual != expected:
        _require_signature_match(
            expected, actual, report, root=root, label=label, killer_proofs=proofs
        )


def _retain_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fixture_adaptation(source: Path, copy: Path, case: RealMutationCase) -> dict[str, object]:
    if case.project == "datasette":
        relative = Path("tests/conftest.py")
        kind = "idempotent Datasette test plugin registration"
    elif case.project == "packaging":
        relative = Path("pyproject.toml")
        kind = "scope copied Packaging pytest configuration to upstream utility tests"
    else:
        return {"kind": "none"}
    original = (source / relative).read_text(encoding="utf-8")
    adapted = (copy / relative).read_text(encoding="utf-8")
    return {
        "kind": kind,
        "path": str(relative),
        "original_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "adapted_sha256": hashlib.sha256(adapted.encode()).hexdigest(),
        "diff": "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                adapted.splitlines(keepends=True),
                fromfile=f"upstream/{relative}",
                tofile=f"adapted/{relative}",
            )
        ),
    }


def _mutmut_dependency_overlay(python: Path, case: RealMutationCase) -> dict[str, object]:
    lock = REPO_ROOT / "benchmarks/locks/real-mutation-mutmut.txt"
    content = lock.read_text(encoding="utf-8")
    expected = dict(re.findall(r"^([a-z0-9-]+)==([^\s]+)", content, flags=re.MULTILINE))
    if not expected or "libcst" not in expected or "textual" not in expected:
        raise RuntimeError("mutmut dependency overlay is incomplete")
    additional_locks: list[dict[str, str]] = []
    if case.project == "packaging":
        supplement = REPO_ROOT / "benchmarks/locks/packaging-mutation-click.txt"
        extra = dict(re.findall(r"^([a-z0-9-]+)==([^\s]+)", supplement.read_text(), re.MULTILINE))
        if extra != {"click": "8.4.2"}:
            raise RuntimeError("Packaging mutmut dependency supplement changed")
        expected.update(extra)
        additional_locks.append(
            {
                "lock": str(supplement.relative_to(REPO_ROOT)),
                "sha256": hashlib.sha256(supplement.read_bytes()).hexdigest(),
            }
        )
    command = [
        str(python),
        "-c",
        "import importlib.metadata,json,sys; "
        "names=json.loads(sys.argv[1]); "
        "print(json.dumps({name:importlib.metadata.version(name) for name in names}))",
        json.dumps(sorted(expected)),
    ]
    installed = json.loads(run_checked(command, cwd=REPO_ROOT, env=os.environ.copy()).stdout)
    if installed != expected:
        raise RuntimeError("installed mutmut dependencies differ from hashed overlay lock")
    return {
        "lock": str(lock.relative_to(REPO_ROOT)),
        "sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "packages": installed,
        "additional_locks": additional_locks,
    }


def _prepared_vfy_files(
    prepared: dict[str, object],
    artifact: dict[str, str | None],
    python: Path,
    root: Path,
    project: ProjectManifest,
) -> object:
    # Load the optional coverage harness only when checking a prepared installation.
    from validation.pytest_runner.benchmark_vfy_coverage import (  # noqa: PLC0415
        interpreter_details,
        verify_installed_distribution_files,
    )

    build = prepared["vfy_build"]
    if not isinstance(build, dict) or build.get("wheel_sha256") != artifact["wheel_sha256"]:
        raise RuntimeError("prepared interpreter and controller use different release wheels")
    wheel = Path(str(build["wheel_path"]))
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != artifact["wheel_sha256"]:
        raise RuntimeError("prepared wheel changed during measurement")
    selected = interpreter_details(python, root, workflow_environment(project, python, root=root))
    verify_installed_distribution_files("real mutation", "vfy", selected, wheel)
    if selected.get("vfy_extension_sha256") != artifact["extension_sha256"]:
        raise RuntimeError("prepared interpreter and controller use different native artifacts")
    return selected["vfy_files"]


def _require_prepared_unchanged(
    prepared: dict[str, object],
    artifact: dict[str, str | None],
    python: Path,
    root: Path,
    project: ProjectManifest,
    before: object,
) -> None:
    if _prepared_vfy_files(prepared, artifact, python, root, project) != before:
        raise RuntimeError("prepared interpreter vfy payload changed during measurement")


def main() -> int:
    args = _args()
    artifact_before = (
        artifact_provenance(strict=True)
        if args.mode == BenchmarkMode.PUBLICATION.value and not args.validate_only
        else None
    )
    case = CASES[args.project]
    project = manifest(case.project)
    root = args.root.resolve()
    # Keep venv interpreter paths lexical: resolving their python symlink would
    # run the system interpreter without the prepared project dependencies.
    python = args.python.absolute()
    mutmut_python = args.mutmut_python.absolute()
    args.mutmut = str(args.mutmut.resolve())
    if mutmut_python != python:
        raise RuntimeError("mutmut and vfy must use the same prepared project interpreter")
    mutmut_revision, mutmut_tree, mutmut_dirty = repository_state(args.mutmut_repository.resolve())
    if mutmut_revision != MUTMUT_REVISION:
        raise RuntimeError(f"mutmut checkout is {mutmut_revision}; expected {MUTMUT_REVISION}")
    if mutmut_dirty:
        raise RuntimeError(f"mutmut checkout has uncommitted changes: {mutmut_dirty[:3]}")
    mutmut_overlay = _mutmut_dependency_overlay(python, case)
    prepared = validate_prepared_root(
        project, root, python, args.preparation_manifest, args.artifacts
    )
    selected_before = (
        _prepared_vfy_files(prepared, artifact_before, python, root, project)
        if artifact_before is not None
        else None
    )
    upstream_outcomes = _validate_upstream_test(project, case, root, python, args.artifacts)

    with tempfile.TemporaryDirectory(prefix=f"vfy-{case.project}-mutation-parity-") as temporary:
        parity_root = Path(temporary) / "project"
        _prepare_copy(root, parity_root, case)
        adaptation = _fixture_adaptation(root, parity_root, case)
        adapted_outcomes = _validate_upstream_test(
            project,
            case,
            parity_root,
            python,
            args.output.with_suffix(".artifacts") / "adapted-upstream",
        )
        if adapted_outcomes != upstream_outcomes:
            raise RuntimeError("adapted fixture changed the upstream test outcomes")
        source = parity_root / case.source
        if case.imported_module is not None or case.all_function_bodies:
            scope_ranges, scope_names = _function_body_ranges(parity_root, case)
            scope_unit = (
                "bodies of functions imported by upstream test module"
                if case.imported_module is not None
                else "bodies of all top-level functions"
            )
        else:
            scope = _function_scope(source, case.function, case.test)
            scope_ranges = [(scope.start_byte, scope.end_byte)]
            scope_names = [case.function]
            scope_unit = "function"
        byte_range = (
            min(item[0] for item in scope_ranges),
            max(item[1] for item in scope_ranges),
        )
        vfy_keys, _vfy_catalog_ms, vfy_ids = vfy_catalog(parity_root, (source,))
        mutmut_keys, _mutmut_catalog_ms, mutmut_names = mutmut_catalog(
            parity_root, str(mutmut_python), (source,)
        )

        def inside(key: tuple[str, int, int, str, str]) -> bool:
            return any(start <= key[1] < key[2] <= end for start, end in scope_ranges)

        vfy_scope = {key for key in vfy_keys if inside(key)}
        mutmut_scope = {key for key in mutmut_keys if inside(key)}
        common = vfy_scope & mutmut_scope
        if not common:
            raise RuntimeError("real project function has no exact common mutation catalog")
        names = [mutmut_names[key] for key in sorted(common)]
        env = _environment(project, python, parity_root)
        baseline = measured_vfy(
            parity_root,
            args,
            common,
            source_paths=(source,),
            pytest_args=(case.test,),
            python=str(python),
            child_environment=env,
            retain_process=True,
        )
        _retain_json(args.output.with_suffix(".artifacts") / "vfy-parity-raw.json", baseline)
        vfy_signature = _validated_vfy_signature(
            baseline, root=parity_root, expected_catalog=common, label="real vfy parity"
        )
        reference = measured_mutmut(
            parity_root, args, names, child_environment=env, retain_process=True
        )
        _retain_json(
            args.output.with_suffix(".artifacts") / "parity-raw-reports.json",
            {"vfy": baseline, "mutmut": reference},
        )
        mutmut_signature = _validated_mutmut_signature(
            reference, expected_names=names, label="real mutmut parity"
        )
        parity = compare_fixture_outcomes(
            baseline,
            reference,
            common_catalog=common,
            vfy_ids=vfy_ids,
            mutmut_names=mutmut_names,
        )
        _retain_json(
            args.output.with_suffix(".artifacts") / "parity-reports.json",
            {"vfy": baseline, "mutmut": reference, "comparison": parity},
        )
        proofs = _KillerProofs(
            args.output.with_suffix(".artifacts") / "killer-proofs",
            python=str(python),
            timeout=args.timeout,
            test_environment=lambda proof_root: _environment(project, python, proof_root),
        )
        _prove_killers(proofs, baseline, parity_root)

    samples: list[dict[str, object]] = []
    if not args.validate_only:
        for index in range(args.warmups + args.runs):
            order = ("vfy", "mutmut") if index % 2 == 0 else ("mutmut", "vfy")
            with tempfile.TemporaryDirectory(
                prefix=f"vfy-{case.project}-mutation-sample-"
            ) as temporary:
                sample_root = Path(temporary) / "project"
                _prepare_copy(root, sample_root, case)
                source = sample_root / case.source
                env = _environment(project, python, sample_root)
                row: dict[str, object] = {
                    "round": index + 1,
                    "order": list(order),
                    "phase": "warmup" if index < args.warmups else "measured",
                }
                for engine in order:
                    print(f"{case.project} mutation round {index + 1}: {engine}", flush=True)
                    started = time.perf_counter_ns()
                    if engine == "vfy":
                        sample = measured_vfy(
                            sample_root,
                            args,
                            common,
                            source_paths=(source,),
                            pytest_args=(case.test,),
                            python=str(python),
                            child_environment=env,
                            retain_process=True,
                            resource_path=(
                                args.output.with_suffix(".artifacts")
                                / "resources"
                                / f"round-{index + 1:03d}-vfy.json"
                                if args.tree_resources
                                else None
                            ),
                        )
                        workflow_ms = (time.perf_counter_ns() - started) / 1_000_000
                        row["vfy_ms"] = workflow_ms
                        row["vfy_command_ms"] = sample["wall_ms"]
                        row["vfy_report"] = sample
                        _retain_json(
                            args.output.with_suffix(".artifacts")
                            / "samples"
                            / f"round-{index + 1:03d}.json",
                            row,
                        )
                        signature = _validated_vfy_signature(
                            sample,
                            root=sample_root,
                            expected_catalog=common,
                            label=f"round {index + 1} vfy",
                        )
                        _check_vfy_round(
                            vfy_signature,
                            signature,
                            sample,
                            root=sample_root,
                            label=f"round {index + 1} vfy",
                            proofs=proofs,
                        )
                    else:
                        sample = measured_mutmut(
                            sample_root,
                            args,
                            names,
                            child_environment=env,
                            retain_process=True,
                            resource_path=(
                                args.output.with_suffix(".artifacts")
                                / "resources"
                                / f"round-{index + 1:03d}-mutmut.json"
                                if args.tree_resources
                                else None
                            ),
                        )
                        workflow_ms = (time.perf_counter_ns() - started) / 1_000_000
                        row["mutmut_ms"] = workflow_ms
                        row["mutmut_command_ms"] = sample["wall_ms"]
                        row["mutmut_report"] = sample
                        _retain_json(
                            args.output.with_suffix(".artifacts")
                            / "samples"
                            / f"round-{index + 1:03d}.json",
                            row,
                        )
                        signature = _validated_mutmut_signature(
                            sample, expected_names=names, label=f"round {index + 1} mutmut"
                        )
                        if signature != mutmut_signature:
                            raise RuntimeError(
                                f"round {index + 1} mutmut mutation outcomes changed"
                            )
                    _retain_json(
                        args.output.with_suffix(".artifacts")
                        / "samples"
                        / f"round-{index + 1:03d}.json",
                        row,
                    )
                samples.append(row)

    if artifact_before is not None:
        require_unchanged_artifact(artifact_before)
        _require_prepared_unchanged(
            prepared, artifact_before, python, root, project, selected_before
        )
    result = {
        "schema": "vfy.real-mutation-benchmark",
        "project": case.project,
        "project_revision": project.revision,
        "scope": {
            "source": str(case.source),
            "function": case.function,
            "test": case.test,
            "unit": scope_unit,
            "functions": scope_names,
            "function_byte_ranges": [list(item) for item in scope_ranges],
            "byte_range": list(byte_range),
        },
        "catalog": {
            "vfy": len(vfy_scope),
            "mutmut": len(mutmut_scope),
            "common": len(common),
            "identities": [list(key) for key in sorted(common)],
        },
        "parity": parity,
        "vfy_outcomes": baseline["summary"],
        "mutmut_outcomes": reference["summary"],
        "upstream_outcomes": upstream_outcomes,
        "fixture_adaptation": adaptation,
        "killer_proofs": proofs.records(),
        "preparation": prepared,
        "mutmut_revision": mutmut_revision,
        "mutmut_tree": mutmut_tree,
        "mutmut_dependency_overlay": mutmut_overlay,
        "mutmut_config": {
            "source_paths": [case.source_path],
            "only_mutate": [str(case.source)],
            "pytest_target": case.test,
        },
        "methodology": {
            "mode": args.mode,
            "engine": args.vfy_route,
            "test_isolation": {
                "mode": args.test_isolation,
                "cli_arguments": list(_test_isolation_arguments(args)),
            },
            "warmups": args.warmups,
            "runs": args.runs,
            "statistic": "median",
            "rotating_order": True,
            "resources": tree_resource_methodology() if args.tree_resources else None,
            "boundary": (
                "fresh copied project; each tool's complete measured helper call, including "
                "vfy test selection and catalog mapping, or mutmut's equivalent internal "
                "setup, plus its cold mutation command and result parsing"
            ),
        },
        "samples": samples,
        "recorded_at_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "provenance": build_provenance(
            REPO_ROOT,
            workload={"project": case.project, "scope": case.function, "catalog": len(common)},
            workers=args.workers,
            packages=("vfy", "pytest", "coverage"),
            publication=args.mode == BenchmarkMode.PUBLICATION.value and not args.validate_only,
        ),
        "validation": {"status": "passed"},
    }
    measured = [row for row in samples if row["phase"] == "measured"]
    if measured:
        vfy_times = [_sample_time(row, "vfy_ms") for row in measured]
        mutmut_times = [_sample_time(row, "mutmut_ms") for row in measured]
        vfy_timing = summarize(vfy_times)
        mutmut_timing = summarize(mutmut_times)
        result["timing"] = {
            "vfy": vfy_timing,
            "mutmut": mutmut_timing,
            "ratio_of_medians": mutmut_timing["median_ms"] / vfy_timing["median_ms"],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"validation": "passed", "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
