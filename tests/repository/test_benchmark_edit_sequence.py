"""The controlled benchmark exercises persisted state through failure and repair."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from benchmarks.benchmark_edit_loop import create_project, run_developer_sequence
from benchmarks.benchmark_harness import benchmark_environment
from scripts.summarize_competitor_benchmarks import edit_loop_selection


def test_developer_sequence_preserves_selection_and_repairs_failure(tmp_path: Path) -> None:
    pristine = tmp_path / "pristine"
    pristine.mkdir()
    create_project(pristine, 4, "shared")
    result = run_developer_sequence(
        pristine, tmp_path / "sequence", benchmark_environment(), modules=4, measure=False
    )
    first = "tests/test_module_000.py::test_module_000"
    phases = {record["phase"]: record["outcomes"] for record in result["phases"]}
    assert phases["unchanged"] == {}
    assert phases["local-edit"] == {first: "passed"}
    assert phases["shared-edit"] == {first: "passed"}
    assert phases["breaking"] == {first: "failed"}
    assert phases["repair"] == phases["shared-edit"]
    assert phases["full"] == phases["baseline"]


@pytest.mark.parametrize("workload", ["shared", "parametrized"])
def test_edit_loop_output_round_trips_through_public_compare(tmp_path: Path, workload: str) -> None:
    """The real producer output must satisfy the public diagnostic comparator."""

    root = Path(__file__).resolve().parents[2]
    environment = benchmark_environment()
    output = tmp_path / "edit-loop.json"
    copy = tmp_path / "edit-loop-copy.json"
    command = [
        sys.executable,
        "-m",
        "benchmarks",
        "run",
        "edit-loop",
        "--mode",
        "diagnostic",
        "--modules",
        "4",
        "--workload",
        workload,
        "--warmups",
        "0",
        "--runs",
        "1",
        "--output",
        str(output),
    ]
    produced = subprocess.run(
        command,
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert produced.returncode == 0, produced.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["benchmark"] == "edit-loop"
    assert payload["methodology"]["validation"]
    selected = edit_loop_selection(payload)
    assert selected["declared_policy"] == payload["selection_vs_testmon"]
    assert selected["vfy_test_ids"] == sorted(payload["outcomes"]["selected"])
    assert selected["testmon_test_ids"] == sorted(payload["outcomes"]["testmon"])
    if selected["declared_policy"] == "vfy_conservative":
        assert "exact selected pytest node IDs" not in payload["methodology"]["validation"]
    shutil.copyfile(output, copy)

    compared = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks",
            "compare",
            "--mode",
            "diagnostic",
            str(output),
            str(copy),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert compared.returncode == 0, compared.stderr
    report = json.loads(compared.stdout)
    assert report["records"]
