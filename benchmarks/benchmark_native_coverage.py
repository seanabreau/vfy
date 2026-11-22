"""Compare serial Coverage.py and vfy collection through vfy's independent frontend."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from validation.pytest_runner.benchmark_native_projects import archive_run
from validation.pytest_runner.benchmark_native_xdist import Route, independent_execution
from validation.pytest_runner.check_native_xdist_adoption import junit_records

from benchmarks.benchmark_harness import (
    benchmark_environment,
    rotated_orders,
    summarize,
    timed_process,
)
from benchmarks.benchmark_projects import (
    GeneratedFiles,
    _independent_coverage_command,
    _validate_coverage_reports,
    first_schema7_difference,
    independent_coverage_artifact,
    normalized_data,
    outcome_records,
    preparation_metadata,
    selected_interpreter_metadata,
    sequence_commands,
)
from benchmarks.benchmark_provenance import (
    JSONValue,
    prepared_project_provenance,
    require_clean,
    require_revision,
)
from benchmarks.observer_plugins import (
    NATIVE_REFERENCE_PLUGIN,
    PLUGIN_RECEIPT,
    RICH_CLOCK_PLUGIN,
    stage_plugins,
)
from benchmarks.project_manifest import PROJECTS, ProjectManifest

ROLES = ("coverage.py-independent", "vfy-independent", "plain-independent")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", choices=sorted(PROJECTS), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--preparation-manifest", type=Path, required=True)
    parser.add_argument("--build-manifest", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mode", choices=("diagnostic", "publication"), default="publication")
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--runs", type=int, default=15)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--coverage-subprocess",
        action="store_true",
        help="derive one shared coverage configuration enabling subprocess coverage",
    )
    parser.add_argument(
        "--timing-route",
        action="append",
        choices=ROLES,
        help="time only these routes; all three routes still pass complete preflight",
    )
    parser.add_argument(
        "--compress-samples",
        action="store_true",
        help="gzip large validated warmup and sample artifacts outside the timer",
    )
    parser.add_argument(
        "--rich-clock",
        action="store_true",
        help="load the scoped Rich clock and FileProxy cleanup fixtures for every route",
    )
    return parser.parse_args()


def coverage_inputs(project: ProjectManifest, root: Path) -> tuple[list[str] | None, Path | None]:
    sources: list[str] = []
    config: Path | None = None
    for argument in project.coverage_args:
        if argument == "--cov":
            if sources:
                raise ValueError("ambiguous coverage source configuration")
            return None, config
        if argument.startswith("--cov="):
            sources.append(argument.partition("=")[2])
        elif argument.startswith("--cov-config="):
            config = root / argument.partition("=")[2]
        elif argument != "--cov-context=test":
            raise ValueError(f"unsupported native reference coverage option: {argument}")
    if project.key == "rich":
        config = Path(__file__).resolve().parent / "configs/rich.coveragerc"
    return sources, config


def command_for(
    role: str,
    project: ProjectManifest,
    python: Path,
    root: Path,
    destination: Path,
    *,
    cov_config: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    sources, config = coverage_inputs(project, root)
    if cov_config is not None:
        config = cov_config
    commands = sequence_commands(
        str(python),
        project,
        plugin="vfy.pytest_plugin",
        reports=("json:coverage.json", "xml:coverage.xml"),
        cov_config=config,
        extra_args=(),
        tests=project.tests,
        phases=("serial",),
        outcome_path=destination / "outcomes.jsonl",
        coverage_context="test",
    )
    argv = _independent_coverage_command(
        "vfy-independent", commands[0][1], destination, preflight=True
    )
    argv.insert(6, "--all")
    environment = {
        "COVERAGE_FILE": str(destination / ".coverage"),
        "VFY_BENCHMARK_OUTCOMES": str(destination / "outcomes.jsonl"),
    }
    if role != "vfy-independent":
        argv = [arg for arg in argv if not arg.startswith("--cov")]
        argv.extend(["--no-cov"])
    if role == "coverage.py-independent":
        argv.extend(["-p", NATIVE_REFERENCE_PLUGIN])
        environment.update(
            {
                "VFY_ORACLE_ENABLED": "1",
                "VFY_ORACLE_DATA": str(destination / ".coverage"),
                "VFY_ORACLE_EVIDENCE": str(destination / "oracle.json"),
                "VFY_ORACLE_SOURCE_JSON": json.dumps(sources),
                "VFY_ORACLE_JSON": str(destination / "coverage.json"),
                "VFY_ORACLE_XML": str(destination / "coverage.xml"),
                "VFY_ORACLE_BRANCH": "config",
            }
        )
        if config is not None:
            environment["VFY_ORACLE_CONFIG"] = str(config)
    return argv, environment


def run_attempt(  # noqa: C901 - retain each attempt as one evidence transaction
    role: str,
    project: ProjectManifest,
    python: Path,
    root: Path,
    destination: Path,
    environment: dict[str, str],
    baseline: dict[str, Any] | None,
    records: list[dict[str, Any]] | None = None,
    *,
    cov_config: Path | None = None,
    observer_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    destination.mkdir(parents=True, exist_ok=False)
    observer_path = stage_plugins(observer_path or destination / "observer-path")
    argv, overrides = command_for(role, project, python, root, destination, cov_config=cov_config)
    child_env = {**environment, **overrides}
    child_env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(observer_path), environment.get("PYTHONPATH")))
    )
    measurement_keys = {
        "COVERAGE_FILE",
        "VFY_BENCHMARK_OUTCOMES",
        "PYTHONPATH",
        "PYTHONHASHSEED",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "PYTEST_PLUGINS",
        "VIRTUAL_ENV",
        "PATH",
        *(key for key, _ in project.environment),
        *(key for key in overrides if key.startswith("VFY_ORACLE_")),
    }
    receipt: dict[str, Any] = {
        "role": role,
        "command": argv,
        "cwd": str(root),
        "status": "running",
        "observer_plugins": json.loads((observer_path / PLUGIN_RECEIPT).read_text()),
        "environment": {
            key: child_env[key] for key in sorted(measurement_keys) if key in child_env
        },
    }
    if records is not None:
        records.append(receipt)
    (destination / "command.json").write_text(json.dumps(receipt, indent=2) + "\n")
    try:
        elapsed, completed = timed_process(argv, cwd=root, env=child_env, timeout=1800)
        (destination / "stdout.log").write_text(completed.stdout or "")
        (destination / "stderr.log").write_text(completed.stderr or "")
        receipt.update(milliseconds=elapsed, exitstatus=completed.returncode)
        if completed.returncode != 0:
            raise RuntimeError(f"{role}: command exited {completed.returncode}")
        execution = independent_execution(
            destination, root, python, destination / "tmp", Route("native", 1, False)
        )
        outcomes = outcome_records(destination / "outcomes.jsonl")
        if any(outcome == "failed" for _nodeid, _phase, outcome, _xfail in outcomes):
            raise RuntimeError(f"{role}: phase evidence contains failed outcomes")
        cases = execution["cases"]
        actual_ids = {case["nodeid"] for case in cases}
        outcome_ids = {nodeid.split("::[subtest]", 1)[0] for nodeid, *_ in outcomes}
        if len(cases) != len(actual_ids) or actual_ids != outcome_ids:
            raise RuntimeError(
                f"{role}: independent execution inventory differs from phase outcomes"
            )
        junit = junit_records(destination / "junit.xml", root, destination / "tmp")
        if not junit:
            raise RuntimeError(f"{role}: JUnit evidence is empty")
        observed: dict[str, Any] = {"outcomes": outcomes, "junit": junit}
        if role == "coverage.py-independent":
            oracle = json.loads((destination / "oracle.json").read_text())
            if oracle.get("status") != "finished" or oracle.get("tracer") != "CTracer":
                raise RuntimeError("Coverage.py reference did not activate CTracer and finish")
            receipt["oracle"] = oracle
        if role != "plain-independent":
            observed["coverage"] = normalized_data(destination / ".coverage", root)
            measured = observed["coverage"]
            if not any("|" in context for context in measured["contexts"]):
                raise RuntimeError(f"{role}: per-test contexts are missing")
            if not any(
                path.startswith(project.package_path) and (values["lines"] or values["arcs"])
                for path, values in measured["files"].items()
            ):
                raise RuntimeError(f"{role}: measured package coverage is empty")
            report = destination / "coverage.json"
            if baseline is None:
                observed["report"] = str(report)
            else:
                if observed["coverage"] != baseline["coverage"]:
                    difference = first_schema7_difference(
                        baseline["coverage"], observed["coverage"]
                    )
                    raise RuntimeError(f"{role}: schema-7 mismatch: {difference}")
                _validate_coverage_reports(role, root, destination, Path(baseline["report"]))
        if baseline is not None:
            if outcomes != baseline["outcomes"]:
                raise RuntimeError(f"{role}: phase outcomes differ")
            if junit != baseline["junit"]:
                raise RuntimeError(f"{role}: JUnit testcase multiset differs")
        evidence_files = ["independent.json", "outcomes.jsonl", "junit.xml"]
        if role != "plain-independent":
            evidence_files.extend((".coverage", "coverage.json", "coverage.xml"))
            receipt["measured_files"] = len(observed["coverage"]["files"])
            receipt["contexts"] = len(observed["coverage"]["contexts"])
        if role == "coverage.py-independent":
            evidence_files.append("oracle.json")
        receipt["evidence_sha256"] = {
            name: hashlib.sha256((destination / name).read_bytes()).hexdigest()
            for name in evidence_files
        }
        receipt.update(
            status="passed", engine="vfy-independent-v1", cases=len(cases), junit_cases=len(junit)
        )
        return receipt, observed
    except BaseException as error:
        if isinstance(error, subprocess.TimeoutExpired):
            for name, stream in (("stdout", error.stdout), ("stderr", error.stderr)):
                value = (
                    stream.decode(errors="replace") if isinstance(stream, bytes) else stream or ""
                )
                (destination / f"{name}.log").write_text(value)
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        (destination / "validation.json").write_text(json.dumps(receipt, indent=2) + "\n")


def main() -> int:  # noqa: C901 - the controller owns parity and artifact guards
    args = parse_args()
    if args.warmups < 0 or args.runs < 1 or (not args.validate_only and args.output is None):
        raise SystemExit("invalid sampling or missing output")
    if (
        not args.validate_only
        and args.mode == "publication"
        and (args.warmups < 3 or args.runs < 15)
    ):
        raise SystemExit("publication requires at least 3 warmups and 15 runs")
    timed_roles = tuple(args.timing_route or ROLES)
    if len(set(timed_roles)) != len(timed_roles):
        raise SystemExit("timing routes must not repeat")
    root = args.root.resolve()
    python = args.python.absolute()
    project = PROJECTS[args.project]
    if args.rich_clock and project.key != "rich":
        raise SystemExit("--rich-clock is only available for Rich")
    if args.rich_clock:
        project = replace(project, plugins=(*project.plugins, RICH_CLOCK_PLUGIN))
    if project.key == "datasette":
        project = replace(project, serial_args=())
    repository = Path(__file__).resolve().parents[1]
    controller = require_clean(repository)
    fixture_path = repository / "benchmarks/rich_clock_plugin.py"
    fixture_sha256 = (
        hashlib.sha256(fixture_path.read_bytes()).hexdigest() if args.rich_clock else None
    )
    source = require_clean(root)
    if source.revision != project.revision:
        raise RuntimeError("prepared project revision does not match pinned manifest")
    metadata = preparation_metadata(
        python, root, manifest_path=args.preparation_manifest, build_manifest=args.build_manifest
    )
    lock = repository / project.dependency_lock
    if (
        metadata.get("project") != project.key
        or metadata.get("checked_out_revision") != project.revision
        or metadata.get("revision") != project.revision
        or metadata.get("python") != str(python)
        or metadata.get("dependency_lock") != project.dependency_lock
        or metadata.get("dependency_lock_sha256") != hashlib.sha256(lock.read_bytes()).hexdigest()
    ):
        raise RuntimeError("prepared project or dependency lock does not match manifest")
    artifacts = args.artifacts.resolve()
    if artifacts.is_relative_to(root) or artifacts.is_relative_to(repository):
        raise ValueError("benchmark artifacts must be outside project and controller checkouts")
    if args.output is not None:
        output = args.output.resolve()
        if output.is_relative_to(root) or output.is_relative_to(repository):
            raise ValueError("benchmark output must be outside project and controller checkouts")
    if artifacts.exists() or (args.output is not None and args.output.exists()):
        raise FileExistsError("artifact or output path already exists")
    artifacts.mkdir(parents=True)
    observer_path = stage_plugins(artifacts / "observer-path")
    observer_record = json.loads((observer_path / PLUGIN_RECEIPT).read_text())
    environment = benchmark_environment(
        {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONHASHSEED": "0"}
    )
    environment.update(project.environment)
    if project.plugins:
        environment["PYTEST_PLUGINS"] = ",".join(p for p in project.plugins if p != "xdist.plugin")
    environment["PATH"] = str(python.parent) + os.pathsep + environment.get("PATH", "")
    environment["VIRTUAL_ENV"] = str(python.parent.parent)
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    coverage_overlay: dict[str, JSONValue] | None = None
    cov_config: Path | None = None
    if args.coverage_subprocess:
        from benchmarks.coverage_config_overlay import (  # noqa: PLC0415 - optional tomlkit
            prepare_subprocess_config,
        )

        _sources, config = coverage_inputs(project, root)
        coverage_overlay = prepare_subprocess_config(
            python,
            root,
            artifacts / "coverage-subprocess.toml",
            config,
            environment=environment,
        )
        cov_config = Path(str(coverage_overlay["config_path"]))
    generated = GeneratedFiles(root, project.generated_files)
    installed = independent_coverage_artifact(python, root, environment, args.build_manifest)
    selected = selected_interpreter_metadata(python, root=root, environment=environment)
    wheel = metadata["vfy_wheel"]
    if not isinstance(wheel, dict) or not isinstance(wheel.get("source_revision"), str):
        raise RuntimeError("prepared wheel has no source revision")
    workload: dict[str, JSONValue] = {
        "project": project.key,
        "scope": project.scope,
        "workflow": "independent-coverage-reference",
        "phases": ["serial"],
        "rich_clock_fixture_sha256": fixture_sha256,
        "observer_plugins": observer_record,
        **({"coverage_config_overlay": coverage_overlay} if coverage_overlay is not None else {}),
    }
    methodology = {
        "mode": args.mode,
        "warmups": args.warmups,
        "measured_runs": args.runs,
        "frontend": "vfy-independent-v1",
        "workers": 1,
        "reference": "Coverage.py CTracer with --no-cov",
        "candidate": "vfy independent coverage",
        "control": "independent --no-cov; preflight always, timed only when selected",
        "timing_routes": list(timed_roles),
        "observer_plugins": observer_record,
        **({"coverage_config_overlay": coverage_overlay} if coverage_overlay is not None else {}),
        "scope": (
            "Complete CLI workflows; reference and candidate use different controller paths. "
            "Not isolated collector cost."
        ),
        "validation": (
            "Each run: independent engine inventory, phase outcomes and JUnit; coverage roles "
            "additionally require exact schema-7 contexts, lines, arcs, tracers, JSON and XML."
        ),
        "warnings": "not compared",
        "sample_artifacts": (
            "large validated warmup and sample files are gzip-compressed after each child; "
            "compression and hashing are outside the child timer; preflight remains uncompressed"
            if args.compress_samples
            else "uncompressed"
        ),
        "controlled_input": (
            {
                "plugin": RICH_CLOCK_PLUGIN,
                "source": str(fixture_path),
                "sha256": fixture_sha256,
                "method": "scoped fixed logging clock and FileProxy teardown for every route",
            }
            if args.rich_clock
            else None
        ),
        "seed": 0,
        "result_json": "independent result JSON included in all measured child commands",
        "reference_receipt": (
            "Coverage.py tracer and child-shard receipt written inside the reference child"
        ),
    }
    provenance = prepared_project_provenance(
        repository,
        workload=workload,
        workers=1,
        cache="prepared checkout; parity before timing",
        packages=("coverage",),
        selected=selected,
        prepared=metadata,
        source_revision=wheel["source_revision"],
        upstream_revision=project.revision,
        sampling={"mode": args.mode, "warmups": args.warmups, "measured_runs": args.runs},
        publication=args.mode == "publication",
        build_manifest=args.build_manifest,
        validation=methodology["validation"],
    )
    (artifacts / "artifact-before.json").write_text(json.dumps(installed, indent=2) + "\n")
    records: list[dict[str, Any]] = []
    samples: dict[str, list[float]] = {role: [] for role in timed_roles}
    gate: dict[str, Any] = {
        "status": "running",
        "runs": records,
        "controlled_input": methodology["controlled_input"],
    }
    try:
        baseline: dict[str, Any] | None = None
        for role in ROLES:
            if (
                fixture_sha256 is not None
                and hashlib.sha256(fixture_path.read_bytes()).hexdigest() != fixture_sha256
            ):
                raise RuntimeError("Rich fixture changed before preflight")
            generated.restore()
            record, observed = run_attempt(
                role,
                project,
                python,
                root,
                artifacts / "preflight" / role,
                environment,
                baseline,
                records,
                cov_config=cov_config,
                observer_path=observer_path,
            )
            if baseline is None:
                baseline = observed
        assert baseline is not None
        gate["status"] = "passed"
        baseline_files = (
            Path(baseline["report"]),
            Path(baseline["report"]).with_suffix(".xml"),
            artifacts / "preflight" / ROLES[0] / ".coverage",
            artifacts / "preflight" / ROLES[0] / "independent.json",
            artifacts / "preflight" / ROLES[1] / "independent.json",
        )
        hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in baseline_files}
        gate["proof_sha256"] = {str(path): digest for path, digest in hashes.items()}
        (artifacts / "preflight.json").write_text(json.dumps(gate, indent=2) + "\n")
        if not args.validate_only:
            for stage, count in (("warmup", args.warmups), ("sample", args.runs)):
                for index, order in enumerate(rotated_orders(timed_roles, count), start=1):
                    for role in order:
                        if (
                            fixture_sha256 is not None
                            and hashlib.sha256(fixture_path.read_bytes()).hexdigest()
                            != fixture_sha256
                        ):
                            raise RuntimeError("Rich fixture changed before sample")
                        if any(
                            hashlib.sha256(path.read_bytes()).hexdigest() != digest
                            for path, digest in hashes.items()
                        ):
                            raise RuntimeError("preflight evidence changed before sample")
                        generated.restore()
                        record, _ = run_attempt(
                            role,
                            project,
                            python,
                            root,
                            artifacts / stage / str(index) / role,
                            environment,
                            baseline,
                            records,
                            cov_config=cov_config,
                            observer_path=observer_path,
                        )
                        if args.compress_samples:
                            record["retained_artifacts"] = archive_run(
                                artifacts / stage / str(index) / role
                            )
                        if stage == "sample":
                            samples[role].append(record["milliseconds"])
                    print(f"{project.key}: {stage} {index}/{count}", file=sys.stderr, flush=True)
    except BaseException as error:
        gate.update(status="failed", error=str(error))
        raise
    finally:
        generated.restore()
        after = independent_coverage_artifact(python, root, environment, args.build_manifest)
        (artifacts / "artifact-after.json").write_text(json.dumps(after, indent=2) + "\n")
        if after != installed:
            gate.update(status="failed", guard_error="installed artifact changed")
        if (
            fixture_sha256 is not None
            and hashlib.sha256(fixture_path.read_bytes()).hexdigest() != fixture_sha256
        ):
            gate.update(status="failed", guard_error="Rich fixture changed")
            (artifacts / "validation.json").write_text(json.dumps(gate, indent=2) + "\n")
            raise RuntimeError("Rich fixture changed during benchmark")
        require_revision(repository, controller.revision)
        require_clean(repository)
        require_revision(root, source.revision)
        require_clean(root)
        (artifacts / "validation.json").write_text(json.dumps(gate, indent=2) + "\n")
        if after != installed:
            raise RuntimeError("installed artifact changed")
    if args.validate_only:
        print(json.dumps({"status": "passed", "artifacts": str(artifacts)}))
        return 0
    assert args.output is not None
    result = {
        "benchmark": "native-coverage",
        "status": "passed",
        "mode": args.mode,
        "parity": "exact",
        "workload": workload,
        "provenance": provenance,
        "controller_revision": controller.revision,
        "runtime_revision": wheel["source_revision"],
        "project": project.key,
        "project_revision": project.revision,
        "scope": project.scope,
        "workers": 1,
        "warmups": args.warmups,
        "runs": args.runs,
        "methodology": methodology,
        "preflight": str(artifacts / "preflight.json"),
        "artifacts": str(artifacts),
        "attempts": records,
        **{role: summarize(values) for role, values in samples.items()},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
