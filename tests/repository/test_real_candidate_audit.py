"""Retained evidence must preserve arc contexts and immutable runtime bytes."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from coverage import CoverageData
from validation.pytest_runner.audit_real_candidates import audit_stratum
from validation.pytest_runner.benchmark_real_candidates import verify_runtime_parity
from validation.pytest_runner.benchmark_vfy_coverage import report_digest


@pytest.mark.parametrize("damage", ["arc-context", "tracer", "proof-metadata"])
def test_retained_audit_rejects_changes_hidden_by_aggregate_coverage(
    tmp_path: Path, damage: str
) -> None:
    source = str(tmp_path / "subject.py")
    alternatives = [
        [(-1, 1), (1, 2), (2, 3), (3, -1)],
        [(-1, 1), (1, 3), (3, 2), (2, -1)],
    ]
    for role in ("baseline", "candidate"):
        child = tmp_path / "samples" / f"parity-00-{role}"
        child.mkdir(parents=True)
        data = CoverageData(str(child / ".coverage"))
        try:
            for index, context in enumerate(("first", "second")):
                data.set_context(context)
                edges = alternatives[
                    1 - index if role == "candidate" and damage == "arc-context" else index
                ]
                data.add_arcs({source: edges})
            if role == "candidate" and damage == "tracer":
                data.add_file_tracers({source: "changed.Plugin"})
            data.write()
        finally:
            data.close(force=True)
        (child / "command.json").write_text(json.dumps({"cwd": str(tmp_path)}))
        (child / "status.json").write_text(json.dumps({"exit_status": 0, "seconds": 0.001}))
        proof = {"report": "changed"} if role == "candidate" and damage == "proof-metadata" else {}
        (child / "proof.json").write_text(json.dumps(proof))
        (child / "validated.json").write_text(json.dumps({"semantic_digest": report_digest(proof)}))
    stratum = {
        "count": 1,
        "history_state": "retained",
        "samples": {"baseline": [], "candidate": []},
    }
    failure = (
        "retained semantic parity differs from baseline"
        if damage == "proof-metadata"
        else "lines/arcs/contexts/tracer parity failed"
    )
    with pytest.raises(ValueError, match=failure):
        audit_stratum(tmp_path / "samples", stratum, ("baseline", "candidate"))


def test_retained_audit_binds_database_to_recorded_exact_proof(tmp_path: Path) -> None:
    child = tmp_path / "parity-00-baseline"
    child.mkdir()
    data = CoverageData(str(child / ".coverage"))
    try:
        data.add_lines({str(tmp_path / "subject.py"): [1]})
        data.write()
    finally:
        data.close(force=True)
    proof = {"coverage": {"by_context": {}}}
    (child / "command.json").write_text(json.dumps({"cwd": str(tmp_path)}))
    (child / "status.json").write_text(json.dumps({"exit_status": 0, "seconds": 0.001}))
    (child / "proof.json").write_text(json.dumps(proof))
    (child / "validated.json").write_text(json.dumps({"semantic_digest": report_digest(proof)}))
    stratum = {"count": 1, "history_state": "retained", "samples": {"baseline": []}}
    with pytest.raises(ValueError, match="database differs from its recorded exact proof"):
        audit_stratum(tmp_path, stratum, ("baseline",))


def test_runtime_parity_rejects_different_pytest_bytes_with_same_version() -> None:
    installed = {
        "python": "3.12.0",
        "implementation": "CPython",
        "dependencies": [["pytest", "9.1.1"]],
        "pytest_files": {"_pytest/config.py": "original bytes"},
    }
    identity = {
        "baseline": {"installed": installed},
        "candidate": {"installed": copy.deepcopy(installed)},
    }
    verify_runtime_parity(identity)
    identity["candidate"]["installed"]["pytest_files"] = {"_pytest/config.py": "modified bytes"}
    with pytest.raises(ValueError, match="runtime pytest_files differs"):
        verify_runtime_parity(identity)
    del identity["baseline"]["installed"]["implementation"]
    with pytest.raises(ValueError, match="metadata is missing implementation"):
        verify_runtime_parity(identity)
