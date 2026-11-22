"""Compare optional pinned upstream workloads using the public xdist options.

Both routes load pytest-xdist and receive identical worker/distribution options.
JUnit retains outcomes, diagnostics, and literal captures; this checker does
not implement the adoption matrix's complete warning/phase observer protocol.
Coverage uses the shared schema-7 signatures and pinned report oracle.
Use ``--phase outcomes`` when reporting an execution-only result; the default
also runs the complete workload with coverage and keeps its exact parity gate.
Coverage always runs an outcomes baseline and must preserve its case outcomes.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from validation.pytest_runner import check_native_adoption_matrix as matrix  # noqa: E402

LOCK = ROOT / "validation/pytest_runner/native-xdist-requirements.txt"
logger = logging.getLogger(__name__)
Phase = Literal["outcomes", "coverage"]


@dataclass(frozen=True)
class Workload:
    project: matrix.Project
    declaration: str
    data_repository: str | None = None
    data_revision: str | None = None


WORKLOADS = {
    "chardet": Workload(
        matrix.Project(
            "chardet",
            "chardet/chardet",
            "dcf07fb97b082df57cf06125a0f0778e9aaef79d",
            "src",
            ("tests", "scripts/tests"),
            ("--cov=chardet", "--cov-branch"),
            plugins=("xdist.plugin",),
            dependency_lock=LOCK,
            setup_args=(".",),
            generated_files=("src/chardet/_version.py",),
            scope="complete configured chardet 7.6.0 suite; upstream deselects benchmarks",
        ),
        "CLAUDE.md: full pytest -n auto; tests/test_accuracy.py: pytest -n auto",
        "chardet/test-data",
        "10bf971cf01cb6ae18266c23be05fcca8008d99f",
    ),
    "requests-cache": Workload(
        matrix.Project(
            "requests-cache",
            "requests-cache/requests-cache",
            "2cdd81a3d71319fee175ecab47c550c7e2395dda",
            ".",
            ("tests/unit",),
            ("--cov=requests_cache", "--cov-branch"),
            plugins=("xdist.plugin",),
            dependency_lock=LOCK,
            scope="complete requests-cache v1.3.3 upstream deploy tests/unit phase; "
            "integration and compatibility phases are outside this workload",
        ),
        ".github/workflows/deploy.yml: pytest -n auto tests/unit; "
        "noxfile.py: --numprocesses=auto --dist=loadfile",
    ),
    "black": Workload(
        replace(
            matrix.PROJECTS_BY_KEY["black"],
            plugins=("xdist.plugin",),
            stock_plugins=(),
            stock_extra_args=(),
        ),
        "tox.ini: official no_jupyter phase with --numprocesses auto",
    ),
}


def command(
    project: matrix.Project,
    root: Path,
    python: Path,
    output: Path,
    basetemp: Path,
    route: Literal["stock", "pytest-native"],
    *,
    workers: int,
    coverage: bool,
) -> list[str]:
    """Give stock pytest and native vfy the same actual xdist invocation."""
    prefix = (
        [str(python), "-P", "-m", "pytest"]
        if route == "stock"
        else [
            str(python),
            "-m",
            "vfy",
            "test",
            "--engine=pytest-native",
            "--history",
            str(output / "history.json"),
        ]
    )
    if coverage:
        if route == "stock":
            prefix.extend(("-p", "vfy.pytest_plugin"))
        coverage_args = [
            *project.coverage_args,
            "--cov-context=test",
            f"--cov-report=json:{output / 'coverage.json'}",
        ]
    else:
        coverage_args = [] if route == "stock" else ["--no-cov"]
    return [
        *prefix,
        *matrix.plugin_args(project),
        "-n" if route == "stock" else "--numprocesses",
        str(workers),
        "--dist",
        "loadfile",
        "--basetemp",
        str(basetemp),
        f"--junitxml={output / 'junit.xml'}",
        "-o",
        "junit_logging=all",
        "-o",
        "junit_log_passing_tests=true",
        "-q",
        *coverage_args,
        *project.extra_args,
        *matrix.expanded_tests(root, project.tests),
    ]


def junit_records(path: Path, root: Path, basetemp: Path) -> list[dict[str, Any]]:
    """Retain every testcase body; remove only checkout/temp path identities."""

    def normalize(value: str) -> str:
        return value.replace(str(root), "<CHECKOUT>").replace(str(basetemp), "<PYTEST_TEMP>")

    def element_record(element: ET.Element) -> dict[str, Any]:
        return {
            "tag": element.tag,
            "attributes": {key: normalize(value) for key, value in element.attrib.items()},
            "text": normalize(element.text or ""),
            "children": [element_record(child) for child in element],
        }

    records = [
        {
            "identity": [normalize(case.get("classname", "")), normalize(case.get("name", ""))],
            "children": [element_record(child) for child in case],
        }
        for case in ET.parse(path).getroot().iter("testcase")
    ]
    # Distributed completion order is unspecified. Preserve duplicate occurrences
    # and every diagnostic/capture value while comparing testcase multisets.
    return sorted(records, key=lambda value: json.dumps(value, sort_keys=True))


def run_route(
    project: matrix.Project,
    root: Path,
    python: Path,
    output: Path,
    basetemp: Path,
    route: Literal["stock", "pytest-native"],
    *,
    workers: int,
    coverage: bool,
    timeout: float,
) -> dict[str, Any]:
    environment = matrix.project_environment(project, root, python)
    environment["COVERAGE_FILE"] = str(output / ".coverage")
    if route == "pytest-native":
        environment["PYTEST_NATIVE_RESULT"] = str(output / "native.json")
    run = matrix.invoke(
        command(project, root, python, output, basetemp, route, workers=workers, coverage=coverage),
        root,
        environment,
        output,
        timeout,
    )
    errors = []
    try:
        cases = junit_records(output / "junit.xml", root, basetemp)
        if not cases:
            errors.append("JUnit contains no testcases")
    except (OSError, ET.ParseError) as error:
        cases = []
        errors.append(f"JUnit: {error}")
    native_execution = []
    if route == "pytest-native":
        try:
            native_execution = native_execution_records(
                output, root, python, basetemp, workers=workers, junit_cases=cases
            )
        except (OSError, KeyError, TypeError, ValueError) as error:
            errors.append(f"native execution: {error}")
    signature = matrix.coverage_result(root, output) if coverage else None
    return {
        "exit_status": run["exit_status"],
        "seconds": run["seconds"],
        "junit_cases": cases,
        "record_errors": errors,
        "native_execution": native_execution,
        "coverage": signature,
    }


def native_execution_records(
    output: Path,
    root: Path,
    python: Path,
    basetemp: Path,
    *,
    workers: int,
    junit_cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Verify native engines and complete phase records against JUnit case counts."""
    paths = (
        [output / f"native.json.gw{index}" for index in range(workers)]
        if workers
        else [output / "native.json"]
    )
    if set(output.glob("native.json*")) != set(paths):
        raise ValueError("missing or unexpected native worker result files")
    records = []
    executed = 0
    case_count = len(junit_cases)
    for path in paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{path.name}: execution record is not an object")
        cases, session = matrix.normalize_native(raw, root, python, basetemp)
        matrix.validate_cases(cases, session)
        collected = raw.get("collected")
        if type(collected) is not int or collected != case_count:
            raise ValueError(f"{path.name}: native collection count differs from JUnit inventory")
        executed += len(cases)
        records.append(
            {
                "path": str(path),
                "sha256": matrix.file_sha256(path),
                "engine": raw["engine"],
                "collected": collected,
                "cases": len(cases),
            }
        )
    if executed != case_count or not executed:
        raise ValueError("native execution count differs from JUnit inventory")
    return records


def compare_routes(
    stock: dict[str, Any], native: dict[str, Any], *, coverage: bool
) -> dict[str, Any]:
    cases_equal = bool(stock["junit_cases"]) and stock["junit_cases"] == native["junit_cases"]
    result: dict[str, Any] = {
        "successful": stock["exit_status"] == native["exit_status"] == 0,
        "junit_equal": cases_equal,
        "junit_difference": matrix.first_difference(stock["junit_cases"], native["junit_cases"]),
        "record_errors": [*stock["record_errors"], *native["record_errors"]],
    }
    if coverage:
        baseline, candidate = stock["coverage"], native["coverage"]
        for field in ("normalized_json", "data", "aggregate_data"):
            result[f"{field}_equal"] = bool(
                baseline and candidate and baseline[field] == candidate[field]
            )
            result[f"{field}_difference"] = (
                matrix.first_difference(baseline[field], candidate[field])
                if baseline and candidate
                else {
                    "stock_available": baseline is not None,
                    "native_available": candidate is not None,
                }
            )
        result["report_oracles_equal"] = bool(
            baseline
            and candidate
            and baseline["report_oracle"]["equal"]
            and candidate["report_oracle"]["equal"]
        )
    result["equal"] = bool(
        result["successful"]
        and cases_equal
        and not result["record_errors"]
        and (
            not coverage
            or all(
                result[f"{field}_equal"]
                for field in ("normalized_json", "data", "aggregate_data", "report_oracles")
            )
        )
    )
    return result


def compare_phase_outcomes(baseline: dict[str, Any], covered: dict[str, Any]) -> dict[str, Any]:
    """Require coverage to preserve the uninstrumented inventory and outcomes."""

    def outcomes(run: dict[str, Any]) -> list[dict[str, Any]]:
        records = [
            {
                "identity": case["identity"],
                "outcomes": [
                    {"outcome": child["tag"], "type": child["attributes"].get("type", "")}
                    for child in case["children"]
                    if child["tag"] in {"failure", "error", "skipped"}
                ],
            }
            for case in run["junit_cases"]
        ]
        return sorted(records, key=lambda value: json.dumps(value, sort_keys=True))

    expected, actual = outcomes(baseline), outcomes(covered)
    return {
        "equal": bool(expected) and expected == actual,
        "difference": matrix.first_difference(expected, actual),
    }


def run_workload(
    workload: Workload,
    output: Path,
    wheel: Path,
    python_version: str,
    workers: tuple[int, ...],
    timeout: float,
    *,
    phases: tuple[Phase, ...] = ("outcomes", "coverage"),
) -> dict[str, Any]:
    if "coverage" in phases:
        phases = ("outcomes", "coverage")
    project = workload.project
    with tempfile.TemporaryDirectory(prefix=f"vfy-xdist-{project.key}-") as temporary:
        work = Path(temporary)
        upstream = matrix.checkout(project, work / "upstream")
        python = matrix.prepare_python(project, work, output, wheel, python_version)
        data = None
        if workload.data_repository is not None and workload.data_revision is not None:
            data_project = matrix.Project(
                "test-data",
                workload.data_repository,
                workload.data_revision,
                ".",
                (),
                (),
            )
            data = matrix.checkout(data_project, work / "test-data")
        provenance = {
            "repository": project.repository,
            "revision": project.revision,
            "tree": matrix.git(upstream, "rev-parse", "HEAD^{tree}"),
            "scope": project.scope,
            "xdist_declaration": workload.declaration,
            "data_repository": workload.data_repository,
            "data_revision": workload.data_revision,
            "data_tree": matrix.git(data, "rev-parse", "HEAD^{tree}") if data else None,
        }
        matrix.write_atomic(output / "source.json", provenance)
        subprocess.run(
            [
                "git",
                "-C",
                str(upstream),
                "archive",
                "--format=tar.gz",
                f"--output={output / 'source.tar.gz'}",
                "HEAD",
            ],
            check=True,
            timeout=300,
        )
        if data is not None:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(data),
                    "archive",
                    "--format=tar.gz",
                    f"--output={output / 'test-data.tar.gz'}",
                    "HEAD",
                ],
                check=True,
                timeout=300,
            )
        comparisons: list[dict[str, Any]] = []
        for count in workers:
            outcome_runs: dict[str, dict[str, Any]] = {}
            for phase in phases:
                coverage = phase == "coverage"
                pair: dict[str, dict[str, Any]] = {}
                for route in ("stock", "pytest-native"):
                    destination = work / f"{route}-{count}-{phase}"
                    root = matrix.copy_checkout(upstream, destination)
                    route_output = output / f"workers-{count}" / f"{route}-{phase}"
                    if project.setup_args:
                        matrix.prepare_project(
                            project,
                            root,
                            python,
                            route_output.with_name(f"{route}-{phase}-setup"),
                            timeout,
                        )
                    if data is not None:
                        shutil.copytree(
                            data, root / "tests/data", ignore=shutil.ignore_patterns(".git")
                        )
                        (root / "tests/data/.test-data-ref").write_text("7.6.0")
                    pair[route] = run_route(
                        project,
                        root,
                        python,
                        route_output,
                        work / "pytest-temp",
                        route,
                        workers=count,
                        coverage=coverage,
                        timeout=timeout,
                    )
                comparison = compare_routes(pair["stock"], pair["pytest-native"], coverage=coverage)
                if coverage:
                    comparison["coverage_outcomes"] = {
                        route: compare_phase_outcomes(outcome_runs[route], pair[route])
                        for route in ("stock", "pytest-native")
                    }
                    comparison["coverage_outcomes_equal"] = all(
                        record["equal"] for record in comparison["coverage_outcomes"].values()
                    )
                    comparison["equal"] = (
                        comparison["equal"] and comparison["coverage_outcomes_equal"]
                    )
                else:
                    outcome_runs = pair
                retained_routes = {}
                for route, run in pair.items():
                    signature_path = (
                        output
                        / f"workers-{count}"
                        / f"{route}-{phase}"
                        / "coverage-normalized.json"
                    )
                    retained_routes[route] = {
                        **run,
                        "coverage": {
                            "signature_path": str(signature_path),
                            "signature_sha256": matrix.file_sha256(signature_path),
                            "report_oracle_equal": run["coverage"]["report_oracle"]["equal"],
                        }
                        if run["coverage"]
                        else None,
                    }
                comparisons.append(
                    {
                        "workers": count,
                        "phase": phase,
                        "routes": retained_routes,
                        "comparison": comparison,
                    }
                )
                matrix.write_atomic(output / "partial-result.json", {"comparisons": comparisons})
        return {
            "project": project.key,
            **provenance,
            "comparisons": comparisons,
            "passed": all(record["comparison"]["equal"] for record in comparisons),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--build-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", choices=sorted(WORKLOADS), action="append")
    parser.add_argument("--workers", type=int, action="append")
    parser.add_argument(
        "--phase",
        choices=("outcomes", "coverage"),
        action="append",
        help="coverage also runs an outcomes baseline",
    )
    parser.add_argument("--python-version", default="3.12")
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args(argv)
    workers = tuple(args.workers if args.workers is not None else (0, 2))
    if not workers or any(value < 0 for value in workers) or len(set(workers)) != len(workers):
        parser.error("worker counts must be distinct nonnegative integers")
    selected = args.project or list(WORKLOADS)
    if len(set(selected)) != len(selected):
        parser.error("project selections must be unique")
    phases: tuple[Phase, ...] = tuple(
        phase for phase in ("outcomes", "coverage") if args.phase is None or phase in args.phase
    )
    if args.phase is not None and len(set(args.phase)) != len(args.phase):
        parser.error("phase selections must be unique")
    if "coverage" in phases:
        phases = ("outcomes", "coverage")
    wheel = args.wheel.resolve()
    manifest = json.loads(args.build_manifest.read_text())
    if manifest.get("wheel_sha256") != matrix.file_sha256(wheel):
        raise ValueError("candidate wheel hash differs from its build manifest")
    if manifest.get("native_profile") != "release":
        raise ValueError("candidate build manifest must record the release profile")
    matrix.validate_source_revision(manifest)
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"output must be absent or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    input_paths = {
        Path(__file__),
        LOCK,
        LOCK.with_suffix(".in"),
        ROOT / "validation/pytest_runner/check_native_adoption_matrix.py",
        *(WORKLOADS[key].project.dependency_lock or LOCK for key in selected),
    }
    inputs = {
        **matrix.SOURCE_INPUT_SHA256,
        **{str(path.relative_to(ROOT)): matrix.file_sha256(path) for path in sorted(input_paths)},
    }
    records: list[dict[str, Any]] = []
    for key in selected:
        logger.info("Running %s", key)
        try:
            record = run_workload(
                WORKLOADS[key],
                output / key,
                wheel,
                args.python_version,
                workers,
                args.timeout,
                phases=phases,
            )
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            record = {
                "project": key,
                "passed": False,
                "setup_error": f"{type(error).__name__}: {error}",
            }
        records.append(record)
        matrix.write_atomic(output / "partial-result.json", {"projects": records})
    changed = [name for name, digest in inputs.items() if matrix.file_sha256(ROOT / name) != digest]
    result = {
        "schema_version": 1,
        "candidate": manifest,
        "source_inputs_sha256": inputs,
        "changed_source_inputs": changed,
        "workers": workers,
        "phases": phases,
        "projects": records,
        "passed": not changed and all(record["passed"] for record in records),
    }
    matrix.write_atomic(output / "result.json", result)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
