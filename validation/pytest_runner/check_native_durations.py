"""Compare pytest duration reporting through installed vfy commands."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support import VfyProject
from validation.pytest_runner.check_vfy_coverage_boundary import command
from validation.pytest_runner.upstream_pytest import pytest_version

TEST_SOURCE = """import time


def test_slowest():
    time.sleep(0.12)


def test_second_slowest():
    time.sleep(0.06)
"""
DURATION_LINE = re.compile(
    r"^(?P<duration>[0-9]+\.[0-9]{2})s (?P<phase>setup|call|teardown)\s+(?P<nodeid>.+)$"
)
EXPECTED_SLOWEST = [
    ("call", "test_slow.py::test_slowest"),
    ("call", "test_slow.py::test_second_slowest"),
]


def run_case(
    destination: Path,
    python: Path,
    *,
    candidate: bool,
    arguments: tuple[str, ...],
    scenario: str,
) -> dict[str, object]:
    name = "candidate" if candidate else "stock"
    project = VfyProject(destination / f"{scenario}-{name}")
    project.write("src/__init__.py", "")
    project.write("test_slow.py", TEST_SOURCE)
    event = destination / f"{scenario}-{name}-unused-event.txt"
    native_result = destination / f"{scenario}-{name}-native-result.json"
    event.unlink(missing_ok=True)
    native_result.unlink(missing_ok=True)
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
            "--no-cov",
            "-c",
            os.devnull,
            "--rootdir",
            str(project.root),
            *arguments,
            "test_slow.py",
        ),
        event=event,
        native_result=native_result,
        native_bridge=candidate,
    )
    if status != 0:
        raise RuntimeError(f"{scenario} {name} run returned {status}:\n{stdout}\n{stderr}")
    if candidate:
        record = json.loads(native_result.read_text(encoding="utf-8"))
        if record.get("engine") != "pytest-runner-native-v1" or record.get("collected") != 2:
            raise RuntimeError(f"{scenario} did not execute through the native loop: {record}")
    elif native_result.exists():
        raise RuntimeError(f"{scenario} stock run wrote a native result")
    rows = []
    for line in stdout.splitlines():
        match = DURATION_LINE.fullmatch(line.strip())
        if match is not None:
            rows.append((match.group("phase"), match.group("nodeid")))
    hidden = [line.strip() for line in stdout.splitlines() if " durations < " in line]
    headers = [
        line.strip("= ")
        for line in stdout.splitlines()
        if line.startswith("=") and line.endswith("=") and "slowest" in line
    ]
    return {
        "duration_rows": rows,
        "hidden_messages": hidden,
        "headers": headers,
        "exit_status": status,
    }


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
    scenarios = {
        "slowest_two": ("--durations=2", "--durations-min=0"),
        "hidden": ("--durations=0", "--durations-min=10"),
    }
    observations: dict[str, dict[str, dict[str, object]]] = {}
    for scenario, arguments in scenarios.items():
        runs = {
            "stock": run_case(
                destination,
                args.stock_python,
                candidate=False,
                arguments=arguments,
                scenario=scenario,
            ),
            "candidate": run_case(
                destination,
                args.candidate_python,
                candidate=True,
                arguments=arguments,
                scenario=scenario,
            ),
        }
        if runs["candidate"] != runs["stock"]:
            raise RuntimeError(f"{scenario} native duration output differs from stock: {runs}")
        observations[scenario] = runs

    slowest = observations["slowest_two"]["candidate"]
    if slowest["duration_rows"] != EXPECTED_SLOWEST or slowest["headers"] != [
        "slowest 2 durations"
    ]:
        raise RuntimeError(f"native slowest-duration ordering is invalid: {slowest}")
    hidden = observations["hidden"]["candidate"]
    if (
        hidden["duration_rows"]
        or hidden["hidden_messages"] != ["(6 durations < 10s hidden.)"]
        or hidden["headers"] != ["slowest durations"]
    ):
        raise RuntimeError(f"native hidden-duration summary is invalid: {hidden}")

    report = destination / "native-durations.json"
    report.write_text(
        json.dumps(
            {
                "pytest_versions": versions,
                "observations": observations,
                "phase_order_and_hidden_count_match_stock": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(f"{report}\n")


if __name__ == "__main__":
    main()
