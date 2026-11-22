"""Compare native --maxfail/-x behavior with installed pytest 9.1.1."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support import VfyProject

PROBE = Path(__file__).resolve().parent
TRACE = "PYTEST_NATIVE_MAXFAIL_TRACE"
COMMON = f'''from pathlib import Path
import os

def record(value):
    with Path(os.environ["{TRACE}"]).open("a", encoding="utf-8") as stream:
        stream.write(value + "\\n")
'''


@dataclass(frozen=True)
class Scenario:
    name: str
    files: dict[str, str]
    arguments: tuple[str, ...]
    expected_trace: tuple[str, ...]
    expected_phases: tuple[tuple[str, str, str], ...]
    collected: int
    collection_errors: int = 0


SCENARIOS = (
    Scenario(
        "call_failure",
        {
            "test_call.py": COMMON
            + """\ndef test_failure():
    record("call")
    assert False, "call failure"

def test_later():
    record("later")
""",
        },
        ("--maxfail=1", "test_call.py"),
        ("call",),
        (
            ("test_call.py::test_failure", "setup", "passed"),
            ("test_call.py::test_failure", "call", "failed"),
            ("test_call.py::test_failure", "teardown", "passed"),
        ),
        2,
    ),
    Scenario(
        "setup_failure",
        {
            "test_setup.py": COMMON
            + """\nimport pytest

@pytest.fixture
def failing_setup():
    record("setup")
    raise RuntimeError("setup failure")

def test_failure(failing_setup):
    record("call")

def test_later():
    record("later")
""",
        },
        ("-x", "test_setup.py"),
        ("setup",),
        (
            ("test_setup.py::test_failure", "setup", "error"),
            ("test_setup.py::test_failure", "teardown", "passed"),
        ),
        2,
    ),
    Scenario(
        "teardown_failure",
        {
            "test_teardown.py": COMMON
            + """\nimport pytest

@pytest.fixture
def failing_teardown():
    yield
    record("teardown")
    raise RuntimeError("teardown failure")

def test_failure(failing_teardown):
    record("call")

def test_later():
    record("later")
""",
        },
        ("--maxfail=1", "test_teardown.py"),
        ("call", "teardown"),
        (
            ("test_teardown.py::test_failure", "setup", "passed"),
            ("test_teardown.py::test_failure", "call", "passed"),
            ("test_teardown.py::test_failure", "teardown", "error"),
        ),
        2,
    ),
    Scenario(
        "call_and_teardown_count_separately",
        {
            "test_phases.py": COMMON
            + """\nimport pytest

@pytest.fixture
def failing_teardown():
    yield
    record("teardown")
    raise RuntimeError("teardown failure")

def test_failure(failing_teardown):
    record("call")
    assert False, "call failure"

def test_later():
    record("later")
""",
        },
        ("--maxfail=2", "test_phases.py"),
        ("call", "teardown"),
        (
            ("test_phases.py::test_failure", "setup", "passed"),
            ("test_phases.py::test_failure", "call", "failed"),
            ("test_phases.py::test_failure", "teardown", "error"),
        ),
        2,
    ),
    Scenario(
        "final_teardown_after_call_failure",
        {
            "test_finalizer.py": COMMON
            + """\nimport pytest

@pytest.fixture(scope="session")
def failing_finalizer():
    yield
    record("finalizer")
    raise RuntimeError("session finalizer failure")

def test_failure(failing_finalizer):
    record("call")
    assert False, "call failure"

def test_later(failing_finalizer):
    record("later")
""",
        },
        ("--maxfail=1", "test_finalizer.py"),
        ("call", "finalizer"),
        (
            ("test_finalizer.py::test_failure", "setup", "passed"),
            ("test_finalizer.py::test_failure", "call", "failed"),
            ("test_finalizer.py::test_failure", "teardown", "error"),
        ),
        2,
    ),
    Scenario(
        "collection_error_reaches_limit",
        {
            "test_good.py": COMMON
            + """\ndef test_first_collected():
    record("first")

def test_not_run():
    record("later")
""",
            "test_broken.py": "import missing_native_maxfail_module\n",
        },
        (
            "--continue-on-collection-errors",
            "--maxfail=1",
            "test_good.py",
            "test_broken.py",
        ),
        ("first",),
        (
            ("test_good.py::test_first_collected", "setup", "passed"),
            ("test_good.py::test_first_collected", "call", "passed"),
            ("test_good.py::test_first_collected", "teardown", "passed"),
        ),
        2,
        collection_errors=1,
    ),
    Scenario(
        "collection_error_without_tests",
        {"test_broken.py": "import missing_native_maxfail_module\n"},
        ("--continue-on-collection-errors", "--maxfail=1", "test_broken.py"),
        (),
        (),
        0,
        collection_errors=1,
    ),
    Scenario(
        "collection_error_plus_test_failure",
        {
            "test_good.py": COMMON
            + """\ndef test_failure():
    record("call")
    assert False, "call failure"

def test_not_run():
    record("later")
""",
            "test_broken.py": "import missing_native_maxfail_module\n",
        },
        (
            "--continue-on-collection-errors",
            "--maxfail=2",
            "test_good.py",
            "test_broken.py",
        ),
        ("call",),
        (
            ("test_good.py::test_failure", "setup", "passed"),
            ("test_good.py::test_failure", "call", "failed"),
            ("test_good.py::test_failure", "teardown", "passed"),
        ),
        2,
        collection_errors=1,
    ),
    Scenario(
        "zero_keeps_running",
        {
            "test_zero.py": COMMON
            + """\ndef test_failure():
    record("call")
    assert False, "call failure"

def test_later():
    record("later")
""",
        },
        ("--maxfail=0", "test_zero.py"),
        ("call", "later"),
        (
            ("test_zero.py::test_failure", "setup", "passed"),
            ("test_zero.py::test_failure", "call", "failed"),
            ("test_zero.py::test_failure", "teardown", "passed"),
            ("test_zero.py::test_later", "setup", "passed"),
            ("test_zero.py::test_later", "call", "passed"),
            ("test_zero.py::test_later", "teardown", "passed"),
        ),
        2,
    ),
)


class PhaseObservation(TypedDict):
    nodeid: str
    phase: str
    outcome: str


class RunObservation(TypedDict):
    exit_status: int
    trace: list[str]
    phases: list[PhaseObservation]
    collected: int
    collection_errors: int
    summary: str
    stop_reason: str


def _phase_observations(records: object) -> list[PhaseObservation]:
    if not isinstance(records, list):
        raise RuntimeError("phase records are not an array")
    phases: list[PhaseObservation] = []
    for record in records:
        if not isinstance(record, dict):
            raise RuntimeError("phase record is not an object")
        nodeid = record.get("nodeid")
        phase = record.get("phase")
        outcome = record.get("outcome")
        if (
            not isinstance(nodeid, str)
            or not isinstance(phase, str)
            or not isinstance(outcome, str)
        ):
            raise RuntimeError("phase record is missing its string identity or outcome")
        phases.append({"nodeid": nodeid, "phase": phase, "outcome": outcome})
    return phases


def _pytest_version(python: Path, cwd: Path) -> str:
    completed = subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata; print(importlib.metadata.version('pytest'))",
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _run(
    root: Path,
    python: Path,
    scenario: Scenario,
    engine: str,
) -> RunObservation:
    project = VfyProject(root)
    for name, source in scenario.files.items():
        project.write(name, source)
    trace_path = root.parent / f"{root.name}-trace.txt"
    trace_path.unlink(missing_ok=True)
    phases_path = root.parent / f"{root.name}-phases.jsonl"
    phases_path.unlink(missing_ok=True)
    environment = {TRACE: str(trace_path)}
    command = [
        str(python.absolute()),
        "-m",
        "pytest",
        "-q",
        "--tb=no",
        "--color=no",
        "-c",
        os.devnull,
        "--rootdir",
        str(root),
        "-p",
        "no:cacheprovider",
    ]
    if engine == "stock":
        environment.update(PYTHONPATH=str(PROBE), PYTEST_STOCK_MAXFAIL_PHASES=str(phases_path))
        command.extend(("-p", "stock_maxfail_probe"))
    else:
        environment["PYTEST_NATIVE_RESULT"] = str(phases_path.with_suffix(".json"))
        command.extend(("-p", "vfy._pytest.native_pytest"))
    command.extend(scenario.arguments)
    result = project.run(*command, env=environment)
    output = result.stdout + result.stderr
    summary = next(
        (
            re.sub(r"\s+in\s+[0-9.]+s\s*$", "", line.strip())
            for line in reversed(output.splitlines())
            if re.search(r"\bin\s+[0-9.]+s\s*$", line.strip())
        ),
        "",
    )
    stop_reason = next(
        (line.strip() for line in output.splitlines() if "stopping after " in line),
        "",
    )
    if engine == "stock":
        records = [json.loads(line) for line in phases_path.read_text().splitlines()]
        phase_records = [record for record in records if record["phase"] != "collect"]
        phases = _phase_observations(phase_records)
        collection_errors = sum(record["phase"] == "collect" for record in records)
        collected = scenario.collected
    else:
        record = json.loads(phases_path.with_suffix(".json").read_text())
        if record["engine"] != "pytest-runner-native-v1":
            raise RuntimeError(f"native runner did not activate: {record}")
        phases = _phase_observations(
            [
                {"nodeid": case["nodeid"], "phase": phase["phase"], "outcome": phase["outcome"]}
                for case in record["cases"]
                for phase in case["phases"]
            ]
        )
        collection_errors = scenario.collection_errors
        collected = record["collected"]
    return {
        "exit_status": result.returncode,
        "trace": trace_path.read_text().splitlines() if trace_path.exists() else [],
        "phases": phases,
        "collected": collected,
        "collection_errors": collection_errors,
        "summary": summary,
        "stop_reason": stop_reason,
    }


def _check_collection_interrupt(destination: Path, interpreters: dict[str, Path]) -> None:
    observations = {}
    for engine, python in interpreters.items():
        root = destination / "collection_interrupt" / engine
        project = VfyProject(root)
        project.write("test_good.py", COMMON + 'def test_never_runs():\n    record("run")\n')
        project.write("test_broken.py", "import missing_native_maxfail_module\n")
        trace = root.parent / f"{engine}-trace.txt"
        trace.unlink(missing_ok=True)
        phases = root.parent / f"{engine}-phases.jsonl"
        phases.unlink(missing_ok=True)
        environment = {TRACE: str(trace)}
        command = [
            str(python),
            "-m",
            "pytest",
            "-q",
            "--tb=no",
            "--color=no",
            "-c",
            os.devnull,
            "--rootdir",
            str(root),
            "-p",
            "no:cacheprovider",
        ]
        if engine == "stock":
            environment.update(
                PYTHONPATH=str(PROBE),
                PYTEST_STOCK_MAXFAIL_PHASES=str(phases),
            )
            command.extend(("-p", "stock_maxfail_probe"))
        else:
            command.extend(("-p", "vfy._pytest.native_pytest"))
        command.extend(("--maxfail=1", "test_good.py", "test_broken.py"))
        result = project.run(*command, env=environment)
        output = result.stdout + result.stderr
        interrupted = re.search(r"Interrupted: (\d+) errors? during collection", output)
        observations[engine] = {
            "exit_status": result.returncode,
            "trace": trace.read_text().splitlines() if trace.exists() else [],
            "collection_errors": int(interrupted.group(1)) if interrupted else 0,
            "interrupted_during_collection": interrupted is not None,
        }
    if observations["stock"] != observations["candidate"]:
        raise RuntimeError(f"collection error interruption parity failed: {observations}")
    if observations["candidate"] != {
        "exit_status": 2,
        "trace": [],
        "collection_errors": 1,
        "interrupted_during_collection": True,
    }:
        raise RuntimeError(f"unexpected collection error behavior: {observations}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-python", required=True, type=Path)
    parser.add_argument("--candidate-python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    versions = {
        "stock": _pytest_version(args.stock_python.absolute(), destination),
        "candidate": _pytest_version(args.candidate_python.absolute(), destination),
    }
    if versions != {"stock": "9.1.1", "candidate": "9.1.1"}:
        raise RuntimeError(f"maxfail differential requires pinned installed wheels: {versions}")

    observations: dict[str, dict[str, RunObservation]] = {}
    for scenario in SCENARIOS:
        by_engine = {}
        for engine, python in (
            ("stock", args.stock_python),
            ("candidate", args.candidate_python),
        ):
            by_engine[engine] = _run(
                destination / scenario.name / engine,
                python,
                scenario,
                engine,
            )
        observations[scenario.name] = by_engine
        if by_engine["stock"] != by_engine["candidate"]:
            mismatch = destination / f"{scenario.name}-mismatch.json"
            mismatch.write_text(json.dumps(by_engine, indent=2) + "\n", encoding="utf-8")
            raise RuntimeError(f"installed --maxfail behavior differs: {mismatch}")
        candidate = by_engine["candidate"]
        if candidate["collected"] != scenario.collected:
            raise RuntimeError(
                f"{scenario.name}: expected {scenario.collected} collected cases, "
                f"got {candidate['collected']}"
            )
        if candidate["trace"] != list(scenario.expected_trace):
            raise RuntimeError(
                f"{scenario.name}: expected trace {scenario.expected_trace}, "
                f"got {candidate['trace']}"
            )
        actual_phases = [
            (phase["nodeid"], phase["phase"], phase["outcome"]) for phase in candidate["phases"]
        ]
        if actual_phases != list(scenario.expected_phases):
            raise RuntimeError(f"{scenario.name}: unexpected executed phases {actual_phases}")
        expected_stop = scenario.name != "zero_keeps_running"
        if bool(candidate["stop_reason"]) != expected_stop:
            raise RuntimeError(
                f"{scenario.name}: unexpected maxfail stop message {candidate['stop_reason']!r}"
            )

    _check_collection_interrupt(
        destination,
        {"stock": args.stock_python.absolute(), "candidate": args.candidate_python.absolute()},
    )
    record = {
        "reference": "pytest==9.1.1",
        "candidate": versions["candidate"],
        "parity": True,
        "cases": observations,
        "collection_without_continue": "matched exit status 2 and skipped test execution",
    }
    output = destination / "maxfail-differential.json"
    output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(
        f"native --maxfail/-x matches pytest 9.1.1 across {len(SCENARIOS)} cases: {output}\n"
    )


if __name__ == "__main__":
    main()
