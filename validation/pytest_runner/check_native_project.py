"""Compare an installed native runner with pytest on the pinned Rich suite.

Both execution routes use vfy coverage to isolate the execution boundary.
Each report is checked against Coverage.py using that run's own collected data.
This is an untimed adoption check, not an independent collection-parity claim.
Each route receives a fresh checkout; original project files remain unchanged.
Reference failures are retained and must match; infrastructure exits are rejected.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TypedDict

import coverage

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from benchmarks.benchmark_projects import load_json  # noqa: E402
from benchmarks.project_manifest import manifest  # noqa: E402
from scripts.process_tree import run_captured  # noqa: E402
from scripts.pytest_execution_record import (  # noqa: E402
    controlled_environment,
    file_sha256,
    resolve_executable,
    resolved_environment,
    verify_installed_artifacts,
    write_atomic,
)

type JunitCase = tuple[str, str, tuple[tuple[str, str], ...]]


class CoverageResult(TypedDict):
    files: dict[str, dict[str, object]]
    totals: object


class ProjectResult(CoverageResult):
    cases: list[JunitCase]
    exit_status: int


def junit_cases(path: Path) -> list[JunitCase]:
    """Compare ordered test identities and outcomes, excluding timings."""
    root = ET.parse(path).getroot()
    cases = [
        (
            case.attrib["classname"],
            case.attrib["name"],
            tuple(
                (child.tag, child.attrib.get("message", ""))
                for child in case
                if child.tag in {"failure", "error", "skipped"}
            ),
        )
        for case in root.iter("testcase")
    ]
    if not cases:
        raise ValueError(f"no test cases in {path}")
    return cases


def git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments], text=True, timeout=60
    ).strip()


def compare_cases(reference: list[JunitCase], candidate: list[JunitCase]) -> None:
    """Reject partial, reordered, or semantically different test outcomes."""
    if candidate != reference:
        raise ValueError("project execution differs in cases; inspect JUnit records")


def compare_results(reference: ProjectResult, candidate: ProjectResult) -> None:
    """Require equal process status and complete ordered JUnit outcomes."""
    if candidate["exit_status"] != reference["exit_status"]:
        raise ValueError("project execution exit status differs")
    if not reference["cases"]:
        raise ValueError("project reference produced no test cases")
    compare_cases(reference["cases"], candidate["cases"])


def compare_coverage(reference: CoverageResult, candidate: CoverageResult) -> None:
    """Compare independent reports of the same collected observations."""
    for field in ("files", "totals"):
        if candidate[field] != reference[field]:
            raise ValueError(f"coverage report differs in {field}; inspect normalized records")


def coverage_reference(
    root: Path,
    output: Path,
    *,
    config_file: str | bool | None = None,
    branch: bool = True,
) -> CoverageResult:
    """Report the run's schema-7 data with the pinned independent analyzer."""
    if coverage.__version__ != "7.15.4":
        raise ValueError("the project report oracle requires Coverage.py 7.15.4")
    data_file = output / ".coverage"
    before = file_sha256(data_file)
    destination = output / "coverage-reference.json"
    with contextlib.chdir(root):
        reference = coverage.Coverage(
            data_file=str(data_file),
            branch=branch,
            config_file=str(root / ".coveragerc") if config_file is None else config_file,
        )
        try:
            reference.load()
            reference.json_report(outfile=str(destination))
        finally:
            reference.get_data().close(force=True)
    if file_sha256(data_file) != before:
        raise ValueError("coverage report oracle changed the collected data")
    report = json.loads(destination.read_text(encoding="utf-8"))
    return {"files": load_json(destination, root), "totals": report["totals"]}


def run_route(python: Path, root: Path, output: Path, *, workers: int | None) -> ProjectResult:
    """Run the actual public command with coverage and JUnit output."""
    output.mkdir()
    environment = controlled_environment(pythonpath=str(root))
    for name in tuple(environment):
        if name.startswith(("VFY_", "PYTEST_NATIVE_", "COVERAGE_")):
            environment.pop(name)
    environment.update(
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        TERM="xterm-256color",
        COVERAGE_FILE=str(output / ".coverage"),
    )
    reports = [
        "--cov=rich",
        "--cov-branch",
        "--cov-context=test",
        f"--cov-report=json:{output / 'coverage.json'}",
        f"--junitxml={output / 'junit.xml'}",
        "-q",
        "tests",
    ]
    if workers is None:
        command = [str(python), "-m", "pytest", "-p", "vfy.pytest_plugin", *reports]
    else:
        environment["PYTEST_NATIVE_RESULT"] = str(output / "native.json")
        command = [
            str(python),
            "-m",
            "vfy",
            "test",
            "--engine=pytest-native",
            "--all",
            f"--workers={workers}",
            "--history",
            str(output / "test-history.sqlite3"),
            *reports,
        ]
    completed = run_captured(command, cwd=root, env=environment, timeout=300)
    (output / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (output / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    write_atomic(output / "command.json", {"argv": command, "status": completed.returncode})
    if completed.returncode not in (0, 1):
        raise RuntimeError(f"project run failed ({completed.returncode}); inspect {output}")
    cases = junit_cases(output / "junit.xml")
    failures = sum(
        any(tag in {"failure", "error"} for tag, _ in outcomes) for _, _, outcomes in cases
    )
    if bool(failures) != (completed.returncode == 1):
        raise ValueError("project exit status disagrees with JUnit failures")
    if workers is not None:
        native = json.loads((output / "native.json").read_text(encoding="utf-8"))
        if native.get("engine") != "pytest-runner-native-v1":
            raise ValueError("project run did not record native execution")
        if len(native["cases"]) != len(cases):
            raise ValueError("native execution and JUnit case counts differ")
    report = json.loads((output / "coverage.json").read_text(encoding="utf-8"))
    result: ProjectResult = {
        "cases": cases,
        "exit_status": completed.returncode,
        "files": load_json(output / "coverage.json", root),
        "totals": report["totals"],
    }
    reference = coverage_reference(root, output)
    write_atomic(output / "coverage-reference-normalized.json", dict(reference))
    write_atomic(output / "coverage-normalized.json", dict(result))
    compare_coverage(reference, result)
    return result


def check_edit_recovery(python: Path, root: Path, output: Path) -> list[dict[str, object]]:
    """Prove a real source regression fails in native execution, then repairs."""
    patch = next(patch for patch in manifest("rich").patches if patch.kind == "breaking")
    source = root / patch.path
    original = source.read_text(encoding="utf-8")
    if original.count(patch.before) != 1:
        raise ValueError("reviewed Rich edit does not match the pinned source")
    rows: list[dict[str, object]] = []
    try:
        for stage, expected_status in (("breaking", 1), ("repair", 0)):
            source.write_text(
                original.replace(patch.before, patch.after) if stage == "breaking" else original,
                encoding="utf-8",
            )
            directory = output / stage
            directory.mkdir()
            environment = controlled_environment(pythonpath=str(root))
            for name in tuple(environment):
                if name.startswith(("VFY_", "PYTEST_NATIVE_", "COVERAGE_")):
                    environment.pop(name)
            environment.update(
                PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                PYTHONPYCACHEPREFIX=str(directory / "bytecode"),
                TERM="xterm-256color",
            )
            record = directory / "native.json"
            environment["PYTEST_NATIVE_RESULT"] = str(record)
            command = [
                str(python),
                "-m",
                "vfy",
                "test",
                "--engine=pytest-native",
                "--history",
                str(directory / "test-history.sqlite3"),
                "--no-cov",
                "-q",
                *patch.tests,
            ]
            completed = run_captured(command, cwd=root, env=environment, timeout=60)
            (directory / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
            (directory / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
            write_atomic(
                directory / "command.json", {"argv": command, "status": completed.returncode}
            )
            if completed.returncode != expected_status:
                raise ValueError(f"{stage}: expected exit {expected_status}; inspect {directory}")
            payload = json.loads(record.read_text(encoding="utf-8"))
            if payload.get("engine") != "pytest-runner-native-v1" or not payload.get("cases"):
                raise ValueError(f"{stage}: missing native test execution")
            failures = [
                case["nodeid"]
                for case in payload["cases"]
                if any(
                    phase["phase"] == "call" and phase["outcome"] == "failed"
                    for phase in case["phases"]
                )
            ]
            if (stage == "breaking") != bool(failures):
                raise ValueError(f"{stage}: exit status lacks matching test-call evidence")
            rows.append(
                {
                    "stage": stage,
                    "exit_status": completed.returncode,
                    "failed_calls": failures,
                    "cases": len(payload["cases"]),
                }
            )
    finally:
        source.write_text(original, encoding="utf-8")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--vfy-wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if coverage.__version__ != "7.15.4":
        raise ValueError("the project report oracle requires Coverage.py 7.15.4")
    project = manifest("rich")
    upstream = args.upstream_root.resolve()
    if git(upstream, "rev-parse", "HEAD") != project.revision:
        raise ValueError("Rich checkout does not match the committed project manifest")
    if git(upstream, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Rich checkout has tracked modifications")
    python = resolve_executable(args.python)
    wheel = args.vfy_wheel.resolve()
    installed = verify_installed_artifacts(python, [wheel], "vfy")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    results: dict[str, ProjectResult] = {}
    for route, workers in (("pytest", None), ("pytest-native", 1), ("native-workers", 2)):
        checkout = output / f"{route}-project"
        subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(upstream), str(checkout)],
            check=True,
            timeout=120,
        )
        results[route] = run_route(python, checkout, output / route, workers=workers)
        if git(checkout, "status", "--porcelain"):
            raise ValueError(f"{route} modified upstream files")
        write_atomic(output / f"{route}-normalized.json", dict(results[route]))
    for route in ("pytest-native", "native-workers"):
        compare_results(results["pytest"], results[route])
    edit_sequence = check_edit_recovery(python, output / "native-project", output)
    if git(output / "native-project", "status", "--porcelain"):
        raise ValueError("edit sequence did not restore the pristine project")
    write_atomic(
        output / "result.json",
        {
            "project": project.key,
            "revision": project.revision,
            "scope": "full Rich suite; plug-in autoload disabled on all routes",
            "cases": len(results["pytest"]["cases"]),
            "routes": list(results),
            "route_statuses": {route: result["exit_status"] for route, result in results.items()},
            "reference_failures": sum(
                any(tag in {"failure", "error"} for tag, _ in outcomes)
                for _, _, outcomes in results["pytest"]["cases"]
            ),
            "excluded_tests": [],
            "edit_sequence": edit_sequence,
            "edit_bytecode_cache": "isolated per stage; not an import-cache invalidation check",
            "comparison": (
                "equal exit status and ordered JUnit identities/outcomes, "
                "including reference failures"
            ),
            "coverage_comparison": "each route's complete JSON files/totals against its own data",
            "coverage_oracle": {
                "version": coverage.__version__,
                "python": sys.version,
                "scope": "report parity over same-run schema-7 data, not collection parity",
            },
            "cross_run_coverage_hits_compared": False,
            "transcripts_compared": False,
            "coverage_implementation": "vfy on both execution routes",
            "timing_claim": False,
            "checker_sha256": file_sha256(Path(__file__)),
            "vfy_wheel_sha256": file_sha256(wheel),
            "installed_artifacts": installed,
            "environment": resolved_environment(python),
            "project_dependency_lock_sha256": file_sha256(ROOT / project.dependency_lock),
        },
    )
    sys.stdout.write(f"{output / 'result.json'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
