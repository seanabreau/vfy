"""Compare warning scopes and filter precedence through both pytest CLIs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support import VfyProject

PROBE = Path(__file__).resolve().parent
SOURCE = """\
import warnings

import pytest


@pytest.fixture
def warning_fixture():
    warnings.warn("setup sentinel", UserWarning)
    yield
    warnings.warn("teardown sentinel", UserWarning)


@pytest.mark.filterwarnings("ignore:.* sentinel:UserWarning")
def test_marker_covers_all_phases(warning_fixture):
    warnings.warn("call sentinel", UserWarning)


def test_default_deprecation():
    warnings.warn("default sentinel", DeprecationWarning)


def test_marker_filter_restored():
    warnings.warn("restore sentinel", UserWarning)


def test_config_and_cli_precedence():
    warnings.warn("config sentinel", UserWarning)
"""
SENTINELS = (
    "setup sentinel",
    "call sentinel",
    "teardown sentinel",
    "default sentinel",
    "restore sentinel",
    "config sentinel",
)
EXIT_SOURCE = """\
import os
import warnings
from pathlib import Path

import pytest


@pytest.fixture
def exit_fixture():
    yield
    Path(os.environ["PYTEST_CONTROL_TEARDOWN"]).write_text("torn down\\n")


@pytest.mark.filterwarnings("error::UserWarning")
def test_controlled_exit_restores_warnings(exit_fixture):
    with pytest.raises(UserWarning):
        warnings.warn("exit warning sentinel", UserWarning)
    pytest.exit("controlled warning exit", returncode=7)
"""
DYNAMIC_SOURCE = """\
import warnings


def test_00_change_warning_config(request):
    request.config.addinivalue_line("filterwarnings", "ignore:dynamic sentinel:UserWarning")


def test_01_uses_changed_warning_config():
    warnings.warn("dynamic sentinel", UserWarning)
"""


def _phases(path: Path, *, stock: bool) -> list[dict[str, str]]:
    if stock:
        return [json.loads(line) for line in path.read_text().splitlines()]
    record = json.loads(path.read_text())
    if record["engine"] != "pytest-runner-native-v1":
        raise RuntimeError("candidate did not activate the native engine")
    return [
        {
            "nodeid": case["nodeid"],
            **{key: phase[key] for key in ("phase", "outcome", "stdout", "stderr")},
        }
        for case in record["cases"]
        for phase in case["phases"]
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-python", required=True, type=Path)
    parser.add_argument("--candidate-python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    project = VfyProject(output / "project")
    project.write("test_warnings.py", SOURCE)
    project.write("test_dynamic_warnings.py", DYNAMIC_SOURCE)
    report = []
    modes = {
        "defaults": ((), 0, "test_warnings.py"),
        "precedence": (
            ("-o", "filterwarnings=ignore::UserWarning", "-W", "error::UserWarning"),
            1,
            "test_warnings.py",
        ),
        "disabled": (("-p", "no:warnings", "-W", "error::UserWarning"), 0, "test_warnings.py"),
        "dynamic_config": ((), 0, "test_dynamic_warnings.py"),
    }
    for mode, (options, expected_exit, source_file) in modes.items():
        observations: dict[str, dict[str, object]] = {}
        for name, python in (
            ("stock", args.stock_python.absolute()),
            ("candidate", args.candidate_python.absolute()),
        ):
            phase_path = output / f"{mode}-{name}-phases.json"
            phase_path.unlink(missing_ok=True)
            env = {"PYTHONPATH": str(PROBE)}
            if name == "stock":
                env["PYTEST_STOCK_PHASE_LOG"] = str(phase_path)
            else:
                env["PYTEST_NATIVE_RESULT"] = str(phase_path)
            command = [
                str(python),
                "-m",
                "pytest",
                "-q",
                "--tb=no",
                "-c",
                "/dev/null",
                "--rootdir",
                str(project.root),
                "-p",
                "no:cacheprovider",
                *options,
            ]
            if name == "stock":
                command.extend(("-p", "stock_phase_probe"))
            else:
                command.extend(("-p", "vfy._pytest.native_pytest"))
            command.append(source_file)
            completed = project.run(*command, env=env)
            if completed.returncode != expected_exit:
                raise RuntimeError(
                    f"{mode}/{name}: exit {completed.returncode}, expected {expected_exit}\n"
                    f"{completed.stdout}\n{completed.stderr}"
                )
            observations[name] = {
                "phases": _phases(phase_path, stock=name == "stock"),
                "warning_mentions": {
                    sentinel: (
                        completed.stdout.count(sentinel),
                        completed.stderr.count(sentinel),
                    )
                    for sentinel in SENTINELS
                },
            }
            if mode == "dynamic_config":
                observations[name]["dynamic_warning_mentioned"] = "dynamic sentinel" in (
                    completed.stdout + completed.stderr
                )
        if observations["stock"] != observations["candidate"]:
            mismatch = output / f"{mode}-mismatch.json"
            mismatch.write_text(json.dumps(observations, indent=2) + "\n")
            raise RuntimeError(f"warning parity failed: {mismatch}")
        if mode == "defaults":
            warning_mentions = observations["stock"].get("warning_mentions")
            if not isinstance(warning_mentions, dict):
                raise RuntimeError("warning observation is missing per-stream mention counts")
            default_mentions = warning_mentions.get("default sentinel")
            if (
                not isinstance(default_mentions, tuple)
                or not default_mentions
                or not isinstance(default_mentions[0], int)
                or default_mentions[0] == 0
            ):
                raise RuntimeError("stock pytest did not display the default deprecation warning")
        report.append(
            {"mode": mode, "exit_status": expected_exit, "phase_and_warning_parity": True}
        )
    project.write("test_exit_warning.py", EXIT_SOURCE)
    exit_program = (
        "import pytest,sys,warnings; "
        "filters=warnings.filters; stdout=sys.stdout; stderr=sys.stderr; "
        "code=pytest.main(sys.argv[1:]); "
        "assert code == 7 and warnings.filters is filters; "
        "assert sys.stdout is stdout and sys.stderr is stderr; "
        "print('warning scope and streams restored'); raise SystemExit(code)"
    )
    for name, python in (
        ("stock", args.stock_python.absolute()),
        ("candidate", args.candidate_python.absolute()),
    ):
        teardown = output / f"{name}-warning-exit-teardown.txt"
        teardown.unlink(missing_ok=True)
        exit_args = [
            "-q",
            "-c",
            "/dev/null",
            "--rootdir",
            str(project.root),
            "-p",
            "no:cacheprovider",
        ]
        if name == "candidate":
            exit_args.extend(("-p", "vfy._pytest.native_pytest"))
        exit_args.append("test_exit_warning.py")
        completed = project.run(
            str(python),
            "-c",
            exit_program,
            *exit_args,
            env={"PYTHONPATH": str(PROBE), "PYTEST_CONTROL_TEARDOWN": str(teardown)},
        )
        if (
            completed.returncode != 7
            or not teardown.is_file()
            or teardown.read_text() != "torn down\n"
            or "warning scope and streams restored" not in completed.stdout
        ):
            raise RuntimeError(
                f"{name}: warning scope or streams not restored after controlled exit\n"
                f"{completed.stdout}\n{completed.stderr}"
            )
    report.append({"controlled_exit_status": 7, "warning_scope_and_streams_restored": True})
    destination = output / "warnings-contract.json"
    destination.write_text(json.dumps(report, indent=2) + "\n")
    sys.stdout.write(f"{destination}\n")


if __name__ == "__main__":
    main()
