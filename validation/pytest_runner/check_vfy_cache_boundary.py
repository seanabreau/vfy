"""Compare pytest's cache API through installed stock and native vfy commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support import VfyProject
from validation.pytest_runner.check_vfy_coverage_boundary import (
    CUSTOM_ITEM_CONFTEST,
    assert_run_evidence,
    normalized_coverage,
    normalized_history_tests,
    run_test,
    run_unsupported_case,
)
from validation.pytest_runner.upstream_pytest import pytest_version

CACHE_OUTCOMES = {
    "test_contract.py::test_cache_fixture": "passed",
    "test_contract.py::test_pytestconfig_cache": "passed",
    "test_contract.py::test_request_config_cache": "passed",
    "test_contract.py::test_cache_access_from_fixture": "passed",
}
CACHE_SENTINEL = b"preserve-cache-value\n"


def cache_test_source(expected_run: int | None, current_run: int) -> str:
    return f"""import os
from pathlib import Path

import pytest

from src.sample import measured


def record(name):
    event = Path(os.environ["VFY_NATIVE_BOUNDARY_EVENT"])
    with event.open("a", encoding="utf-8") as stream:
        stream.write(name + "\\n")


def test_cache_fixture(cache, pytestconfig):
    assert cache is pytestconfig.cache
    assert cache.get("vfy/bridge-fixture", None) == {expected_run!r}
    cache.set("vfy/bridge-fixture", {current_run})
    assert cache.mkdir("vfy-bridge-dir").is_dir()
    assert measured(True) == 42
    record("fixture")


def test_pytestconfig_cache(pytestconfig, request):
    assert pytestconfig.cache is request.config.cache
    assert pytestconfig.cache.get("vfy/bridge-pytestconfig", None) == {expected_run!r}
    pytestconfig.cache.set("vfy/bridge-pytestconfig", {current_run})
    assert measured(True) == 42
    record("pytestconfig")


def test_request_config_cache(request, cache):
    assert request.config.cache is cache
    assert request.config.cache.get("vfy/bridge-request", None) == {expected_run!r}
    request.config.cache.set("vfy/bridge-request", {current_run})
    assert measured(True) == 42
    record("request")


@pytest.fixture
def cached_value(request):
    value = request.config.cache.get("vfy/bridge-fixture-function", None)
    request.config.cache.set("vfy/bridge-fixture-function", {current_run})
    return value


def test_cache_access_from_fixture(cached_value):
    assert cached_value == {expected_run!r}
    assert measured(True) == 42
    record("fixture-function")
"""


def assert_two_run_history(project: VfyProject) -> dict[str, object]:
    tests = normalized_history_tests(project)
    if set(tests) != set(CACHE_OUTCOMES):
        raise RuntimeError(
            "cache-service history has extra or missing tests: "
            f"expected={sorted(CACHE_OUTCOMES)}, actual={sorted(tests)}"
        )
    run_ids_by_position: list[set[object]] = [set(), set()]
    for nodeid, expected_outcome in CACHE_OUTCOMES.items():
        entry = tests[nodeid][1]
        observations = entry.get("observations") if isinstance(entry, dict) else None
        if not isinstance(observations, list) or len(observations) != 2:
            raise RuntimeError(
                f"cache-service history does not contain exactly two runs for {nodeid}: {entry}"
            )
        for position, observation in enumerate(observations):
            if not isinstance(observation, dict) or (
                observation.get("attempt"),
                observation.get("outcome"),
            ) != ("primary", expected_outcome):
                raise RuntimeError(
                    f"cache-service history has an invalid observation for {nodeid}: {observation}"
                )
            run_id = observation.get("run_id")
            if run_id is None:
                raise RuntimeError(f"cache-service history omitted a run ID for {nodeid}")
            run_ids_by_position[position].add(run_id)
    if any(len(run_ids) != 1 for run_ids in run_ids_by_position):
        raise RuntimeError(
            "cache-service history does not group each execution under one run ID: "
            f"{run_ids_by_position}"
        )
    ordered_run_ids = [next(iter(run_ids)) for run_ids in run_ids_by_position]
    if ordered_run_ids[0] == ordered_run_ids[1]:
        raise RuntimeError("cache-service history reused a run ID across executions")
    return {
        "nodeids": list(CACHE_OUTCOMES),
        "observation_counts": [2] * len(CACHE_OUTCOMES),
        "distinct_run_count": 2,
    }


def check_cache_service(
    destination: Path, stock_python: Path, candidate_python: Path
) -> dict[str, object]:
    runs: dict[str, dict[str, object]] = {}
    expected_events = ["fixture", "pytestconfig", "request", "fixture-function"]
    cache_keys = (
        "bridge-fixture",
        "bridge-pytestconfig",
        "bridge-request",
        "bridge-fixture-function",
    )
    for name, python, candidate in (
        ("stock", stock_python, False),
        ("candidate", candidate_python, True),
    ):
        project = VfyProject(destination / f"cache-service-{name}")
        run_observations: list[dict[str, object]] = []
        for current_run, expected_run in ((1, None), (2, 1)):
            event = destination / f"cache-service-{name}-run-{current_run}-events.txt"
            native_result = destination / f"cache-service-{name}-run-{current_run}-result.json"
            phase_log = destination / f"cache-service-{name}-run-{current_run}-phases.jsonl"
            for artifact in (event, native_result, phase_log):
                artifact.unlink(missing_ok=True)
            status, stdout, stderr = run_test(
                project,
                python,
                event,
                native_result,
                tests=cache_test_source(expected_run, current_run),
                native_bridge=candidate,
                engine="pytest-native" if candidate else "pytest",
                extra_arguments=(
                    "-c",
                    os.devnull,
                    "--rootdir",
                    str(project.root),
                    "-o",
                    "cache_dir=.pytest_cache",
                ),
                phase_log=None if candidate else phase_log,
            )
            if status != 0:
                raise RuntimeError(
                    f"{name} cache-service run {current_run} failed: {status}\n{stdout}\n{stderr}"
                )
            events = event.read_text(encoding="utf-8").splitlines() if event.is_file() else []
            if events != expected_events:
                raise RuntimeError(
                    f"{name} cache-service run {current_run} has wrong events: {events}"
                )
            phases = assert_run_evidence(
                project,
                native_result,
                CACHE_OUTCOMES,
                candidate=candidate,
                phase_log=None if candidate else phase_log,
            )
            run_observations.append(
                {
                    "exit_status": status,
                    "events": events,
                    "coverage": normalized_coverage(project.root / ".coverage", project.root),
                    "phases": phases,
                }
            )
        cache_values = {
            key: json.loads((project.root / ".pytest_cache/v/vfy" / key).read_text())
            for key in cache_keys
        }
        if set(cache_values.values()) != {2}:
            raise RuntimeError(f"{name} cache values did not persist across runs: {cache_values}")
        if not (project.root / ".pytest_cache/d/vfy-bridge-dir").is_dir():
            raise RuntimeError(f"{name} cache mkdir path was not preserved")
        runs[name] = {
            "runs": run_observations,
            "cache_values": cache_values,
            "cache_directory_created": True,
            "history": assert_two_run_history(project),
        }
    if runs["stock"] != runs["candidate"]:
        raise RuntimeError(f"native vfy cache behavior differs from stock: {runs}")
    return {
        "runs": runs,
        "cache_fixture_supported": True,
        "config_cache_supported": True,
        "persistence_verified_across_runs": True,
        "coverage_and_history_match_stock": True,
    }


def check_rejected_cache_preservation(
    destination: Path, candidate_python: Path
) -> dict[str, object]:
    project_root = destination / "rejected-cache-suite"
    coverage_path = destination / "cache-service-stock/.coverage"
    if not coverage_path.is_file():
        raise RuntimeError("stock cache run did not publish the coverage seed")
    return run_unsupported_case(
        destination,
        candidate_python,
        coverage_path.read_bytes(),
        project_root.name,
        """import os
from pathlib import Path


def test_custom_item_suite_rejected(cache):
    cache.set("vfy/rejected-preserved", "changed")
    Path(os.environ["VFY_NATIVE_BOUNDARY_EVENT"]).write_text("ran\\n")
""",
        cache_seed=CACHE_SENTINEL,
        conftest=CUSTOM_ITEM_CONFTEST,
        extra_arguments=(
            "-c",
            os.devnull,
            "--rootdir",
            str(project_root),
            "-o",
            "cache_dir=.pytest_cache",
        ),
    )


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
    report = destination / "vfy-cache-boundary.json"
    report.write_text(
        json.dumps(
            {
                "pytest_versions": versions,
                "cache_service": check_cache_service(
                    destination, args.stock_python, args.candidate_python
                ),
                "rejected_suite": check_rejected_cache_preservation(
                    destination, args.candidate_python
                ),
                "unsupported": {
                    "provider_state": ["lastfailed", "nodeids", "stepwise"],
                    "options": [
                        "--lf",
                        "--ff",
                        "--nf",
                        "--sw",
                        "--cache-clear",
                        "--cache-show",
                    ],
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(f"{report}\n")


if __name__ == "__main__":
    main()
