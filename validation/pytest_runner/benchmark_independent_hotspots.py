"""Compare retained release wheels with explicit history and JUnit boundaries.

Pass an artifacts JSON mapping baseline/candidate to python, wheel,
build_manifest and history (a project-relative path). The prepared snapshot is
read-only; each command restores it into --root outside the timer. Packaging
uses its pinned complete suite. --tests can select a named small file instead.

The 20-observation seed records one real validated run twenty times through the
installed recorder. It does not represent twenty separate test executions.
Use --history-seed-evidence with a completed fresh full Packaging cohort to
retain the whole suite's history while timing a small file.
Stock commands without JUnit check terminal counts/status after full semantic
preflight; vfy additionally checks every persisted case and the history update.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from benchmarks.benchmark_harness import (  # noqa: E402
    BenchmarkMode,
    add_sampling_arguments,
    benchmark_environment,
    paired_uncertainty,
    rotated_orders,
    sampling_config,
    summarize,
)
from benchmarks.benchmark_provenance import hardware_details, require_clean  # noqa: E402
from scripts.process_tree import run_captured  # noqa: E402
from scripts.pytest_execution_record import file_sha256, write_atomic  # noqa: E402
from validation.pytest_runner import benchmark_native_xdist as bench  # noqa: E402
from validation.pytest_runner.benchmark_real_candidates import (  # noqa: E402
    case_outcomes,
    normalized_history,
    runtime_identity,
)


def read_history(path: Path) -> dict[str, Any]:
    """Read stored JSON/SQLite independently of the runtime under comparison."""
    if path.suffix != ".sqlite3":
        return json.loads(path.read_text(encoding="utf-8"))
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        tests = {}
        for identity, nodeid, duration, present, negative_zero in connection.execute(
            "SELECT id,nodeid,duration_ms,observations_present,duration_negative_zero "
            "FROM tests ORDER BY nodeid"
        ):
            entry: dict[str, Any] = {}
            if duration is not None:
                entry["duration_ms"] = -0.0 if negative_zero else duration
            if present:
                observations = []
                for (
                    run_id,
                    attempt,
                    outcome,
                    phase,
                    elapsed,
                    observation_negative_zero,
                ) in connection.execute(
                    "SELECT r.run_id,o.attempt,o.outcome,o.failed_phase,"
                    "o.duration_ms,o.duration_negative_zero "
                    "FROM observations o LEFT JOIN runs r ON r.id=o.run_id "
                    "WHERE o.test_id=? ORDER BY o.position",
                    (identity,),
                ):
                    observation = {
                        "run_id": run_id,
                        "attempt": ("primary", "confirmation")[attempt],
                        "outcome": ("passed", "failed", "skipped")[outcome],
                        "duration_ms": -0.0 if observation_negative_zero else elapsed,
                    }
                    if phase is not None:
                        observation["failed_phase"] = ("setup", "call", "teardown")[phase]
                    observations.append(observation)
                entry["observations"] = observations
            tests[nodeid] = entry
        return {"tests": tests}


def check_history(
    actual: dict[str, Any], seed: dict[str, Any] | None, cases: list[dict[str, Any]]
) -> dict[str, Any]:
    """Require every case and exactly one new primary observation from one run."""
    outcomes = case_outcomes(cases)
    called = {
        case["nodeid"]
        for case in cases
        if any(report["phase"] == "call" for report in case["reports"])
    }
    if seed is None:
        if set(actual.get("tests", {})) != set(outcomes):
            raise ValueError("fresh history inventory differs from preflight")
        seed = {
            **actual,
            "tests": {nodeid: {"observations": []} for nodeid in outcomes},
        }
    actual_tests, seed_tests = actual["tests"], seed["tests"]
    if set(actual_tests) != set(seed_tests) or any(
        actual_tests[nodeid] != entry
        for nodeid, entry in seed_tests.items()
        if nodeid not in outcomes
    ):
        raise ValueError("untouched history entries changed or disappeared")
    if {key: value for key, value in actual.items() if key != "tests"} != {
        key: value for key, value in seed.items() if key != "tests"
    }:
        raise ValueError("history metadata changed")
    selected_actual = {"tests": {nodeid: actual_tests[nodeid] for nodeid in outcomes}}
    selected_seed = {"tests": {nodeid: seed_tests[nodeid] for nodeid in outcomes}}
    normalized = normalized_history(selected_actual, selected_seed, outcomes, called)
    return {**seed, "tests": {**seed_tests, **normalized["tests"]}}


def stream_sha256(path: Path) -> str:
    """Hash large history artifacts without retaining another byte buffer."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def retained_seed_run(evidence: Path, snapshot: Path, baseline: dict[str, Any]) -> Path:
    """Require completed full Packaging parity before reusing its observed run."""
    evidence = evidence.resolve()
    result_path, identity_path = evidence / "result.json", evidence / "identity.json"
    if not result_path.is_file() or not identity_path.is_file():
        raise ValueError("history seed requires completed full-suite evidence")
    result, identity = load_json(result_path), load_json(identity_path)
    arguments = identity["arguments"]
    if (
        result.get("status") != "passed"
        or result.get("initial_observations_per_case") != 0
        or arguments.get("project") != "packaging"
        or arguments.get("cases") != 62_435
        or arguments.get("tests")
        or result.get("identity_sha256") != stream_sha256(identity_path)
        or identity.get("snapshot") != bench.serial.snapshot_inputs(snapshot)
    ):
        raise ValueError("history seed evidence does not bind a completed full Packaging cohort")
    source = evidence / "source-run.json"
    if result.get("source_run_sha256") != stream_sha256(source):
        raise ValueError("history seed source-run hash differs")
    previous = identity["build_manifests"]["baseline"]["contents"]
    current = load_json(Path(baseline["build_manifest"]))
    if previous["wheel_sha256"] != current["wheel_sha256"]:
        raise ValueError("history seed baseline artifact differs")
    parity_path = evidence / "baseline/discovery/parity.json"
    if result.get("preflight_sha256", {}).get("baseline") != stream_sha256(parity_path):
        raise ValueError("history seed preflight hash differs")
    proof = load_json(parity_path)["independent-j1"]
    execution = proof["independent_execution"]
    execution_path = Path(execution["path"])
    if (
        proof.get("passed") is not True
        or proof.get("cases") != 62_435
        or execution.get("engine") != "vfy-independent-v1"
        or stream_sha256(execution_path) != execution["retained_sha256"]
    ):
        raise ValueError("history seed full phase proof differs")
    outcomes = case_outcomes(load_json(execution_path)["cases"])
    run = load_json(source)
    observed = {test["nodeid"]: test["outcome"] for test in run["tests"]}
    if len(run["tests"]) != 62_435 or len(outcomes) != 62_435 or observed != outcomes:
        raise ValueError("history seed source outcomes differ from full phase preflight")
    return source


def terminal_counts(stdout: str) -> dict[str, int]:
    """Read only the final pytest summary, retaining the complete raw transcript."""
    lines = [line.strip("= ") for line in stdout.splitlines() if line.strip()]
    if not lines or re.search(r"\bin \d+(?:\.\d+)?s(?: \(.*\))?$", lines[-1]) is None:
        raise ValueError("missing final terminal outcome summary")
    return {
        outcome: int(count)
        for count, outcome in re.findall(
            r"\b(\d+) (passed|failed|skipped|xfailed|xpassed|errors?|warnings?|deselected)\b",
            lines[-1],
        )
    }


def load_json(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open() as stream:
        return json.load(stream)


def seed_history(python: Path, history: Path, run: Path, output: Path) -> dict[str, Any]:
    """Record twenty distinct run identities outside command measurements."""
    history.parent.mkdir(parents=True, exist_ok=True)
    if history.exists():
        raise ValueError("history seed destination already exists")
    program = (
        "import sys\nfrom vfy.native import record_test_execution\n"
        "for index in range(20):\n"
        "    record_test_execution(sys.argv[1],sys.argv[2],f'{index:032x}','primary')\n"
    )
    completed = run_captured(
        [str(python), "-I", "-B", "-c", program, str(history), str(run)],
        cwd=history.parent,
        env=benchmark_environment(),
        timeout=600,
    )
    write_atomic(
        output,
        {
            "exit_status": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "source_run_sha256": file_sha256(run),
            "recorded_copies": 20,
            "actual_test_executions": 1,
        },
    )
    if completed.returncode:
        raise ValueError(f"history recorder failed; see {output}")
    value = read_history(history)
    expected_ids = [f"{index:032x}" for index in range(20)]
    if not value.get("tests") or any(
        [observation.get("run_id") for observation in entry.get("observations", [])] != expected_ids
        for entry in value["tests"].values()
    ):
        raise ValueError("history seed does not have twenty observations per case")
    return value


def unrelated_site_files(python: Path) -> dict[str, str]:
    """Bind every non-vfy installed site file while allowing bytecode writes."""
    purelib = Path(bench.interpreter_metadata(python, "vfy")["purelib"])
    result: dict[str, str] = {}
    for path in sorted(purelib.rglob("*")):
        if not path.is_file() or path.suffix == ".pyc" or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(purelib)
        top = relative.parts[0]
        if top == "vfy" or (top.startswith("vfy-") and top.endswith(".dist-info")):
            continue
        result[relative.as_posix()] = file_sha256(path)
    return result


def verify_unrelated_sites(runtimes: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Require identical real installed files for both artifact roles."""
    files = {
        role: unrelated_site_files(Path(runtime["python"])) for role, runtime in runtimes.items()
    }
    if not files["baseline"] or files["baseline"] != files["candidate"]:
        raise ValueError("non-vfy installed site files differ between benchmark roles")
    return files


def discover(
    args: argparse.Namespace,
    project: bench.matrix.Project,
    runtimes: dict[str, Any],
    root: Path,
    snapshot: Path,
    output: Path,
) -> dict[str, Any]:
    """Finish full fresh/populated semantic gates before admitting any samples."""
    workers = getattr(args, "workers", 1)
    serial_stock = bench.Route("stock", 0)
    stock = bench.Route("stock", 0 if workers == 1 else workers)
    independent = bench.Route("native", workers, False)
    routes = [stock, independent] if workers == 1 else [serial_stock, stock, independent]
    seeds: dict[str, Path] = {}
    cases: dict[str, list[dict[str, Any]]] = {}
    junit_expected: dict[str, Any] = {}
    terminal_expected: dict[str, dict[str, dict[str, int]]] = {}
    canonical_seed = None
    baseline_execution: tuple[Path, str] | None = None
    source_run = output / "source-run.json"
    seed_source = (
        retained_seed_run(args.history_seed_evidence, snapshot, runtimes["baseline"])
        if args.history_seed_evidence is not None
        else source_run
    )
    for role, runtime in runtimes.items():
        python, history = Path(runtime["python"]), root / runtime["history"]
        allowed, expected, proof = bench.preflight(
            project,
            root,
            snapshot,
            python,
            output / role / "discovery",
            None,
            routes,
            Path(runtime["wheel"]),
            args.timeout,
            native_scheduling=getattr(args, "native_scheduling", None),
            require_active_workers=workers > 1,
            capture=args.capture,
            junit=args.junit,
            history_path=history,
        )
        if allowed != [stock, independent]:
            raise ValueError(f"{role} failed full semantic preflight")
        execution_path = Path(proof[independent.name]["independent_execution"]["path"])
        if role == "baseline":
            baseline_execution = (execution_path, stream_sha256(execution_path))
        role_cases = load_json(execution_path)["cases"]
        if len(role_cases) != args.cases or len(case_outcomes(role_cases)) != args.cases:
            raise ValueError("preflight did not execute the requested complete case inventory")
        if role == "candidate":
            if baseline_execution is None:
                raise ValueError("baseline preflight must precede candidate")
            baseline_path, baseline_sha256 = baseline_execution
            if stream_sha256(baseline_path) != baseline_sha256:
                raise ValueError("baseline preflight execution artifact changed")
            if role_cases != load_json(baseline_path)["cases"]:
                raise ValueError("baseline and candidate phase observations differ")
        terminal_expected[role] = {}
        for route in (stock, independent):
            stdout = output / role / "discovery" / route.name / "stdout.txt"
            text = (
                stdout.read_text()
                if stdout.exists()
                else gzip.decompress(stdout.with_suffix(".txt.gz").read_bytes()).decode()
            )
            terminal_expected[role][route.name] = terminal_counts(text)
        observed = read_history(history)
        check_history(observed, None, role_cases)
        if role == "baseline":
            write_atomic(
                source_run,
                {
                    "infrastructure_failed": False,
                    "tests": [
                        {
                            "nodeid": nodeid,
                            "outcome": entry["observations"][-1]["outcome"],
                            "duration_ms": entry["observations"][-1]["duration_ms"],
                            **(
                                {"call_duration_ms": entry["duration_ms"]}
                                if "duration_ms" in entry
                                else {}
                            ),
                        }
                        for nodeid, entry in observed["tests"].items()
                    ],
                },
            )
        seed = None
        if args.history_observations:
            seed_path = output / role / "seed" / history.name
            seed = seed_history(python, seed_path, seed_source, output / role / "seed-receipt.json")
            if canonical_seed is not None and seed != canonical_seed:
                raise ValueError("JSON/SQLite seeds differ semantically")
            canonical_seed = seed
            seeds[role] = seed_path
            allowed, expected, proof = bench.preflight(
                project,
                root,
                snapshot,
                python,
                output / role / "seeded-preflight",
                None,
                routes,
                Path(runtime["wheel"]),
                args.timeout,
                native_scheduling=getattr(args, "native_scheduling", None),
                require_active_workers=workers > 1,
                capture=args.capture,
                junit=args.junit,
                history_path=history,
                history_seed=seed_path,
            )
            if allowed != [stock, independent]:
                raise ValueError(f"{role} populated-history preflight failed")
            check_history(read_history(history), seed, role_cases)
        junit_expected[role] = expected
        # Full documents remain available until every role-specific gate completes.
        # Timed history validation needs identity and every phase/outcome pair.
        cases[role] = [
            {
                "nodeid": case["nodeid"],
                "reports": [
                    {"phase": report["phase"], "outcome": report["outcome"]}
                    for report in case["reports"]
                ],
            }
            for case in role_cases
        ]
        del role_cases
    return {
        "seeds": seeds,
        "cases": cases,
        "junit": junit_expected,
        "terminal": terminal_expected,
        "canonical_seed": canonical_seed,
        "seed_source": seed_source,
    }


def measure_one(
    args: argparse.Namespace,
    project: bench.matrix.Project,
    runtime: dict[str, Any],
    root: Path,
    snapshot: Path,
    destination: Path,
    role: str,
    label: str,
    route: bench.Route,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Restore initial state outside timing and validate ordinary public outputs."""
    python, history = Path(runtime["python"]), root / runtime["history"]
    seed_path = state["seeds"].get(role)
    bench.restore_project_history(root, snapshot, history, seed_path)
    if seed_path is not None and read_history(history) != state["canonical_seed"]:
        raise ValueError("restored history seed differs")
    env = bench.route_environment(
        project, root, python, route, args.native_scheduling, output=destination
    )
    command = bench.command(
        project,
        root,
        python,
        destination,
        None,
        route,
        capture=args.capture,
        timed_evidence="junit",
        junit=args.junit,
        history_path=history,
    )
    run = bench.matrix.invoke(command, root, env, destination, args.timeout)
    if (
        run["exit_status"] != 0
        or terminal_counts(run["stdout"]) != state["terminal"][role][route.name]
    ):
        raise ValueError(f"timed status or terminal counts differ: {destination}")
    if (
        args.junit
        and bench.adoption.junit_records(
            destination / "junit.xml", root, root.parent / "pytest-tmp"
        )
        != state["junit"][role][route.name]
    ):
        raise ValueError(f"timed JUnit cases differ: {destination}")
    if label != "stock":
        check_history(read_history(history), state["canonical_seed"], state["cases"][role])
        shutil.copyfile(history, destination / history.name)
    return run


def measure(
    args: argparse.Namespace,
    project: bench.matrix.Project,
    runtimes: dict[str, Any],
    root: Path,
    snapshot: Path,
    output: Path,
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, list[float]]]:
    """Rotate retained release artifacts and stock within shared rounds."""
    sampling = sampling_config(args)
    records: list[dict[str, Any]] = []
    values: dict[str, list[float]] = {name: [] for name in ("stock", "baseline", "candidate")}
    for phase, count in (("warmup", sampling.warmups), ("sample", sampling.runs)):
        for index, order in enumerate(rotated_orders(list(values), count)):
            for label in order:
                role = "candidate" if label == "stock" else label
                runtime = runtimes[role]
                destination = output / f"{phase}-{index:02d}-{label}"
                route = (
                    bench.Route("stock", 0 if args.workers == 1 else args.workers)
                    if label == "stock"
                    else bench.Route("native", args.workers, False)
                )
                run = measure_one(
                    args, project, runtime, root, snapshot, destination, role, label, route, state
                )
                row = {
                    "phase": phase,
                    "index": index,
                    "role": label,
                    "seconds": run["seconds"],
                    "artifacts": bench.serial.archive_run(destination),
                }
                records.append(row)
                if phase == "sample":
                    values[label].append(run["seconds"] * 1000)
                write_atomic(output / "samples.json", {"samples": records})
                sys.stdout.write(f"{phase} {index + 1}/{count} {label}: {run['seconds']:.6f}s\n")
                sys.stdout.flush()
    return records, values


def validate_worker_options(workers: int, *, junit: bool) -> None:
    """Reject distributed timings unless all timed routes retain case evidence."""
    if workers < 1:
        raise ValueError("workers must be positive")
    if workers > 1 and not junit:
        raise ValueError("distributed comparisons require JUnit on all timed routes")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--project", choices=(*bench.matrix.PROJECTS_BY_KEY, "generated"), default="packaging"
    )
    parser.add_argument("--tests", nargs="+")
    parser.add_argument("--cases", type=int, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--native-scheduling", choices=("file", "scope", "cases", "striped"))
    parser.add_argument("--history-observations", type=int, choices=(0, 20), default=0)
    parser.add_argument(
        "--history-seed-evidence",
        type=Path,
        help="completed fresh full Packaging cohort supplying all 62,435 cases to subset history",
    )
    parser.add_argument("--junit", action="store_true")
    parser.add_argument("--capture", choices=("sys", "fd", "no"), default="sys")
    parser.add_argument("--timeout", type=float, default=600)
    add_sampling_arguments(parser, warmups=0, runs=3, mode=BenchmarkMode.DIAGNOSTIC)
    args = parser.parse_args()
    sampling = sampling_config(args)
    root, snapshot, output = args.root.absolute(), args.snapshot.resolve(), args.output.absolute()
    paths = (root, snapshot, output)
    if any(left.is_relative_to(right) for left in paths for right in paths if left != right):
        raise ValueError("runtime root, snapshot and output must be separate")
    if len(set(paths)) != 3 or root.exists():
        raise ValueError("require a fresh runtime root and separate immutable snapshot/output")
    validate_worker_options(args.workers, junit=args.junit)
    if args.cases < 1 or (snapshot / ".vfy").exists():
        raise ValueError("require a positive case count and a snapshot without runtime history")
    if args.history_seed_evidence is not None and (
        args.history_observations != 20 or args.project != "packaging"
    ):
        raise ValueError("retained seed evidence requires Packaging and twenty observations")
    project = (
        bench.matrix.Project("generated", "", "", ".", ("test_workload.py",), ())
        if args.project == "generated"
        else bench.matrix.PROJECTS_BY_KEY[args.project]
    )
    if args.tests:
        project = replace(project, tests=tuple(args.tests))
    if project.key == "packaging":
        if bench.matrix.git(snapshot, "rev-parse", "HEAD") != project.revision:
            raise ValueError("Packaging snapshot is not at its pinned revision")
        if not args.tests and args.cases != 62_435:
            raise ValueError("complete Packaging requires all 62,435 cases")
    source = require_clean(ROOT)
    output.mkdir(parents=True, exist_ok=False)
    runtimes = json.loads(args.artifacts.read_text())
    if set(runtimes) != {"baseline", "candidate"}:
        raise ValueError("artifacts must identify baseline and candidate runtimes")
    runtimes = {role: runtimes[role] for role in ("baseline", "candidate")}
    for runtime in runtimes.values():
        relative = Path(runtime["history"])
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != (".vfy",):
            raise ValueError("runtime history must be a relative path inside .vfy")
    bench.serial.restore_project(root, snapshot)
    identities = runtime_identity(project, root, runtimes)
    unrelated_sites = verify_unrelated_sites(runtimes)
    snapshot_inputs = bench.serial.snapshot_inputs(snapshot)
    write_atomic(
        output / "identity.json",
        {
            "source": source.as_dict(),
            "runtimes": identities,
            "unrelated_site_files": unrelated_sites,
            "build_manifests": {
                role: {
                    "path": runtime["build_manifest"],
                    "sha256": file_sha256(Path(runtime["build_manifest"])),
                    "contents": load_json(Path(runtime["build_manifest"])),
                }
                for role, runtime in runtimes.items()
            },
            "hardware": hardware_details(),
            "snapshot": snapshot_inputs,
            "harness_sha256": {
                **bench.SOURCE_INPUTS,
                str(Path(__file__).relative_to(ROOT)): file_sha256(Path(__file__)),
            },
            "arguments": vars(args)
            | {key: str(value) for key, value in vars(args).items() if isinstance(value, Path)},
        },
    )
    state = discover(args, project, runtimes, root, snapshot, output)
    records, values = measure(args, project, runtimes, root, snapshot, output, state)
    if (
        bench.serial.snapshot_inputs(snapshot) != snapshot_inputs
        or runtime_identity(project, root, runtimes) != identities
        or verify_unrelated_sites(runtimes) != unrelated_sites
    ):
        raise ValueError("snapshot or installed artifacts changed")
    if require_clean(ROOT) != source:
        raise ValueError("controller source changed")
    write_atomic(
        output / "result.json",
        {
            "status": "passed",
            "sampling": sampling.as_dict(),
            "junit": args.junit,
            "workers": args.workers,
            "initial_observations_per_case": args.history_observations,
            "seed_actual_test_executions": 1,
            "seed_recorded_copies": args.history_observations,
            "source_run_sha256": stream_sha256(output / "source-run.json"),
            "identity_sha256": stream_sha256(output / "identity.json"),
            "preflight_sha256": {
                role: stream_sha256(output / role / "discovery/parity.json") for role in runtimes
            },
            "seed_source": {
                "path": str(state["seed_source"]),
                "sha256": stream_sha256(state["seed_source"]),
            },
            "seeds": {
                role: {
                    "path": str(path),
                    "sha256": stream_sha256(path),
                    "bytes": path.stat().st_size,
                }
                for role, path in state["seeds"].items()
            },
            "timer": "complete public command; snapshot restoration and validation outside timer",
            "timed_validation": (
                "status/terminal counts on every route; full vfy history; JUnit if requested"
            ),
            "preflight": (
                "complete ordered inventory, phases, warnings, independent engine; "
                "stock observer outside timing"
            ),
            "limitations": (
                "without JUnit, stock per-case outcomes and all timed phase/warning "
                "records are not rechecked"
            ),
            "summary": {name: summarize(samples) for name, samples in values.items()},
            "comparisons": {
                name: paired_uncertainty(values["candidate"], values[name])
                for name in ("baseline", "stock")
            },
            "samples": records,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
