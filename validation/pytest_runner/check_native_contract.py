"""Compare installed engines through fresh CLI and pytest.main processes.

The oracle records ordered phase outcomes and phase-local stdout/stderr.
Fixtures independently record their observable setup and finalizer event order.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmarks.benchmark_harness import benchmark_environment
from scripts.process_tree import run_captured

ROOT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).resolve().parent


class EntryObservation(TypedDict):
    phases: list[dict[str, str]]
    events: list[str]
    uncaptured_stdout: dict[str, int]
    uncaptured_stderr: int


def _check_runxfail(destination: Path, interpreters: dict[str, Path]) -> int:
    phases_by_engine = {}
    for name, python in interpreters.items():
        phase_path = destination / f"runxfail-{name}-phases.json"
        phase_path.unlink(missing_ok=True)
        environment = benchmark_environment({"PYTHONPATH": str(PROBE)})
        if name == "stock":
            environment["PYTEST_STOCK_PHASE_LOG"] = str(phase_path)
        else:
            environment["PYTEST_NATIVE_RESULT"] = str(phase_path)
        pytest_args = [
            "-q",
            "--tb=no",
            "-c",
            "/dev/null",
            "--rootdir",
            str(ROOT),
            "-p",
            "no:cacheprovider",
            "--runxfail",
            "-k",
            "imperative_xfail or xpass or test_xfail",
        ]
        if name == "stock":
            pytest_args.extend(("-p", "stock_phase_probe"))
        else:
            pytest_args.extend(("-p", "vfy._pytest.native_pytest"))
        pytest_args.extend(
            (
                str(PROBE / "compat_cases" / "test_outcomes.py"),
                str(PROBE / "compat_cases" / "test_semantics.py"),
            )
        )
        completed = run_captured(
            [str(python), "-m", "pytest", *pytest_args],
            cwd=ROOT,
            env=environment,
            timeout=120,
        )
        if completed.returncode != 1:
            raise RuntimeError(
                f"runxfail/{name}: expected marked failure exit 1, "
                f"got {completed.returncode}\n{completed.stdout}\n{completed.stderr}"
            )
        if name == "stock":
            phases = [json.loads(line) for line in phase_path.read_text().splitlines()]
        else:
            record = json.loads(phase_path.read_text())
            if record["engine"] != "pytest-runner-native-v1":
                raise RuntimeError("candidate did not activate the native engine")
            phases = [
                {
                    "nodeid": case["nodeid"],
                    **{key: phase[key] for key in ("phase", "outcome", "stdout", "stderr")},
                }
                for case in record["cases"]
                for phase in case["phases"]
            ]
        phases_by_engine[name] = phases
    if phases_by_engine["stock"] != phases_by_engine["candidate"]:
        mismatch = destination / "runxfail-mismatch.json"
        mismatch.write_text(json.dumps(phases_by_engine, indent=2) + "\n")
        raise RuntimeError(f"native --runxfail phase parity failed: {mismatch}")
    return len(phases_by_engine["stock"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-python", required=True, type=Path)
    parser.add_argument("--candidate-python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    interpreters = {
        "stock": args.stock_python.absolute(),
        "candidate": args.candidate_python.absolute(),
    }
    results = []
    for entry in ("cli", "main"):
        observations: dict[str, EntryObservation] = {}
        for name, python in interpreters.items():
            phase_path = destination / f"{entry}-{name}-phases.json"
            events_path = destination / f"{entry}-{name}-events.txt"
            phase_path.unlink(missing_ok=True)
            events_path.unlink(missing_ok=True)
            environment = benchmark_environment(
                {
                    "PYTHONPATH": str(PROBE),
                    "PYTEST_NATIVE_EVENT_LOG": str(events_path),
                }
            )
            pytest_args = [
                "-q",
                "--tb=no",
                "--capture=fd",
                "-c",
                "/dev/null",
                "--rootdir",
                str(ROOT),
                "-o",
                "xfail_strict=true",
                "-p",
                "no:cacheprovider",
            ]
            if name == "stock":
                environment["PYTEST_STOCK_PHASE_LOG"] = str(phase_path)
                pytest_args.extend(("-p", "stock_phase_probe"))
            else:
                environment["PYTEST_NATIVE_RESULT"] = str(phase_path)
                pytest_args.extend(("-p", "vfy._pytest.native_pytest"))
            pytest_args.extend(
                str(PROBE / "compat_cases" / filename)
                for filename in (
                    "test_semantics.py",
                    "test_scoped_parameters.py",
                    "test_outcomes.py",
                    "test_capture_boundaries.py",
                    "test_direct_parameter_specialization.py",
                    "test_compact_families.py",
                    "test_fixture_override_invalidation.py",
                )
            )
            invocation = (
                ["-m", "pytest"]
                if entry == "cli"
                else ["-c", "import pytest,sys; raise SystemExit(pytest.main(sys.argv[1:]))"]
            )
            completed = run_captured(
                [str(python), *invocation, *pytest_args],
                cwd=ROOT,
                env=environment,
                timeout=120,
            )
            if completed.returncode != 1:
                raise RuntimeError(
                    f"{entry}/{name}: expected controlled test failure exit 1, "
                    f"got {completed.returncode}\n{completed.stdout}\n{completed.stderr}"
                )
            if name == "stock":
                phases = [json.loads(line) for line in phase_path.read_text().splitlines()]
            else:
                record = json.loads(phase_path.read_text())
                if record["engine"] != "pytest-runner-native-v1":
                    raise RuntimeError("candidate did not activate the native engine")
                phases = [
                    {
                        "nodeid": case["nodeid"],
                        **{key: phase[key] for key in ("phase", "outcome", "stdout", "stderr")},
                    }
                    for case in record["cases"]
                    for phase in case["phases"]
                ]
            observations[name] = {
                "phases": phases,
                "events": events_path.read_text().splitlines(),
                "uncaptured_stdout": {
                    marker: completed.stdout.count(marker)
                    for marker in ("uncaptured capfd stdout", "uncaptured capsys stdout")
                },
                "uncaptured_stderr": completed.stderr.count("uncaptured capfd stderr"),
            }
        if observations["stock"] != observations["candidate"]:
            mismatch = destination / f"{entry}-mismatch.json"
            mismatch.write_text(json.dumps(observations, indent=2) + "\n")
            raise RuntimeError(f"native phase or event parity failed: {mismatch}")
        results.append(
            {
                "entry": entry,
                "phase_count": len(observations["stock"]["phases"]),
                "event_count": len(observations["stock"]["events"]),
                "ordered_phases_streams_and_events_equal": True,
            }
        )
    results.append(
        {"runxfail_phase_count": _check_runxfail(destination, interpreters), "runxfail_equal": True}
    )
    for name, python in interpreters.items():
        teardown_path = destination / f"{name}-exit-teardown.txt"
        teardown_path.unlink(missing_ok=True)
        environment = benchmark_environment({"PYTEST_CONTROL_TEARDOWN": str(teardown_path)})
        command = [
            str(python),
            "-c",
            "import pytest,sys; code=pytest.main(sys.argv[1:]); "
            "print('stream restored after exit'); raise SystemExit(code)",
            "-q",
            "-c",
            "/dev/null",
            "--rootdir",
            str(ROOT),
            "-p",
            "no:cacheprovider",
            str(PROBE / "compat_cases" / "test_exit_control.py"),
        ]
        if name == "candidate":
            command[-1:-1] = ["-p", "vfy._pytest.native_pytest"]
        completed = run_captured(
            command,
            cwd=ROOT,
            env=environment,
            timeout=120,
        )
        if (
            completed.returncode != 7
            or not teardown_path.is_file()
            or teardown_path.read_text() != "torn down\n"
            or "stream restored after exit" not in completed.stdout
        ):
            raise RuntimeError(
                f"{name}: controlled exit did not finalize fixtures and restore streams\n"
                f"{completed.stdout}\n{completed.stderr}"
            )
    results.append({"controlled_exit_status": 7, "teardown_and_stream_restoration_equal": True})
    report = destination / "contract.json"
    report.write_text(json.dumps(results, indent=2) + "\n")
    sys.stdout.write(f"{report}\n")


if __name__ == "__main__":
    main()
