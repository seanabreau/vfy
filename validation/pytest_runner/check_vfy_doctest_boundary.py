"""Compare text doctests through installed stock and native vfy commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support import VfyProject
from validation.pytest_runner.check_vfy_coverage_boundary import (
    PHASE_CONFTST,
    SOURCE,
    assert_run_evidence,
    command,
    normalized_coverage,
)
from validation.pytest_runner.upstream_pytest import pytest_version

EXPECTED_OUTCOMES = {
    "test_passing.txt::test_passing.txt": "passed",
    "test_failing.txt::test_failing.txt": "failed",
}
PASSING_DOCTEST = """vfy text doctest coverage.

>>> from src.sample import measured
>>> measured(True)
42
>>> getfixture("answer")
42
"""
FAILING_DOCTEST = """vfy text doctest failure.

>>> from src.sample import measured
>>> measured(False)
42
"""
FIXTURE_CONFTEST = """import pytest


@pytest.fixture
def answer():
    return 42
"""


def run_case(
    destination: Path,
    python: Path,
    *,
    candidate: bool,
) -> dict[str, object]:
    name = "candidate" if candidate else "stock"
    project = VfyProject(destination / f"project-{name}")
    project.write("src/__init__.py", "")
    project.write("src/sample.py", SOURCE)
    project.write("test_passing.txt", PASSING_DOCTEST)
    project.write("test_failing.txt", FAILING_DOCTEST)
    project.write(
        "conftest.py",
        FIXTURE_CONFTEST if candidate else f"{FIXTURE_CONFTEST}\n{PHASE_CONFTST}",
    )
    event = destination / f"{name}-unused-event.txt"
    native_result = destination / f"{name}-native-result.json"
    phase_log = destination / f"{name}-phases.jsonl"
    for artifact in (event, native_result, phase_log):
        artifact.unlink(missing_ok=True)
    status, stdout, stderr = command(
        project,
        python,
        (
            "--isolated",
            "test",
            f"--engine={'pytest-native' if candidate else 'pytest'}",
            "--workers=1",
            "--no-replay",
            "--history",
            ".vfy/test-history.json",
            "--data-file",
            ".coverage",
            "--source",
            "src",
            "--cov-branch",
            "--cov-context=test",
            "-c",
            os.devnull,
            "--rootdir",
            str(project.root),
            "--doctest-glob=*.txt",
            "test_passing.txt",
            "test_failing.txt",
        ),
        event=event,
        native_result=native_result,
        native_bridge=candidate,
        phase_log=None if candidate else phase_log,
    )
    if status != 1:
        raise RuntimeError(f"{name} doctest run returned {status}:\n{stdout}\n{stderr}")
    phases = assert_run_evidence(
        project,
        native_result,
        EXPECTED_OUTCOMES,
        candidate=candidate,
        phase_log=None if candidate else phase_log,
    )
    coverage_path = project.root / ".coverage"
    if not coverage_path.is_file():
        raise RuntimeError(f"{name} doctest run did not publish coverage")
    observation: dict[str, object] = {
        "exit_status": status,
        "phases": phases,
        "coverage": normalized_coverage(coverage_path, project.root),
    }
    if candidate:
        record = json.loads(native_result.read_text(encoding="utf-8"))
        cases = record.get("cases")
        if not isinstance(cases, list) or len(cases) != 2:
            raise RuntimeError(f"native doctest result has invalid cases: {record}")
        failing_phases = cases[1].get("phases") if isinstance(cases[1], dict) else None
        if not isinstance(failing_phases, list) or len(failing_phases) != 3:
            raise RuntimeError(f"native doctest failure has invalid phases: {cases[1]}")
        detail = failing_phases[1].get("detail")
        if not isinstance(detail, str) or "Expected:" not in detail or "Got:" not in detail:
            raise RuntimeError(f"native doctest failure omitted pytest detail: {cases[1]}")
        observation["failure_detail_complete"] = True
    return observation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-python", required=True, type=Path)
    parser.add_argument("--candidate-python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    versions = {
        "stock": pytest_version(args.stock_python),
        "candidate": pytest_version(args.candidate_python),
    }
    if versions != {"stock": "9.1.1", "candidate": "9.1.1"}:
        raise RuntimeError(f"installed pytest versions differ from the pinned pair: {versions}")

    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    observations = {
        "stock": run_case(destination, args.stock_python, candidate=False),
        "candidate": run_case(destination, args.candidate_python, candidate=True),
    }
    candidate_comparable = dict(observations["candidate"])
    if candidate_comparable.pop("failure_detail_complete", None) is not True:
        raise RuntimeError("native doctest failure detail was not validated")
    if candidate_comparable != observations["stock"]:
        raise RuntimeError(f"native vfy doctest behavior differs from stock: {observations}")

    report = destination / "vfy-doctest-boundary.json"
    report.write_text(
        json.dumps(
            {
                "pytest_versions": versions,
                "expected_outcomes": EXPECTED_OUTCOMES,
                "observations": observations,
                "ordered_phase_coverage_and_history_match_stock": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(f"{report}\n")


if __name__ == "__main__":
    main()
