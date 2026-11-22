"""Run the full stock baseline and compare the supported no-autoload core scope."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import pytest_execution_accounting, pytest_execution_record


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def clone_pristine_upstream(source: Path, destination: Path, expected_commit: str) -> None:
    actual_commit = git(source, "rev-parse", "HEAD")
    if actual_commit != expected_commit:
        raise ValueError(f"upstream source is {actual_commit}, expected {expected_commit}")
    if git(source, "status", "--porcelain") or git(source, "clean", "-ndx"):
        raise ValueError(f"upstream source must be clean and pristine: {source}")
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )
    if git(destination, "rev-parse", "HEAD") != expected_commit:
        raise ValueError(f"cloned upstream revision differs: {destination}")
    if git(destination, "status", "--porcelain") or git(destination, "clean", "-ndx"):
        raise ValueError(f"cloned upstream source is not pristine: {destination}")


def candidate_arguments(
    classifications: list[dict[str, str]],
    *,
    quiet: bool = True,
    corpus_parent: Path | None = None,
) -> list[str]:
    arguments: list[str] = ["-q"] if quiet else []
    arguments.extend(("--runpytest=inprocess", "--confcutdir=<CORPUS_PARENT>"))
    arguments.extend(
        f"--deselect={record['nodeid']}"
        for record in classifications
        if record["disposition"] == "deliberately-unsupported"
    )
    if corpus_parent is not None:
        arguments = [
            f"--confcutdir={corpus_parent.resolve()}"
            if argument == "--confcutdir=<CORPUS_PARENT>"
            else argument
            for argument in arguments
        ]
    return arguments


def create_fresh_output_directory(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=False)


def _resolved_environment_sha256(environment: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(environment, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _preflight_provenance(
    classification: dict[str, Any],
    environment: dict[str, Any],
    artifacts: list[dict[str, str]],
    project_identity_sha256: str,
) -> dict[str, Any]:
    return {
        "upstream_commit": classification["inventory"]["upstream_commit"],
        "inventory_sha256": classification["inventory"]["sha256"],
        "policy_sha256": classification["policy"]["sha256"],
        "python": environment["python"],
        "platform": environment["platform"],
        "runner_artifact_set_sha256": pytest_execution_record.artifact_set_sha256(artifacts),
        "resolved_environment_sha256": _resolved_environment_sha256(environment),
        "resolved_environment": environment,
        "project_identity_sha256": project_identity_sha256,
    }


def execute_corpus(
    *,
    upstream_root: Path,
    classification_path: Path,
    stock_python: Path,
    candidate_python: Path,
    stock_artifacts: list[Path],
    candidate_artifacts: list[Path],
    output: Path,
    rejection_path: Path | None,
    timeout: float = 3_600,
) -> dict[str, Any]:
    pytest_execution_record.validate_timeout(timeout)
    classification = pytest_execution_accounting.load_object(classification_path)
    classifications, _, unsupported = pytest_execution_accounting.validate_three_run_classification(
        classification
    )
    if rejection_path is None:
        raise ValueError(
            "the three-run corpus gate requires provenance-bound rejection and plug-in proofs"
        )

    expected_commit = classification["inventory"]["upstream_commit"]
    pytest_execution_record.validate_upstream(upstream_root, classification)
    stock_python = pytest_execution_record.resolve_executable(stock_python)
    candidate_python = pytest_execution_record.resolve_executable(candidate_python)
    stock_artifact_rows = pytest_execution_record.artifact_records(
        stock_artifacts, "stock runner artifact"
    )
    candidate_artifact_rows = pytest_execution_record.artifact_records(
        candidate_artifacts, "candidate runner artifact"
    )
    stock_artifact_set_sha256 = pytest_execution_record.artifact_set_sha256(stock_artifact_rows)
    candidate_artifact_set_sha256 = pytest_execution_record.artifact_set_sha256(
        candidate_artifact_rows
    )
    if stock_artifact_set_sha256 != candidate_artifact_set_sha256:
        raise ValueError("three-run runner artifact sets differ")
    stock_installations = pytest_execution_record.verify_installed_artifacts(
        stock_python, stock_artifacts, "pytest"
    )
    candidate_installations = pytest_execution_record.verify_installed_artifacts(
        candidate_python, candidate_artifacts, "vfy"
    )
    pytest_execution_record.validate_runner_artifacts("pytest", "pytest", stock_installations)
    pytest_execution_record.validate_runner_artifacts("native", "vfy", candidate_installations)
    stock_environment = pytest_execution_record.resolved_environment(stock_python)
    candidate_environment = pytest_execution_record.resolved_environment(candidate_python)
    if stock_environment != candidate_environment:
        raise ValueError("three-run resolved dependency environments differ")
    pytest_execution_record.interpreter_metadata(stock_python, "pytest")
    pytest_execution_record.interpreter_metadata(candidate_python, "vfy")
    upstream_identity = pytest_execution_record.project_identity(upstream_root)
    stock_provenance = _preflight_provenance(
        classification, stock_environment, stock_artifact_rows, upstream_identity
    )
    candidate_provenance = _preflight_provenance(
        classification,
        candidate_environment,
        candidate_artifact_rows,
        upstream_identity,
    )
    rejection_record = pytest_execution_accounting.load_object(rejection_path)
    rejection_envelope = pytest_execution_accounting.validate_rejection_envelope(
        rejection_record,
        stock_provenance,
        candidate_provenance,
    )
    pytest_execution_accounting.validate_rejection_rows(
        rejection_envelope["rejections"], unsupported
    )
    pytest_execution_accounting.validate_plugin_proofs(
        rejection_envelope["plugin_proofs"],
        stock_provenance,
        candidate_provenance,
        required=True,
    )
    if rejection_envelope["hypothesis_lifecycle_proof"] is None:
        raise ValueError(
            "the controlled pytester protocol requires its ordered inprocess lifecycle diagnostic"
        )

    create_fresh_output_directory(output)
    temporary_root = Path(tempfile.mkdtemp(prefix="pytest-corpus-", dir=output))
    completed = False
    try:
        stock_root = temporary_root / "stock-upstream"
        stock_reference_root = temporary_root / "stock-reference-upstream"
        candidate_root = temporary_root / "candidate-upstream"
        clone_pristine_upstream(upstream_root, stock_root, expected_commit)
        clone_pristine_upstream(upstream_root, stock_reference_root, expected_commit)
        clone_pristine_upstream(upstream_root, candidate_root, expected_commit)
        stock_lock = output / "stock-dependencies.json"
        candidate_lock = output / "candidate-dependencies.json"
        pytest_execution_record.write_atomic(stock_lock, stock_environment)
        pytest_execution_record.write_atomic(candidate_lock, candidate_environment)
        full_stock_record = pytest_execution_record.record_run(
            source="pytest",
            python=stock_python,
            upstream_root=stock_root,
            classification_path=classification_path,
            runner_distribution="pytest",
            runner_artifacts=stock_artifacts,
            dependency_locks=[stock_lock],
            pytest_arguments=["-q"],
            plugin_autoload="enabled",
            evidence_dir=output / "stock-evidence",
            timeout=timeout,
        )
        stock_path = output / "stock-execution-record.json"
        pytest_execution_record.write_atomic(stock_path, full_stock_record)
        if full_stock_record["exit_status"] != 0:
            raise ValueError("full stock baseline must exit successfully before controlled runs")

        corpus_conftest = pytest_execution_record.CORPUS_CONFTEST
        config_path = temporary_root / "conftest.py"
        if config_path.exists():
            raise ValueError("fresh corpus parent unexpectedly contains a conftest")
        shutil.copyfile(corpus_conftest, config_path)

        reference_arguments = candidate_arguments(classifications, corpus_parent=temporary_root)
        stock_reference_record = pytest_execution_record.record_run(
            source="pytest",
            python=stock_python,
            upstream_root=stock_reference_root,
            classification_path=classification_path,
            runner_distribution="pytest",
            runner_artifacts=stock_artifacts,
            dependency_locks=[stock_lock],
            pytest_arguments=reference_arguments,
            plugin_autoload="disabled",
            pytester_mode="inprocess-with-exceptions",
            ancestor_config_root=temporary_root,
            evidence_dir=output / "stock-reference-evidence",
            timeout=timeout,
        )
        stock_reference_path = output / "stock-reference-execution-record.json"
        pytest_execution_record.write_atomic(stock_reference_path, stock_reference_record)

        candidate_record = pytest_execution_record.record_run(
            source="native",
            python=candidate_python,
            upstream_root=candidate_root,
            classification_path=classification_path,
            runner_distribution="vfy",
            runner_artifacts=candidate_artifacts,
            dependency_locks=[candidate_lock],
            pytest_arguments=reference_arguments,
            plugin_autoload="disabled",
            pytester_mode="inprocess-with-exceptions",
            ancestor_config_root=temporary_root,
            evidence_dir=output / "candidate-evidence",
            timeout=timeout,
        )
        candidate_path = output / "candidate-execution-record.json"
        pytest_execution_record.write_atomic(candidate_path, candidate_record)

        result = pytest_execution_accounting.verify_three_run_accounting(
            classification,
            full_stock_record,
            stock_reference_record,
            candidate_record,
            rejection_record,
        )
        accounting_path = output / "corpus-accounting.json"
        pytest_execution_accounting.write_atomic(
            accounting_path,
            json.dumps(result, indent=2, sort_keys=True) + "\n",
        )
        completed = True
        return result
    finally:
        if completed:
            shutil.rmtree(temporary_root, ignore_errors=True)
        else:
            sys.stderr.write(f"Corpus run failed; diagnostic roots retained at {temporary_root}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-root", required=True, type=Path)
    parser.add_argument("--classification", required=True, type=Path)
    parser.add_argument("--stock-python", required=True, type=Path)
    parser.add_argument("--candidate-python", required=True, type=Path)
    parser.add_argument("--stock-runner-artifact", action="append", required=True, type=Path)
    parser.add_argument("--candidate-runner-artifact", action="append", required=True, type=Path)
    parser.add_argument("--rejections", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=3_600)
    args = parser.parse_args()

    result = execute_corpus(
        upstream_root=args.upstream_root.resolve(),
        classification_path=args.classification.resolve(),
        stock_python=pytest_execution_record.resolve_executable(args.stock_python),
        candidate_python=pytest_execution_record.resolve_executable(args.candidate_python),
        stock_artifacts=[path.resolve() for path in args.stock_runner_artifact],
        candidate_artifacts=[path.resolve() for path in args.candidate_runner_artifact],
        output=args.output.resolve(),
        rejection_path=args.rejections.resolve() if args.rejections else None,
        timeout=args.timeout,
    )
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
