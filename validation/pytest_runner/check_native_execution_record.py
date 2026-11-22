"""Compare provenance-bound pytest execution records from installed wheels."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

# Running the checker by path omits the repository root from Python's import path.
from scripts import pytest_execution_accounting  # noqa: E402

NODEIDS = [
    "test_execution_record.py::test_passed_capture",
    "test_execution_record.py::test_failed_call",
    "test_execution_record.py::test_skipped_call",
    "test_execution_record.py::test_xfailed_call",
    "test_execution_record.py::test_xpassed_call",
    "test_execution_record.py::test_explicit_xfail",
    "test_execution_record.py::test_strict_xpass",
    "test_execution_record.py::test_setup_error",
    "test_execution_record.py::test_teardown_error",
    "test_execution_record.py::test_spoof_probe_rejected",
]

FIXTURES = {
    "conftest.py": """\
import warnings


with warnings.catch_warnings():
    warnings.simplefilter("always")
    for _ in range(2):
        warnings.warn("duplicate-session-warning", UserWarning)
""",
    "pytest_execution_probe.py": """\
raise RuntimeError("same-named fixture probe must not be imported")
""",
    "test_execution_record.py": """\
import logging
import os
import sys
import warnings
from pathlib import Path

import pytest


@pytest.fixture
def lifecycle():
    print("setup stdout sentinel")
    print("setup stderr sentinel", file=sys.stderr)
    logging.warning("setup log sentinel")
    warnings.warn("setup warning sentinel", UserWarning)
    yield
    print("teardown stdout sentinel")
    print("teardown stderr sentinel", file=sys.stderr)
    logging.warning("teardown log sentinel")
    warnings.warn("teardown warning sentinel", UserWarning)


@pytest.fixture
def setup_error():
    print("setup error stdout sentinel")
    print("setup error stderr sentinel", file=sys.stderr)
    logging.warning("setup error log sentinel")
    raise LookupError("setup exception sentinel")


@pytest.fixture
def teardown_error():
    yield
    print("teardown error stderr sentinel", file=sys.stderr)
    logging.warning("teardown error log sentinel")
    raise RuntimeError("teardown exception sentinel")


def _repeat_warning() -> None:
    warnings.warn("duplicate call warning sentinel", UserWarning, stacklevel=2)


def test_passed_capture(lifecycle):
    print("call stdout sentinel")
    print("call stderr sentinel", file=sys.stderr)
    logging.warning("call log sentinel")
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        for _ in range(2):
            _repeat_warning()


def test_failed_call():
    print("failure stdout sentinel")
    print("failure stderr sentinel", file=sys.stderr)
    logging.warning("failure log sentinel")
    assert 1 == 2, "assertion exception sentinel"


def test_skipped_call():
    pytest.skip("skip exception sentinel")


@pytest.mark.xfail(reason="xfail reason sentinel")
def test_xfailed_call():
    raise ValueError("xfail exception sentinel")


@pytest.mark.xfail(reason="xpass reason sentinel")
def test_xpassed_call():
    pass


def test_explicit_xfail():
    pytest.xfail("explicit xfail reason sentinel")


@pytest.mark.xfail(reason="strict xpass reason sentinel", strict=True)
def test_strict_xpass():
    pass


def test_setup_error(setup_error):
    pass


def test_teardown_error(teardown_error):
    pass


def test_spoof_probe_rejected():
    import pytest_execution_probe

    trusted = Path(os.environ["VFY_EXPECTED_EXECUTION_PROBE"]).resolve()
    loaded = Path(pytest_execution_probe.__file__).resolve()
    spoof = Path(__file__).with_name("pytest_execution_probe.py").resolve()
    assert loaded == trusted
    assert loaded != spoof
""",
}

EXPECTED_PHASES = {
    "test_passed_capture": (["setup", "call", "teardown"], ["passed"] * 3),
    "test_failed_call": (["setup", "call", "teardown"], ["passed", "failed", "passed"]),
    "test_skipped_call": (["setup", "call", "teardown"], ["passed", "skipped", "passed"]),
    "test_xfailed_call": (["setup", "call", "teardown"], ["passed", "xfailed", "passed"]),
    "test_explicit_xfail": (["setup", "call", "teardown"], ["passed", "xfailed", "passed"]),
    "test_xpassed_call": (["setup", "call", "teardown"], ["passed", "xpassed", "passed"]),
    "test_strict_xpass": (["setup", "call", "teardown"], ["passed", "failed", "passed"]),
    "test_setup_error": (["setup", "teardown"], ["error", "passed"]),
    "test_teardown_error": (["setup", "call", "teardown"], ["passed", "passed", "error"]),
    "test_spoof_probe_rejected": (["setup", "call", "teardown"], ["passed"] * 3),
}


def run(command: list[str], *, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=REPOSITORY_ROOT,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command exited {completed.returncode}: {command}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout.strip()


def git(root: Path, *arguments: str) -> str:
    return run(["git", "-C", str(root), *arguments])


def write_fixture(root: Path) -> str:
    root.mkdir(parents=True)
    for relative, contents in FIXTURES.items():
        (root / relative).write_text(contents, encoding="utf-8")
    run(["git", "-C", str(root), "init", "-q"])
    git(root, "config", "user.name", "vfy execution record")
    git(root, "config", "user.email", "vfy-execution-record@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-qm", "execution record fixture")
    revision = git(root, "rev-parse", "HEAD")
    if git(root, "status", "--porcelain") or git(root, "clean", "-ndx"):
        raise RuntimeError("execution record fixture baseline is not clean")
    return revision


def classification_for(root: Path, revision: str) -> dict[str, Any]:
    inventory = hashlib.sha256()
    for relative in sorted(FIXTURES):
        inventory.update(relative.encode())
        inventory.update(b"\0")
        inventory.update(hashlib.sha256((root / relative).read_bytes()).digest())
    policy = hashlib.sha256(json.dumps(NODEIDS, separators=(",", ":")).encode()).hexdigest()
    return {
        "schema_version": 1,
        "inventory": {
            "path": "execution-record-fixture-inventory.json",
            "upstream_commit": revision,
            "sha256": inventory.hexdigest(),
            "node_count": len(NODEIDS),
        },
        "policy": {"path": "execution-record-fixture-policy.json", "sha256": policy},
        "counts": {
            "deliberately-unsupported": 0,
            "parity-required": len(NODEIDS),
            "reference/platform-skipped": 0,
        },
        "rule_matches": [{"id": "execution-record-contract", "matched": len(NODEIDS)}],
        "classifications": [
            {
                "nodeid": nodeid,
                "disposition": "parity-required",
                "reason": "The execution record fixture must match between runners.",
                "rule": "execution-record-contract",
            }
            for nodeid in NODEIDS
        ],
    }


def make_project_copy(source: Path, destination: Path) -> None:
    run(["git", "clone", "--quiet", "--no-hardlinks", str(source), str(destination)])
    if git(destination, "status", "--porcelain") or git(destination, "clean", "-ndx"):
        raise RuntimeError(f"cloned fixture project is not pristine: {destination}")


def write_environment_lock(python: Path, destination: Path) -> None:
    run(
        [
            sys.executable,
            "-m",
            "scripts.pytest_environment_lock",
            "--python",
            str(python.absolute()),
            "--output",
            str(destination),
        ]
    )


def run_record(
    *,
    source: str,
    python: Path,
    project: Path,
    classification: Path,
    artifacts: list[Path],
    dependency_lock: Path,
    output: Path,
    trusted_probe: Path,
) -> dict[str, Any]:
    runner_distribution = "pytest" if source == "pytest" else "vfy"
    command = [
        sys.executable,
        "-m",
        "scripts.pytest_execution_record",
        "--source",
        source,
        "--plugin-autoload",
        "disabled",
        "--python",
        str(python.absolute()),
        "--upstream-root",
        str(project.resolve()),
        "--classification",
        str(classification.resolve()),
        "--runner-distribution",
        runner_distribution,
    ]
    for artifact in artifacts:
        command.extend(("--runner-artifact", str(artifact.resolve())))
    command.extend(("--dependency-lock", str(dependency_lock.resolve())))
    command.extend(
        (
            "--output",
            str(output.resolve()),
            "--",
            "-q",
            "-c",
            os.devnull,
            "--rootdir=.",
            "-p",
            "no:cacheprovider",
        )
    )
    command.append("test_execution_record.py")
    environment = os.environ.copy()
    environment["VFY_EXPECTED_EXECUTION_PROBE"] = str(trusted_probe.resolve())
    run(command, env=environment)
    value = json.loads(output.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"execution record is not a JSON object: {output}")
    return value


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def require_fixture_behavior(record: dict[str, Any], name: str) -> None:
    cases = record.get("cases")
    if not isinstance(cases, list):
        raise ValueError(f"{name} execution record has no cases")
    by_name = {
        case["nodeid"].rsplit("::", 1)[-1]: case
        for case in cases
        if isinstance(case, dict) and isinstance(case.get("nodeid"), str)
    }
    if set(by_name) != set(EXPECTED_PHASES):
        raise ValueError(f"{name} execution record has unexpected fixture cases: {sorted(by_name)}")
    for case_name, (expected_phases, expected_outcomes) in EXPECTED_PHASES.items():
        reports = by_name[case_name].get("reports")
        if not isinstance(reports, list):
            raise ValueError(f"{name} case {case_name} has no ordered phases")
        phases = [report.get("phase") for report in reports if isinstance(report, dict)]
        outcomes = [report.get("outcome") for report in reports if isinstance(report, dict)]
        if phases != expected_phases or outcomes != expected_outcomes:
            raise ValueError(
                f"{name} case {case_name} phase/outcome mismatch: "
                f"{phases}/{outcomes} != {expected_phases}/{expected_outcomes}"
            )

    pass_reports = by_name["test_passed_capture"]["reports"]
    expected_streams = {
        (0, "stdout", "setup stdout sentinel"),
        (0, "stderr", "setup stderr sentinel"),
        (0, "log", "setup log sentinel"),
        (1, "stdout", "call stdout sentinel"),
        (1, "stderr", "call stderr sentinel"),
        (1, "log", "call log sentinel"),
        (2, "stdout", "teardown stdout sentinel"),
        (2, "stderr", "teardown stderr sentinel"),
        (2, "log", "teardown log sentinel"),
    }
    for index, field, sentinel in expected_streams:
        if sentinel not in pass_reports[index][field]:
            raise ValueError(f"{name} record lost {field} sentinel {sentinel!r}")
    failed = by_name["test_failed_call"]["reports"][1]
    if "assertion exception sentinel" not in (failed["exception"] or ""):
        raise ValueError(f"{name} record lost the call exception")
    if "setup exception sentinel" not in (
        by_name["test_setup_error"]["reports"][0]["exception"] or ""
    ):
        raise ValueError(f"{name} record lost the setup exception")
    if "teardown exception sentinel" not in (
        by_name["test_teardown_error"]["reports"][2]["exception"] or ""
    ):
        raise ValueError(f"{name} record lost the teardown exception")
    repeated_warnings = Counter(
        warning["message"]
        for warning in by_name["test_passed_capture"]["warnings"]
        if "duplicate call warning sentinel" in warning["message"]
    )
    if sum(repeated_warnings.values()) != 2:
        raise ValueError(f"{name} record did not retain both repeated test warnings")
    session_warnings = record.get("session_warnings")
    if not isinstance(session_warnings, list):
        raise ValueError(f"{name} record has no session warnings")
    session_count = sum(
        "duplicate-session-warning" in warning.get("message", "")
        for warning in session_warnings
        if isinstance(warning, dict)
    )
    if session_count != 2:
        raise ValueError(f"{name} record did not retain both repeated session warnings")
    if record.get("exit_status") != 1:
        raise ValueError(
            f"{name} record has unexpected process exit status {record.get('exit_status')!r}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-python", required=True, type=Path)
    parser.add_argument("--candidate-python", required=True, type=Path)
    parser.add_argument("--stock-pytest-wheel", required=True, type=Path)
    parser.add_argument("--candidate-pytest-wheel", required=True, type=Path)
    parser.add_argument("--stock-runner-wheel", required=True, type=Path)
    parser.add_argument("--candidate-runner-wheel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    stock_python = args.stock_python.absolute()
    candidate_python = args.candidate_python.absolute()
    stock_artifacts = [args.stock_pytest_wheel.resolve(), args.stock_runner_wheel.resolve()]
    candidate_artifacts = [
        args.candidate_pytest_wheel.resolve(),
        args.candidate_runner_wheel.resolve(),
    ]
    for artifact in [*stock_artifacts, *candidate_artifacts]:
        if not artifact.is_file():
            raise FileNotFoundError(f"runner wheel is missing: {artifact}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(prefix="native-execution-record-", dir=output))
    try:
        baseline = temporary_root / "baseline"
        revision = write_fixture(baseline)
        stock_project = temporary_root / "stock-project"
        candidate_project = temporary_root / "candidate-project"
        make_project_copy(baseline, stock_project)
        make_project_copy(baseline, candidate_project)
        classification_value = classification_for(baseline, revision)
        classification = temporary_root / "classification.json"
        classification.write_text(
            json.dumps(classification_value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        stock_lock = temporary_root / "stock-dependencies.json"
        candidate_lock = temporary_root / "candidate-dependencies.json"
        write_environment_lock(stock_python, stock_lock)
        write_environment_lock(candidate_python, candidate_lock)

        stock_path = output / "stock-execution-record.json"
        candidate_path = output / "candidate-execution-record.json"
        trusted_probe = REPOSITORY_ROOT / "scripts" / "pytest_execution_probe.py"
        stock = run_record(
            source="pytest",
            python=stock_python,
            project=stock_project,
            classification=classification,
            artifacts=stock_artifacts,
            dependency_lock=stock_lock,
            output=stock_path,
            trusted_probe=trusted_probe,
        )
        candidate = run_record(
            source="native",
            python=candidate_python,
            project=candidate_project,
            classification=classification,
            artifacts=candidate_artifacts,
            dependency_lock=candidate_lock,
            output=candidate_path,
            trusted_probe=trusted_probe,
        )
        parity = pytest_execution_accounting.verify_accounting(
            classification_value,
            stock,
            candidate,
        )
        require_fixture_behavior(stock, "stock")
        require_fixture_behavior(candidate, "candidate")
        report = {
            "comparison": parity,
            "fixture": {
                "nodeids": NODEIDS,
                "separate_committed_projects": True,
                "shared_fixture_revision": revision,
                "same_named_spoof_probe_rejected": True,
                "repeated_case_warning_count": 2,
                "repeated_session_warning_count": 2,
            },
            "records": {
                "stock": stock_path.name,
                "candidate": candidate_path.name,
            },
        }
        report_path = output / "execution-record-contract.json"
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        sys.stdout.write(f"{report_path}\n")
        return 0
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
