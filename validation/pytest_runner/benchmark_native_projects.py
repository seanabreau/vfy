"""Parity-gated public command latency on bounded, pinned project suites.

Full stock execution chooses the scope before native results are inspected.
Observed preflights compare complete phase records. Sampled commands omit the
stock observer and requested native JSON, retain common JUnit reports, and
recheck ordered case identities, outcomes, and exit status outside the timer.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import shutil
import subprocess
import sys
import tokenize
from dataclasses import asdict, replace
from io import StringIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SOURCE_INPUTS = (
    "validation/pytest_runner/benchmark_native_projects.py",
    "validation/pytest_runner/benchmark_child_resources.py",
    "validation/pytest_runner/check_native_adoption_matrix.py",
    "validation/pytest_runner/adoption_observations.py",
    "validation/pytest_runner/check_native_project.py",
    "validation/pytest_runner/benchmark_vfy_coverage.py",
    "validation/pytest_runner/benchmark_real_candidates.py",
    "validation/pytest_runner/patches/rich-deterministic-workload.patch",
    "benchmarks/benchmark_harness.py",
    "benchmarks/benchmark_provenance.py",
    "benchmarks/benchmark_projects.py",
    "benchmarks/project_manifest.py",
    "benchmarks/rich_clock_plugin.py",
    "scripts/process_tree.py",
    "scripts/pytest_execution_record.py",
    "scripts/pytest_execution_observations.py",
    "scripts/pytest_execution_probe.py",
)
# Capture source inputs before importing the route implementations.
SOURCE_INPUT_SHA256 = {
    name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_INPUTS
}

from benchmarks.benchmark_harness import (  # noqa: E402
    SamplingConfig,
    add_sampling_arguments,
    rotated_orders,
    sampling_config,
    summarize,
)
from benchmarks.benchmark_provenance import hardware_details, require_clean  # noqa: E402
from scripts.pytest_execution_record import (  # noqa: E402
    file_sha256,
    interpreter_metadata,
    write_atomic,
)
from validation.pytest_runner.benchmark_real_candidates import (  # noqa: E402
    cli_selectors,
    require_controller_python,
)
from validation.pytest_runner.benchmark_vfy_coverage import (  # noqa: E402
    interpreter_details,
    verify_build_identity,
    verify_installed_distribution_files,
)
from validation.pytest_runner.check_native_adoption_matrix import (  # noqa: E402
    PROJECT_EXPECTATIONS,
    PROJECTS_BY_KEY,
    Project,
    checkout,
    compare_outcomes,
    coverage_command,
    coverage_result,
    coverage_result_errors,
    coverage_route_comparison_errors,
    coverage_run,
    git,
    invoke,
    junit_outcomes,
    native_outcome_command,
    native_outcome_run,
    normalize_outcome_record,
    outcome_parity_errors,
    prepare_project,
    prepare_python,
    project_environment,
    stock_outcome_command,
    stock_outcome_run,
    verify_outcome_artifacts,
)


def select_inventory(inventory: list[str], limit: int) -> tuple[str, ...]:
    """Keep evenly spaced collected cases, including skips and expected failures."""
    if limit < 1 or not inventory or len(set(inventory)) != len(inventory):
        raise ValueError("scope requires a positive limit and a nonempty unique stock inventory")
    count = min(limit, len(inventory))
    if count == 1:
        return (inventory[0],)
    return tuple(inventory[index * (len(inventory) - 1) // (count - 1)] for index in range(count))


def restore_project(root: Path, snapshot: Path) -> None:
    """Restore all prepared source and fixture files outside command timing."""
    basetemp = root.parent / "pytest-tmp"
    if basetemp.exists():
        shutil.rmtree(basetemp)
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(snapshot, root, symlinks=True)


def checkout_local(project: Project, destination: Path, source: Path) -> Path:
    """Use a pinned local Git worktree when network cloning is unavailable."""
    source = source.resolve()
    if git(source, "rev-parse", "HEAD") != project.revision:
        raise ValueError(f"{project.key}: local source is not at the pinned revision")
    subprocess.run(["git", "-C", str(source), "diff", "--quiet", "HEAD", "--"], check=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "worktree",
            "add",
            "--detach",
            str(destination),
            project.revision,
        ],
        check=True,
        timeout=120,
    )
    if git(destination, "status", "--porcelain"):
        raise ValueError(f"{project.key}: local worktree is not clean")
    return destination


def snapshot_inputs(snapshot: Path) -> dict[str, str]:
    """Bind prepared fixtures and generated files, excluding the Git object store."""
    return {
        path.relative_to(snapshot).as_posix(): file_sha256(path)
        for path in sorted(snapshot.rglob("*"))
        if ".git" not in path.relative_to(snapshot).parts and path.is_file()
    }


def archive_run(output: Path) -> dict[str, Any]:
    """Retain raw evidence with hashes, compressing large files after validation."""
    artifacts = {}
    for path in sorted(output.iterdir()):
        if not path.is_file():
            continue
        digest, size = file_sha256(path), path.stat().st_size
        retained = path
        if size >= 64 * 1024:
            retained = path.with_name(f"{path.name}.gz")
            with (
                path.open("rb") as source,
                retained.open("wb") as destination,
                gzip.GzipFile(fileobj=destination, mode="wb", filename="", mtime=0) as stream,
            ):
                shutil.copyfileobj(source, stream)
            path.unlink()
        artifacts[path.name] = {
            "path": str(retained),
            "sha256": digest,
            "bytes": size,
            "retained_sha256": file_sha256(retained),
            "retained_bytes": retained.stat().st_size,
        }
    write_atomic(output / "artifacts.json", artifacts)
    return artifacts


def prepare_benchmark_inputs(
    project: Project, root: Path, *, rich_clock: bool = False
) -> dict[str, int | str]:
    """Apply deterministic test inputs equally to all benchmark routes."""
    if project.key == "rich" and rich_clock:
        conftest = root / "tests/conftest.py"
        original = conftest.read_text(encoding="utf-8")
        if "freeze_rich_log_clock" in original:
            raise ValueError("Rich already defines the benchmark clock fixture")
        fixture_source = (ROOT / "benchmarks/rich_clock_plugin.py").read_text(encoding="utf-8")
        marker = "import logging\n"
        if fixture_source.count(marker) != 1:
            raise ValueError("Rich clock fixture source has changed unexpectedly")
        fixture = marker + fixture_source.split(marker, 1)[1]
        conftest.write_text(original.rstrip() + "\n\n" + fixture, encoding="utf-8")
        return {
            "method": "scoped clock and FileProxy cleanup fixtures in Rich tests/conftest.py",
            "conftest": "tests/conftest.py",
            "fixture_source_sha256": file_sha256(ROOT / "benchmarks/rich_clock_plugin.py"),
            "conftest_sha256": file_sha256(conftest),
        }
    if project.key not in {"attrs", "python-dateutil"}:
        return {}
    conftest = root / "tests/conftest.py"
    source = conftest.read_text(encoding="utf-8")
    reserved = {"_vfy_benchmark_hookimpl", "pytest_vfy_benchmark_seed"}
    if any(
        token.type == tokenize.NAME and token.string in reserved
        for token in tokenize.generate_tokens(StringIO(source).readline)
    ):
        raise ValueError("benchmark seed preparation collides with an existing conftest name")
    hook = """\n\nfrom pytest import hookimpl as _vfy_benchmark_hookimpl


@_vfy_benchmark_hookimpl(specname="pytest_collection_modifyitems")
def pytest_vfy_benchmark_seed(items):
    from hypothesis import seed as _vfy_benchmark_seed

    for item in items:
        obj = item.obj
        function = getattr(obj, "__func__", obj)
        if getattr(function, "is_hypothesis_test", False):
            _vfy_benchmark_seed(0)(function)
"""
    conftest.write_text(source + hook, encoding="utf-8")
    return {
        "method": "public hypothesis.seed after collection",
        "seed": 0,
        "conftest": "tests/conftest.py",
        "hook": "pytest_vfy_benchmark_seed",
    }


def prepare_runtime(
    project: Project, work: Path, output: Path, args: argparse.Namespace
) -> tuple[Path, Path, Path, dict[str, Any]]:
    root, snapshot = work / "project", work / "snapshot"
    prepared_path = work / "prepared.json"
    lock = (
        project.dependency_lock
        or ROOT / "validation/pytest_runner/native-adoption-requirements.txt"
    )
    local_source = getattr(args, "local_checkouts", {}).get(project.key)
    preparation_inputs = {
        "project": json.loads(json.dumps(asdict(project), default=str)),
        "dependency_lock": str(lock.absolute()),
        "dependency_lock_sha256": file_sha256(lock),
        "python_version": args.python_version,
        "preparation_source_sha256": SOURCE_INPUT_SHA256[
            "validation/pytest_runner/check_native_adoption_matrix.py"
        ],
        "benchmark_preparation_source_sha256": SOURCE_INPUT_SHA256[
            "validation/pytest_runner/benchmark_native_projects.py"
        ],
        "local_checkout": str(local_source.resolve()) if local_source is not None else None,
        "rich_clock_fixture_sha256": (
            file_sha256(ROOT / "benchmarks/rich_clock_plugin.py")
            if project.key == "rich" and getattr(args, "rich_clock", False)
            else None
        ),
    }
    if project.key == "rich":
        preparation_inputs["workload_patch_sha256"] = SOURCE_INPUT_SHA256[
            "validation/pytest_runner/patches/rich-deterministic-workload.patch"
        ]
    if prepared_path.is_file():
        prepared = json.loads(prepared_path.read_text())
        if (
            prepared["revision"] != project.revision
            or prepared["wheel_sha256"] != file_sha256(args.wheel)
            or prepared.get("preparation_inputs") != preparation_inputs
            or prepared["snapshot_inputs"] != snapshot_inputs(snapshot)
        ):
            raise ValueError("prepared project, fixture snapshot, or candidate wheel changed")
        python = Path(prepared["python"])
        restore_project(root, snapshot)
    else:
        if work.exists():
            raise FileExistsError(f"incomplete preparation is retained at {work}")
        root = (
            checkout_local(project, root, local_source)
            if local_source is not None
            else checkout(project, root)
        )
        python = prepare_python(project, work, output, args.wheel, args.python_version)
        prepare_project(project, root, python, output / "setup", args.timeout)
        benchmark_inputs = prepare_benchmark_inputs(
            project, root, rich_clock=getattr(args, "rich_clock", False)
        )
        for bytecode in root.rglob("__pycache__"):
            shutil.rmtree(bytecode)
        shutil.copytree(root, snapshot, symlinks=True)
        prepared = {
            "revision": project.revision,
            "python": str(python),
            "wheel_sha256": file_sha256(args.wheel),
            "snapshot_inputs": snapshot_inputs(snapshot),
            "setup_diff": git(root, "diff"),
            "benchmark_inputs": benchmark_inputs,
            "preparation_inputs": preparation_inputs,
        }
        write_atomic(prepared_path, prepared)
    if git(root, "rev-parse", "HEAD") != project.revision:
        raise ValueError("upstream revision differs from the pinned workload")
    installed = interpreter_details(python, root, project_environment(project, root, python))
    require_controller_python(installed)
    if installed.get("pytest") != "9.1.1":
        raise ValueError("prepared runtime requires pinned pytest 9.1.1")
    verify_installed_distribution_files(project.key, "vfy", installed, args.wheel)
    build = verify_build_identity(args.build_manifest, installed, args.wheel, retained=True)
    return root, snapshot, python, {"prepared": prepared, "installed": installed, "build": build}


def preflight_outcomes(
    project: Project,
    root: Path,
    snapshot: Path,
    python: Path,
    output: Path,
    timeout: float,
    wheel: Path,
    selected: tuple[str, ...],
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    basetemp = root.parent / "pytest-tmp"
    purelib = interpreter_metadata(python, "vfy")["purelib"]
    runs = {}
    for role, workers in (("stock", 1), ("native1", 1), ("native2", 2)):
        restore_project(root, snapshot)
        destination = output / role
        if role == "stock":
            run, raw = stock_outcome_run(project, root, python, destination, basetemp, timeout)
        else:
            run, raw = native_outcome_run(
                project, root, python, destination, basetemp, timeout, workers=workers
            )
        verify_outcome_artifacts(run, purelib, wheel, "stock" if role == "stock" else "native")
        runs[role] = run, raw
    stock_run, stock_raw = runs["stock"]
    _, _, stock_inventory = normalize_outcome_record(
        "stock", stock_raw, stock_run, root, python, basetemp
    )
    if tuple(stock_inventory) != selected:
        raise ValueError("stock subset inventory differs from the declared scope")
    if stock_run["exit_status"] != 0 or stock_run["record_errors"]:
        raise ValueError("stock subset did not complete successfully")
    expected = junit_outcomes(output / "stock/junit.xml")
    if not expected:
        raise ValueError("stock subset has no JUnit cases")
    roles, comparisons = ["stock"], {}
    for role in ("native1", "native2"):
        run, raw = runs[role]
        comparison = compare_outcomes(
            stock_run, run, stock_raw, raw, root, root, python, basetemp, project=project
        )
        errors = outcome_parity_errors(comparison, role)
        if not errors and junit_outcomes(output / role / "junit.xml") != expected:
            errors.append("ordered JUnit outcomes differ from stock subset")
        comparisons[role] = {"comparison": comparison, "errors": errors}
        if not errors:
            roles.append(role)
    write_atomic(output / "parity.json", comparisons)
    for role in runs:
        archive_run(output / role)
    return roles, expected, comparisons


def preflight_coverage(
    project: Project,
    root: Path,
    snapshot: Path,
    python: Path,
    output: Path,
    timeout: float,
    roles: list[str],
    expected: list[dict[str, Any]],
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    expectation = PROJECT_EXPECTATIONS[project.key]
    if expectation.coverage == "same-run-report":
        return [], {}, {"blocked": "no cross-route coverage data parity contract"}
    records, errors = {}, {}
    for role in roles:
        restore_project(root, snapshot)
        run, record = coverage_run(
            project,
            root,
            python,
            output / role,
            "pytest" if role == "stock" else "native",
            timeout,
            workers=2 if role == "native2" else 1,
            basetemp=root.parent / "pytest-tmp",
        )
        failures = list(run["record_errors"])
        if run["exit_status"] != 0 or run["junit_outcomes"] != expected:
            failures.append("coverage status or ordered JUnit outcomes differ from stock subset")
        failures.extend(coverage_result_errors(record, role))
        records[role], errors[role] = record, failures
    eligible = ["stock"] if not errors["stock"] else []
    for role in roles[1:]:
        errors[role].extend(
            coverage_route_comparison_errors(records["stock"], records[role], role, expectation)
        )
        if eligible and not errors[role]:
            eligible.append(role)
    write_atomic(output / "parity.json", {"contract": expectation.coverage, "errors": errors})
    artifacts = {role: archive_run(output / role) for role in roles}
    return (
        eligible,
        records,
        {"contract": expectation.coverage, "errors": errors, "artifacts": artifacts},
    )


def invoke_with_resources(
    command: list[str],
    root: Path,
    environment: dict[str, str],
    destination: Path,
    timeout: float,
    *,
    tree: bool = False,
) -> dict[str, Any]:
    """Use the committed collector while retaining both process boundaries."""
    collector = Path(__file__).with_name("benchmark_child_resources.py")
    wrapper = [
        sys.executable,
        str(collector),
        *(["--tree", "--sample-interval-ms", "20"] if tree else []),
        "--record",
        str(destination / "resources.json"),
        "--stdout",
        str(destination / "stdout.txt"),
        "--stderr",
        str(destination / "stderr.txt"),
        "--",
        *command,
    ]
    run = invoke(wrapper, root, environment, destination / "collector", timeout)
    resources = json.loads((destination / "resources.json").read_text())
    wrapper_status = json.loads((destination / "collector/status.json").read_text())
    wrapper_command = json.loads((destination / "collector/command.json").read_text())
    if (
        resources.get("schema_version") != 1
        or resources.get("method")
        != ("linux-process-tree-smaps-rollup" if tree else "linux-single-child-rusage")
        or resources.get("collector_sha256") != file_sha256(collector)
        or resources.get("child_command") != command
        or type(resources.get("child_exit_status")) is not int
        or resources["child_exit_status"] != run["exit_status"]
        or wrapper_status.get("completed") is not True
        or type(wrapper_status.get("exit_status")) is not int
        or wrapper_status["exit_status"] != run["exit_status"]
        or wrapper_status.get("seconds") != run["seconds"]
        or wrapper_command.get("argv") != wrapper
        or wrapper_command.get("cwd") != str(root)
    ):
        raise ValueError("resource record identifies another child or collector")
    for field in (
        "child_wall_ms",
        "user_cpu_ms",
        "system_cpu_ms",
        "total_cpu_ms",
        "peak_rss_bytes",
    ):
        value = resources.get(field)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"invalid child resource field: {field}")
    if resources["child_wall_ms"] <= 0 or resources["peak_rss_bytes"] <= 0:
        raise ValueError("child wall time and peak RSS must be positive")
    if tree:
        if (
            resources.get("sample_interval_ms") != 20
            or resources.get("tree_scope")
            != "sum of simultaneous live child and descendant PSS; collector excluded"
        ):
            raise ValueError("tree resource method or scope differs")
        for field in ("peak_tree_pss_bytes", "peak_tree_rss_bytes", "snapshot_count"):
            value = resources.get(field)
            if type(value) is not int or value <= 0:
                raise ValueError(f"invalid tree resource field: {field}")
    if not math.isclose(
        resources["total_cpu_ms"],
        resources["user_cpu_ms"] + resources["system_cpu_ms"],
        rel_tol=1e-9,
        abs_tol=1e-6,
    ):
        raise ValueError("child total CPU disagrees with user and system CPU")
    child = {
        "completed": True,
        "exit_status": resources["child_exit_status"],
        "seconds": resources["child_wall_ms"] / 1_000,
    }
    write_atomic(
        destination / "command.json",
        {"argv": command, "cwd": str(root), "timeout_seconds": timeout},
    )
    write_atomic(destination / "status.json", child)
    return {
        **child,
        "resources": resources,
        "outer_wrapper_seconds": run["seconds"],
        "collector_artifacts": archive_run(destination / "collector"),
    }


def sample_commands(
    project: Project,
    root: Path,
    snapshot: Path,
    python: Path,
    output: Path,
    roles: list[str],
    expected: list[dict[str, Any]],
    sampling: SamplingConfig,
    timeout: float,
    reference_coverage: dict[str, Any] | None = None,
    *,
    collect_resources: bool = False,
) -> dict[str, Any]:
    samples = {role: [] for role in roles}
    records = []
    for phase, count in (("warmup", sampling.warmups), ("sample", sampling.runs)):
        for index, order in enumerate(rotated_orders(roles, count)):
            for role in order:
                restore_project(root, snapshot)
                destination = output / f"{phase}-{index:02d}-{role}"
                environment = project_environment(project, root, python)
                workers = 2 if role == "native2" else 1
                if reference_coverage is not None:
                    command = coverage_command(
                        project,
                        root,
                        python,
                        destination,
                        "pytest" if role == "stock" else "native",
                        workers=workers,
                        basetemp=root.parent / "pytest-tmp",
                    )
                    environment["COVERAGE_FILE"] = str(destination / ".coverage")
                elif role == "stock":
                    command = stock_outcome_command(
                        project,
                        root,
                        python,
                        destination,
                        root.parent / "pytest-tmp",
                        observer="disabled",
                    )
                else:
                    command = native_outcome_command(
                        project,
                        root,
                        python,
                        destination,
                        root.parent / "pytest-tmp",
                        workers=workers,
                    )
                runner = invoke_with_resources if collect_resources else invoke
                run = runner(command, root, environment, destination, timeout)
                if run["exit_status"] != 0 or junit_outcomes(destination / "junit.xml") != expected:
                    raise ValueError(f"sample ordered outcomes or status changed: {destination}")
                if reference_coverage is not None:
                    coverage = coverage_result(root, destination, retention="raw")
                    errors = coverage_result_errors(coverage, role)
                    errors.extend(
                        coverage_route_comparison_errors(
                            reference_coverage[role],
                            coverage,
                            role,
                            PROJECT_EXPECTATIONS[project.key],
                        )
                    )
                    if errors:
                        raise ValueError(f"sample coverage changed: {destination}: {errors}")
                records.append(
                    {
                        "phase": phase,
                        "index": index,
                        "role": role,
                        "seconds": run["seconds"],
                        "artifacts": archive_run(destination),
                        **{
                            name: run[name]
                            for name in (
                                "resources",
                                "outer_wrapper_seconds",
                                "collector_artifacts",
                            )
                            if name in run
                        },
                    }
                )
                if phase == "sample":
                    samples[role].append(run["seconds"] * 1000)
                write_atomic(output / "samples.json", {"samples": records})
    return {
        "samples": records,
        "summary": {role: summarize(values) for role, values in samples.items()},
    }


def run_project(args: argparse.Namespace, key: str, sampling: SamplingConfig) -> dict[str, Any]:
    project = PROJECTS_BY_KEY[key]
    requested_tests = getattr(args, "test_path", None)
    if requested_tests:
        project = replace(project, tests=tuple(requested_tests))
    output = args.output / key
    root, snapshot, python, identity = prepare_runtime(project, args.work_root / key, output, args)
    write_atomic(output / "identity.json", identity)
    restore_project(root, snapshot)
    full_output = output / "stock-full-inventory"
    full_run, full_raw = stock_outcome_run(
        project, root, python, full_output, root.parent / "pytest-tmp", args.timeout
    )
    purelib = interpreter_metadata(python, "vfy")["purelib"]
    verify_outcome_artifacts(full_run, purelib, args.wheel, "stock")
    _, _, inventory = normalize_outcome_record(
        "stock", full_raw, full_run, root, python, root.parent / "pytest-tmp"
    )
    if full_run["exit_status"] != 0 or full_run["record_errors"]:
        raise ValueError("full stock inventory run failed")
    selected = select_inventory(inventory, len(inventory) if args.full_suite else args.cases)
    scoped = (
        project
        if args.full_suite
        else replace(project, tests=cli_selectors(project, root, selected))
    )
    write_atomic(
        output / "scope.json",
        {
            "mode": (
                "full-requested-paths"
                if requested_tests
                else "full-suite"
                if args.full_suite
                else "bounded"
            ),
            "inventory": inventory,
            "selected": selected,
            "selectors": scoped.tests,
            "stock_record_sha256": file_sha256(full_output / "execution.json"),
        },
    )
    archive_run(full_output)
    roles, expected, comparisons = preflight_outcomes(
        scoped,
        root,
        snapshot,
        python,
        output / "outcome-preflight",
        args.timeout,
        args.wheel,
        selected,
    )
    verdicts = {
        role: {"errors": record["errors"], "passed": not record["errors"]}
        for role, record in comparisons.items()
    }
    row = {
        "project": key,
        "selected_cases": len(selected),
        "scope": {"path": str(output / "scope.json"), "sha256": file_sha256(output / "scope.json")},
        "identity": {
            "path": str(output / "identity.json"),
            "sha256": file_sha256(output / "identity.json"),
        },
        "outcome_parity": {
            "verdicts": verdicts,
            "path": str(output / "outcome-preflight/parity.json"),
            "sha256": file_sha256(output / "outcome-preflight/parity.json"),
        },
        "blocked_routes": [role for role in ("native1", "native2") if role not in roles],
    }
    if len(roles) < 2:
        return {**row, "status": "blocked", "reason": "no native route passed strict subset parity"}
    coverage_roles, coverage_records, coverage_proof = [], {}, {}
    if args.coverage:
        coverage_roles, coverage_records, coverage_proof = preflight_coverage(
            scoped,
            root,
            snapshot,
            python,
            output / "coverage-preflight",
            args.timeout,
            roles,
            expected,
        )
    row["coverage_parity"] = coverage_proof
    sampling_failure: dict[str, str] | None = None
    if not args.validate_only:
        phase = "no_coverage"
        try:
            row["no_coverage"] = sample_commands(
                scoped,
                root,
                snapshot,
                python,
                output / "no-coverage",
                roles,
                expected,
                sampling,
                args.timeout,
                **({"collect_resources": True} if getattr(args, "resources", False) else {}),
            )
            if len(coverage_roles) > 1:
                phase = "coverage"
                row["coverage"] = sample_commands(
                    scoped,
                    root,
                    snapshot,
                    python,
                    output / "coverage",
                    coverage_roles,
                    expected,
                    sampling,
                    args.timeout,
                    coverage_records,
                )
        except Exception as error:  # noqa: BLE001 - retain completed modes and failed raw samples.
            sampling_failure = {
                "failed_phase": phase,
                "error": f"{type(error).__name__}: {error}",
                "artifacts": str(output / phase.replace("_", "-")),
            }
    restore_project(root, snapshot)
    final_identity = interpreter_details(python, root, project_environment(project, root, python))
    if final_identity != identity["installed"]:
        row.pop("no_coverage", None)
        row.pop("coverage", None)
        if sampling_failure is not None:
            row["sampling_failure"] = sampling_failure
        return {
            **row,
            "status": "blocked",
            "failed_phase": "runtime_verification",
            "error": "ValueError: installed runtime changed during the project benchmark",
            "artifacts": str(output),
        }
    if sampling_failure is not None:
        return {**row, "status": "incomplete", **sampling_failure}
    return {**row, "status": "validated" if args.validate_only else "measured"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--build-manifest", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", choices=sorted(PROJECTS_BY_KEY), action="append")
    parser.add_argument("--python-version", default="3.12")
    parser.add_argument("--cases", type=int, default=100)
    parser.add_argument(
        "--test-path",
        action="append",
        help="retain the complete inventory from each requested upstream test path",
    )
    parser.add_argument(
        "--resources",
        action="store_true",
        help="retain Linux child wall, CPU, and peak RSS for every no-coverage sample",
    )
    parser.add_argument(
        "--full-suite",
        action="store_true",
        help="use the complete stock inventory and the project's configured test selectors",
    )
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    add_sampling_arguments(parser)
    args = parser.parse_args()
    if not 1 <= args.cases <= 100:
        parser.error("--cases must be between 1 and 100")
    if args.test_path and (not args.full_suite or not args.project or len(args.project) != 1):
        parser.error("--test-path requires --full-suite and exactly one --project")
    if args.test_path and len(set(args.test_path)) != len(args.test_path):
        parser.error("test paths must be unique")
    if args.test_path and any(
        Path(value).is_absolute() or ".." in Path(value).parts or "::" in value
        for value in args.test_path
    ):
        parser.error("test paths must be relative upstream paths without node selectors")
    if args.resources and (sys.platform != "linux" or args.coverage):
        parser.error("--resources requires Linux without --coverage")
    sampling = sampling_config(args)
    controller = require_clean(ROOT)
    for name in ("wheel", "build_manifest", "work_root", "output"):
        setattr(args, name, getattr(args, name).absolute())
    args.output.mkdir(parents=True, exist_ok=False)
    selected = args.project or list(PROJECTS_BY_KEY)
    if len(selected) != len(set(selected)):
        parser.error("project selections must be unique")
    source_inputs = {
        **SOURCE_INPUT_SHA256,
        **{
            str(lock.relative_to(ROOT)): file_sha256(lock)
            for key in selected
            for lock in (
                PROJECTS_BY_KEY[key].dependency_lock
                or ROOT / "validation/pytest_runner/native-adoption-requirements.txt",
            )
        },
    }
    result = {
        "controller": controller.as_dict(),
        "source_input_sha256": source_inputs,
        "machine": hardware_details(),
        "sampling": sampling.as_dict(),
        "projects": [],
        "method": {
            "scope": (
                "full stock-collected inventory for explicit upstream test paths"
                if args.test_path
                else "full stock-collected inventory with the project's configured test selectors"
                if args.full_suite
                else (
                    f"at most {args.cases} evenly spaced stock-collected cases, "
                    "without outcome filtering"
                )
            ),
            "test_paths": args.test_path,
            "resources": args.resources,
            "timer": (
                "public child spawn/capture/drain/reap; collector startup, validation, "
                "and snapshot restoration excluded; outer wrapper retained separately"
                if args.resources
                else "fresh public child command, including spawn/capture/drain/reap; "
                "validation and snapshot restoration excluded"
            ),
            "evidence": (
                "preflight observer and requested native JSON removed from measured runs; "
                "common ordered JUnit outcomes/status rechecked every run; "
                "intrinsic native controller history/results retained"
            ),
            "cache": (
                "prepared sources/fixtures restored before each child; project bytecode "
                "and pytest caches cold, installed environment and OS caches warmed"
            ),
            "generated_inputs": (
                "Attrs and Dateutil use public Hypothesis seed 0 after collection; "
                "upstream hooks, profiles, and cases are preserved in the prepared snapshot"
            ),
            "coverage": (
                "stock pytest and native use vfy coverage, test contexts, JSON and JUnit; "
                "exact or explicitly aggregate contract; same-run-only coverage blocked"
            ),
        },
    }
    write_atomic(args.output / "inputs.json", result)
    for key in selected:
        try:
            row = run_project(args, key, sampling)
        except Exception as error:  # noqa: BLE001 - preserve blocked routes and every partial artifact.
            row = {
                "project": key,
                "status": "blocked",
                "error": f"{type(error).__name__}: {error}",
                "artifacts": str(args.output / key),
                "work_root": str(args.work_root / key),
            }
        result["projects"].append(row)
        for name, digest in source_inputs.items():
            if file_sha256(ROOT / name) != digest:
                raise ValueError(f"source input changed during execution: {name}")
        write_atomic(args.output / "result.json", result)
        sys.stdout.write(f"{key}: {row['status']}\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
