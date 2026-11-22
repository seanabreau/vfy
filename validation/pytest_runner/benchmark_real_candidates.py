"""Compare retained release wheels on pinned real suites and actual test histories.

Preparation and measurement are separate invocations. History seeds come from
one measured full baseline suite, never fabricated or replicated observations.
Every child retains raw evidence and must satisfy the initial parity proof.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import shutil
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmarks.benchmark_projects import normalized_data
from benchmarks.benchmark_provenance import hardware_details, require_clean
from scripts.process_tree import run_captured
from scripts.pytest_execution_record import write_atomic
from validation.pytest_runner.benchmark_vfy_coverage import (
    interpreter_details,
    load_coverage_report,
    report_digest,
    verify_build_identity,
    verify_installed_distribution_files,
)
from validation.pytest_runner.check_native_adoption_matrix import (
    PROJECTS_BY_KEY,
    Project,
    checkout,
    expanded_tests,
    git,
    junit_outcomes,
    normalize_native,
    normalized_coverage_json,
    plugin_args,
    prepare_project,
    prepare_python,
    project_environment,
    validate_cases,
)
from validation.pytest_runner.check_native_project import coverage_reference


# External JSON records have the same dynamic protocol as the adoption checker.
def ordered_roles(runtimes: dict[str, Any]) -> tuple[str, ...]:
    """Validate role names and put the fixed baseline first in every comparison."""
    if not isinstance(runtimes, dict) or "baseline" not in runtimes:
        raise ValueError("candidate roles require a baseline")
    if any(re.fullmatch(r"[a-z][a-z0-9_-]*", role) is None for role in runtimes):
        raise ValueError("candidate role names must be lowercase path components")
    return ("baseline", *(role for role in runtimes if role != "baseline"))


def require_controller_python(installed: dict[str, Any]) -> None:
    """Use the target interpreter's source semantics for the independent oracle."""
    version = installed.get("python")
    match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", version) if isinstance(version, str) else None
    if match is None:
        raise ValueError("target Python metadata must include a major/minor version")
    target = (int(match[1]), int(match[2]))
    controller = sys.version_info[:2]
    if target != controller:
        raise ValueError(
            "Coverage.py source analysis requires controller Python "
            f"{target[0]}.{target[1]}; found {controller[0]}.{controller[1]}"
        )


def normalized_history(
    actual: dict[str, Any],
    seed: dict[str, Any],
    outcomes: dict[str, str],
    called: set[str] | None = None,
) -> dict[str, Any]:
    """Allow only selected primary observations and their two new durations."""
    tests, previous = actual.get("tests"), seed.get("tests")
    if not isinstance(tests, dict) or not isinstance(previous, dict):
        raise ValueError("history tests must be mappings")
    expected = copy.deepcopy(seed)
    normalized = copy.deepcopy(actual)
    run_ids = set()
    called = set(outcomes) if called is None else called
    for nodeid, outcome in outcomes.items():
        value = tests.get(nodeid)
        old = previous.get(nodeid)
        if not isinstance(value, dict) or not isinstance(old, dict):
            raise ValueError(f"history entry missing: {nodeid}")
        observations = value.get("observations")
        prior = old.get("observations")
        if not isinstance(observations, list) or not observations or not isinstance(prior, list):
            raise ValueError("history observations must be lists")
        latest = observations[-1]
        if not isinstance(latest, dict) or not isinstance(latest.get("run_id"), str):
            raise ValueError("missing new history run identity")
        if re.fullmatch(r"[0-9a-f]{32}", latest["run_id"]) is None:
            raise ValueError("invalid new history run identity")
        run_ids.add(latest["run_id"])
        durations = [latest.get("duration_ms")]
        if nodeid in called:
            durations.append(value.get("duration_ms"))
        for duration in durations:
            if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
                raise ValueError("invalid new history duration")
        observation = {
            "run_id": "<new run id>",
            "attempt": "primary",
            "outcome": outcome,
            "duration_ms": 0,
        }
        if nodeid in called:
            expected["tests"][nodeid]["duration_ms"] = 0
            normalized["tests"][nodeid]["duration_ms"] = 0
        expected["tests"][nodeid]["observations"] = [*prior[-19:], observation]
        normalized["tests"][nodeid]["observations"][-1] = {
            **latest,
            "run_id": "<new run id>",
            "duration_ms": 0,
        }
    if len(run_ids) != 1 or normalized != expected:
        raise ValueError("history changed beyond selected new observations and durations")
    return normalized


def select_cases(cases: list[dict[str, Any]], count: int = 10) -> list[str]:
    """Fix ten evenly spaced passed cases before inspecting candidate results."""
    if count == 0:
        return [case["nodeid"] for case in cases]
    eligible = sorted(
        case["nodeid"]
        for case in cases
        if all(report["outcome"] == "passed" for report in case["reports"])
    )
    if len(eligible) < 10:
        raise ValueError("full suite has fewer than ten passed cases")
    return [eligible[index * (len(eligible) - 1) // 9] for index in range(count)]


def cli_selectors(project: Project, root: Path, nodeids: tuple[str, ...]) -> tuple[str, ...]:
    """Resolve pytest-root-relative identities without changing upstream root discovery."""
    selectors = []
    for nodeid in nodeids:
        filename, separator, suffix = nodeid.partition("::")
        direct = root / filename
        if direct.is_file():
            candidates = {direct.resolve()}
        else:
            candidates = {
                (root / test_root / filename).resolve()
                for test_root in expanded_tests(root, project.tests)
                if (root / test_root).is_dir() and (root / test_root / filename).is_file()
            }
        if len(candidates) != 1:
            raise ValueError(f"node identity has no unique CLI source path: {nodeid}")
        path = candidates.pop()
        if not path.is_relative_to(root.resolve()):
            raise ValueError(f"node identity source is outside the project: {nodeid}")
        selectors.append(f"{path.relative_to(root.resolve()).as_posix()}{separator}{suffix}")
    return tuple(selectors)


def case_outcomes(cases: list[dict[str, Any]]) -> dict[str, str]:
    """Compare persisted history with public phase outcomes independently."""
    outcomes: dict[str, str] = {}
    for case in cases:
        nodeid = case["nodeid"]
        if not isinstance(nodeid, str):
            raise ValueError("case identity must be a string")
        reports = case["reports"]
        if any(report["outcome"] in ("failed", "error") for report in reports):
            raise ValueError("successful benchmark child contains a failed phase")
        if any(
            report["phase"] == "call" and report["outcome"] in ("passed", "xpassed")
            for report in reports
        ):
            outcomes[nodeid] = "passed"
        elif any(report["outcome"] in ("skipped", "xfailed") for report in reports):
            outcomes[nodeid] = "skipped"
        else:
            raise ValueError("case has no completed call or skip outcome")
    return outcomes


def prepare(args: argparse.Namespace) -> None:
    artifacts = json.loads(args.artifacts.read_text())
    roles = ordered_roles(artifacts)
    args.output.mkdir(parents=True, exist_ok=False)
    prepared = {}
    for key in args.project:
        project = PROJECTS_BY_KEY[key]
        destination = args.output / key
        root = checkout(project, destination / "project")
        prepared[key] = {}
        for role in roles:
            wheel = Path(artifacts[role]["wheel"]).absolute()
            python = prepare_python(
                project,
                destination / role,
                args.output / "preparation" / role,
                wheel,
                args.python_version,
            )
            prepared[key][role] = {
                "python": str(python),
                "wheel": str(wheel),
                "build_manifest": str(Path(artifacts[role]["build_manifest"]).absolute()),
            }
        prepare_project(
            project,
            root,
            Path(prepared[key]["baseline"]["python"]),
            destination / "setup",
            args.timeout,
        )
        write_atomic(
            destination / "upstream.json",
            {
                "repository": project.repository,
                "revision": project.revision,
                "status_after_setup": git(root, "status", "--porcelain"),
                "diff_after_setup": git(root, "diff"),
            },
        )
    write_atomic(args.output / "runtimes.json", prepared)


def run_child(
    project: Project,
    root: Path,
    python: Path,
    output: Path,
    selected: tuple[str, ...],
    seed: dict[str, Any] | None,
    seed_bytes: bytes | None,
    timeout: float,
    expected_nodeids: tuple[str, ...] | None = None,
) -> tuple[float, dict[str, Any]]:
    output.mkdir(parents=True, exist_ok=False)
    state = root / ".vfy"
    if state.exists():
        shutil.rmtree(state)
    state.mkdir()
    history_path = state / "test-history.json"
    if seed_bytes is not None:
        history_path.write_bytes(seed_bytes)
    env = project_environment(project, root, python)
    env["PYTEST_NATIVE_RESULT"] = str(output / "native.json")
    env["COVERAGE_FILE"] = str(output / ".coverage")
    command = [
        str(python),
        "-m",
        "vfy",
        "test",
        "--workers=1",
        "--history",
        str(history_path),
        "--data-file",
        str(output / ".coverage"),
        "--source",
        project.coverage_args[0].split("=", 1)[1],
        *plugin_args(project),
        *project.extra_args,
        *selected,
        *project.coverage_args,
        "--cov-branch",
        "--cov-context=test",
        f"--cov-report=json:{output / 'coverage.json'}",
        f"--junitxml={output / 'junit.xml'}",
        "-p",
        "no:cacheprovider",
        "-q",
    ]
    write_atomic(output / "command.json", {"argv": command, "cwd": str(root)})
    start = time.perf_counter()
    completed = run_captured(command, cwd=root, env=env, timeout=timeout)
    elapsed = time.perf_counter() - start
    (output / "stdout.txt").write_text(completed.stdout)
    (output / "stderr.txt").write_text(completed.stderr)
    write_atomic(output / "status.json", {"exit_status": completed.returncode, "seconds": elapsed})
    if completed.returncode:
        raise ValueError(f"child failed; see {output}")
    raw = json.loads((output / "native.json").read_text())
    cases, warnings = normalize_native(raw, root, python, root / "pytest-tmp")
    validate_cases(cases, warnings)
    if raw.get("engine") != "pytest-runner-native-v1" or raw.get("collected") != len(cases):
        raise ValueError("native execution record is incomplete")
    if not cases or len({case["nodeid"] for case in cases}) != len(cases):
        raise ValueError("native execution inventory is empty or duplicated")
    case_nodeids = {case["nodeid"] for case in cases}
    if expected_nodeids is not None and sorted(case_nodeids) != sorted(expected_nodeids):
        raise ValueError("selected node identities differ")
    report = load_coverage_report(output / "coverage.json", root)
    coverage_reference(root, output, config_file=True, branch=True)
    oracle = normalized_coverage_json(output / "coverage-reference.json", root)
    if oracle != normalized_coverage_json(output / "coverage.json", root):
        raise ValueError("report differs from Coverage.py on this child's data")
    history = json.loads(history_path.read_text())
    shutil.copyfile(history_path, output / "history.json")
    outcomes = case_outcomes(cases)
    called = {
        case["nodeid"]
        for case in cases
        if any(report["phase"] == "call" for report in case["reports"])
    }
    if seed is None and set(history.get("tests", {})) != case_nodeids:
        raise ValueError("full/cold history inventory differs from executed cases")
    if seed is not None:
        history = normalized_history(history, seed, outcomes, called)
    else:
        empty_seed = {
            **history,
            "tests": {
                node: {**entry, "observations": []} for node, entry in history["tests"].items()
            },
        }
        normalized_history(history, empty_seed, outcomes, called)
    proof = {
        "cases": cases,
        "warnings": warnings,
        "junit": junit_outcomes(output / "junit.xml"),
        "coverage": json.loads(json.dumps(normalized_data(output / ".coverage", root))),
        "report": report,
        "history": history,
        "called": sorted(called),
    }
    write_atomic(output / "proof.json", proof)
    return elapsed, proof


def verify_runtime_parity(identity: dict[str, Any]) -> None:
    """Hold interpreter, dependencies, and pytest bytes constant between wheels."""
    fields = ("python", "implementation", "dependencies", "pytest_files")
    baseline = identity["baseline"]["installed"]
    for field in fields:
        if baseline.get(field) is None:
            raise ValueError(f"project runtime metadata is missing {field}")
    for role, record in identity.items():
        for field in fields:
            if record["installed"].get(field) != baseline.get(field):
                raise ValueError(f"project runtime {field} differs for {role}")


def runtime_identity(project: Project, root: Path, runtimes: dict[str, Any]) -> dict[str, Any]:
    identity = {}
    roles = ordered_roles(runtimes)
    for role in roles:
        runtime = runtimes[role]
        python = Path(runtime["python"])
        installed = interpreter_details(python, root, project_environment(project, root, python))
        require_controller_python(installed)
        wheel = Path(runtime["wheel"])
        verify_installed_distribution_files(role, "vfy", installed, wheel)
        identity[role] = {
            "installed": installed,
            "build": verify_build_identity(
                Path(runtime["build_manifest"]), installed, wheel, retained=True
            ),
        }
    verify_runtime_parity(identity)
    return identity


def full_suite_seed(
    args: argparse.Namespace,
    project: Project,
    root: Path,
    baseline: dict[str, str],
    identity: dict[str, Any],
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    seed, seed_bytes, full_proof = None, None, None
    full_nodeids = None
    for history_index in range(args.history_runs):
        full = args.prepared / project.key / f"full-suite-seed-{history_index:02d}"
        if (full / "proof.json").is_file():
            recorded_identity = json.loads((full / "seed-identity.json").read_text())
            if recorded_identity != identity:
                raise ValueError("cached full-suite seed artifact identity changed")
            full_proof = json.loads((full / "proof.json").read_text())
        else:
            _, full_proof = run_child(
                project,
                root,
                Path(baseline["python"]),
                full,
                expanded_tests(root, project.tests),
                seed,
                seed_bytes,
                args.timeout,
                full_nodeids,
            )
            write_atomic(full / "seed-identity.json", identity)
        seed_bytes = (full / "history.json").read_bytes()
        seed = json.loads(seed_bytes)
        full_nodeids = tuple(case["nodeid"] for case in full_proof["cases"])
    assert full_proof is not None
    assert seed is not None
    assert seed_bytes is not None
    return seed, seed_bytes, full_proof


def measure(args: argparse.Namespace) -> None:
    controller = require_clean(Path(__file__).resolve().parents[2])
    prepared = json.loads((args.prepared / "runtimes.json").read_text())
    result = {"controller": controller.as_dict(), "machine": hardware_details(), "projects": []}
    for key in args.project:
        project = PROJECTS_BY_KEY[key]
        root = args.prepared / key / "project"
        if git(root, "rev-parse", "HEAD") != project.revision:
            raise ValueError("upstream revision changed")
        setup = json.loads((args.prepared / key / "upstream.json").read_text())
        if git(root, "diff") != setup["diff_after_setup"]:
            raise ValueError("upstream tracked workload changed since preparation")
        identity = runtime_identity(project, root, prepared[key])
        roles = ordered_roles(prepared[key])
        seed, seed_bytes, full_proof = full_suite_seed(
            args, project, root, prepared[key]["baseline"], identity["baseline"]
        )
        row = {
            "project": key,
            "revision": project.revision,
            "identity": identity,
            "roles": roles,
            "full_suite_count": len(full_proof["cases"]),
            "seed_bytes": len(seed_bytes),
            "seed_sha256": hashlib.sha256(seed_bytes).hexdigest(),
            "observations": sorted(
                {len(value["observations"]) for value in seed["tests"].values()}
            ),
            "actual_full_suite_seed_runs": args.history_runs,
            "strata": [],
        }
        result["projects"].append(row)
        for count in args.counts:
            selected = tuple(select_cases(full_proof["cases"], count))
            test_args = (
                cli_selectors(project, root, selected)
                if count
                else expanded_tests(root, project.tests)
            )
            for history_state in args.history_state:
                stratum = {
                    "count": len(selected),
                    "history_state": history_state,
                    "nodeids": selected,
                    "samples": {role: [] for role in roles},
                }
                seed_input = seed if history_state == "retained" else None
                raw_seed = seed_bytes if seed_input is not None else None
                reference = None
                for phase, repeats in (
                    ("parity", 1),
                    ("warmup", args.warmups),
                    ("measured", args.runs),
                ):
                    for index in range(repeats):
                        offset = index % len(roles)
                        order = roles[offset:] + roles[:offset]
                        for role in order:
                            destination = (
                                args.output
                                / key
                                / f"n{count}-{history_state}"
                                / f"{phase}-{index:02d}-{role}"
                            )
                            elapsed, proof = run_child(
                                project,
                                root,
                                Path(prepared[key][role]["python"]),
                                destination,
                                test_args,
                                seed_input,
                                raw_seed,
                                args.timeout,
                                selected,
                            )
                            comparable = dict(proof)
                            if seed_input is None:
                                # Cold history normalizes only fresh durations and run identity.
                                empty_seed = {
                                    **proof["history"],
                                    "tests": {
                                        node: {**entry, "observations": []}
                                        for node, entry in proof["history"]["tests"].items()
                                    },
                                }
                                comparable["history"] = normalized_history(
                                    proof["history"],
                                    empty_seed,
                                    case_outcomes(proof["cases"]),
                                    set(proof["called"]),
                                )
                            if reference is None:
                                reference = comparable
                            elif comparable != reference:
                                raise ValueError(
                                    f"semantic parity changed; timing rejected: {destination}"
                                )
                            if phase == "measured":
                                stratum["samples"][role].append(elapsed)
                            write_atomic(
                                destination / "validated.json",
                                {"semantic_digest": report_digest(comparable)},
                            )
                stratum["median_seconds"] = {
                    role: statistics.median(samples) if samples else None
                    for role, samples in stratum["samples"].items()
                }
                row["strata"].append(stratum)
                write_atomic(args.output / "result.json", result)
                sys.stdout.write(f"{key} {count} {history_state} {stratum['median_seconds']}\n")
                sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepared", type=Path)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--project", choices=("iniconfig", "packaging", "rich"), action="append")
    parser.add_argument("--python-version", default="3.12")
    parser.add_argument("--counts", type=int, nargs="+", choices=(1, 10, 0), default=[1, 10])
    parser.add_argument(
        "--history-state", choices=("cold", "retained"), nargs="+", default=["retained"]
    )
    parser.add_argument("--history-runs", type=int, default=1)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--runs", type=int, default=15)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    args.output = args.output.absolute()
    args.prepared = (args.prepared or args.output).absolute()
    args.project = args.project or ["rich", "iniconfig"]
    if args.warmups < 0 or args.runs < 0 or not 1 <= args.history_runs <= 20:
        parser.error("sample counts must be nonnegative")
    if args.prepare_only:
        if args.artifacts is None:
            parser.error("--prepare-only requires --artifacts")
        prepare(args)
    else:
        measure(args)


if __name__ == "__main__":
    main()
