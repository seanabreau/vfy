"""Audit retained real-project samples using exact context arcs and tracer identity.

This reads existing evidence and retained build artifacts. It never runs tests,
changes a sample, or substitutes an aggregate signature for a database proof.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmarks.benchmark_projects import normalized_data
from scripts.pytest_execution_record import file_sha256, write_atomic
from validation.pytest_runner.benchmark_real_candidates import (
    case_outcomes,
    normalized_history,
    ordered_roles,
    verify_runtime_parity,
)
from validation.pytest_runner.benchmark_vfy_coverage import (
    report_digest,
    verify_build_identity,
    verify_installed_distribution_files,
)


def audit_identity(identity: dict[str, Any]) -> None:
    verify_runtime_parity(identity)
    for role in ordered_roles(identity):
        record = identity[role]
        manifest_path = Path(record["build"]["path"])
        manifest = json.loads(manifest_path.read_text())
        wheel = Path(manifest["wheel_path"])
        verify_installed_distribution_files(role, "vfy", record["installed"], wheel)
        verified = verify_build_identity(manifest_path, record["installed"], wheel, retained=True)
        if verified != record["build"]:
            raise ValueError(f"retained build identity changed: {role}")


def comparable_proof(proof: dict[str, Any], history_state: str) -> dict[str, Any]:
    comparable = dict(proof)
    if history_state == "cold":
        history = proof["history"]
        empty_seed = {
            **history,
            "tests": {
                node: {**entry, "observations": []} for node, entry in history["tests"].items()
            },
        }
        comparable["history"] = normalized_history(
            history,
            empty_seed,
            case_outcomes(proof["cases"]),
            set(proof["called"]),
        )
    return comparable


def verify_recorded_coverage(proof: dict[str, Any], actual: object, child: Path) -> None:
    recorded = proof.get("coverage", {})
    if "by_context" in recorded and json.loads(json.dumps(actual)) != recorded:
        raise ValueError(f"retained database differs from its recorded exact proof: {child}")


def audit_stratum(
    directory: Path, stratum: dict[str, Any], roles: tuple[str, ...]
) -> dict[str, Any]:
    reference_directory = directory / "parity-00-baseline"
    reference_command = json.loads((reference_directory / "command.json").read_text())
    root = Path(reference_command["cwd"])
    expected = normalized_data(reference_directory / ".coverage", root)
    reference_proof = json.loads((reference_directory / "proof.json").read_text())
    reference_digest = report_digest(comparable_proof(reference_proof, stratum["history_state"]))
    phase_counts = {
        phase: {role: [] for role in roles} for phase in ("parity", "warmup", "measured")
    }
    retained = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        match = re.fullmatch(r"(parity|warmup|measured)-(\d+)-(.+)", child.name)
        if match is None or match[3] not in roles:
            raise ValueError(f"unrecognized sample directory: {child}")
        phase, index, role = match[1], int(match[2]), match[3]
        status = json.loads((child / "status.json").read_text())
        seconds = status.get("seconds")
        if (
            status.get("exit_status") != 0
            or type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or seconds < 0
        ):
            raise ValueError(f"retained child has no successful timing status: {child}")
        command = json.loads((child / "command.json").read_text())
        if command["cwd"] != str(root):
            raise ValueError(f"project root changed between samples: {child}")
        proof = json.loads((child / "proof.json").read_text())
        validated = json.loads((child / "validated.json").read_text())
        proof_digest = report_digest(comparable_proof(proof, stratum["history_state"]))
        if proof_digest != validated.get("semantic_digest"):
            raise ValueError(f"original retained semantic proof changed: {child}")
        if proof_digest != reference_digest:
            raise ValueError(f"retained semantic parity differs from baseline: {child}")
        actual = normalized_data(child / ".coverage", root)
        verify_recorded_coverage(proof, actual, child)
        if actual != expected:
            raise ValueError(f"exact lines/arcs/contexts/tracer parity failed: {child}")
        if phase == "measured" and seconds != stratum["samples"][role][index]:
            raise ValueError(f"result sample differs from retained child status: {child}")
        phase_counts[phase][role].append(index)
        retained.append(
            {
                "sample": child.name,
                "database_sha256": file_sha256(child / ".coverage"),
                "exact_signature_sha256": report_digest(actual),
                "original_proof_sha256": proof_digest,
            }
        )
    for phase, counts in phase_counts.items():
        expected_count = len(counts["baseline"])
        if phase == "parity" and expected_count != 1:
            raise ValueError("retained stratum requires exactly one initial parity round")
        for role, indices in counts.items():
            if sorted(indices) != list(range(expected_count)):
                raise ValueError(f"incomplete or unpaired {phase} samples for {role}")
            if phase == "measured" and expected_count != len(stratum["samples"][role]):
                raise ValueError("retained result contains unverified timing samples")
    return {
        "count": stratum["count"],
        "history_state": stratum["history_state"],
        "exact_database_parity": True,
        "phase_counts": {phase: len(counts["baseline"]) for phase, counts in phase_counts.items()},
        "samples": retained,
    }


def audit(results: Path, require_publication: bool) -> dict[str, Any]:
    result = json.loads((results / "result.json").read_text())
    if result["controller"].get("dirty") is not False:
        raise ValueError("retained harness source was not clean")
    projects = []
    for project in result["projects"]:
        audit_identity(project["identity"])
        roles = ordered_roles(project["identity"])
        rows = []
        for stratum in project["strata"]:
            directory = (
                results / project["project"] / f"n{stratum['count']}-{stratum['history_state']}"
            )
            if not directory.is_dir() and stratum["count"] == project["full_suite_count"]:
                directory = results / project["project"] / f"n0-{stratum['history_state']}"
            row = audit_stratum(directory, stratum, roles)
            row["publication_sampling"] = (
                row["phase_counts"]["warmup"] >= 3 and row["phase_counts"]["measured"] >= 15
            )
            if require_publication and not row["publication_sampling"]:
                raise ValueError("publication requires three warmups and fifteen measured rounds")
            rows.append(row)
        if not rows:
            raise ValueError("retained project has no completed strata")
        projects.append({"project": project["project"], "roles": roles, "strata": rows})
    if not projects:
        raise ValueError("retained result has no projects")
    return {
        "results": str(results),
        "result_sha256": file_sha256(results / "result.json"),
        "passed": True,
        "projects": projects,
        "rss_measured": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True, action="append")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-publication", action="store_true")
    args = parser.parse_args()
    write_atomic(
        args.output,
        {"audits": [audit(path.absolute(), args.require_publication) for path in args.results]},
    )


if __name__ == "__main__":
    main()
