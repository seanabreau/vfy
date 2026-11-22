from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

from benchmarks import benchmark_evidence, benchmark_rust_boundary
from benchmarks.benchmark_harness import benchmark_environment
from benchmarks.benchmark_projects import outcome_records, timed_sequence
from benchmarks.observer_plugins import OUTCOME_PLUGIN, stage_plugins


def test_outcome_observer_keeps_call_when_test_clears_environment(tmp_path: Path) -> None:
    (tmp_path / "test_case.py").write_text(
        "import pytest\n"
        "def test_clears_outcome_path(monkeypatch: pytest.MonkeyPatch):\n"
        "    monkeypatch.delenv('VFY_BENCHMARK_OUTCOMES')\n"
        "    assert True\n",
        encoding="utf-8",
    )
    outcome_file = tmp_path / "outcomes.jsonl"
    timed_sequence(
        [
            (
                "serial",
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-p",
                    OUTCOME_PLUGIN,
                    "-q",
                    "test_case.py",
                ],
            )
        ],
        root=tmp_path,
        environment=benchmark_environment(
            {"PYTHONPATH": str(stage_plugins(tmp_path / "observer-plugins"))}
        ),
        outcome_file=outcome_file,
    )
    assert set(outcome_records(outcome_file)) == {
        ("test_case.py::test_clears_outcome_path", phase, "passed", None)
        for phase in ("setup", "call", "teardown")
    }


def test_outcome_observer_restores_outer_session_after_nested_pytest(tmp_path: Path) -> None:
    (tmp_path / "test_outer.py").write_text(
        "from pathlib import Path\n"
        "import pytest\n"
        "def test_nested_pytest(tmp_path, monkeypatch):\n"
        "    child = tmp_path / 'test_child.py'\n"
        "    child.write_text('def test_child():\\n    assert True\\n')\n"
        "    monkeypatch.setenv('VFY_BENCHMARK_OUTCOMES', str(Path.cwd() / 'nested.jsonl'))\n"
        f"    assert pytest.main(['-q', '-p', {OUTCOME_PLUGIN!r}, str(child)]) == 0\n"
        "    monkeypatch.delenv('VFY_BENCHMARK_OUTCOMES')\n",
        encoding="utf-8",
    )
    outcome_file = tmp_path / "outer.jsonl"
    timed_sequence(
        [
            (
                "serial",
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-p",
                    OUTCOME_PLUGIN,
                    "-q",
                    "test_outer.py",
                ],
            )
        ],
        root=tmp_path,
        environment=benchmark_environment(
            {"PYTHONPATH": str(stage_plugins(tmp_path / "observer-plugins"))}
        ),
        outcome_file=outcome_file,
    )
    assert set(outcome_records(outcome_file)) == {
        ("test_outer.py::test_nested_pytest", phase, "passed", None)
        for phase in ("setup", "call", "teardown")
    }
    nested_files = list(tmp_path.glob("**/nested.jsonl"))
    assert len(nested_files) == 1
    nested_records = outcome_records(nested_files[0])
    assert all(nodeid.endswith("test_child.py::test_child") for nodeid, *_ in nested_records)
    assert {(phase, outcome, wasxfail) for _, phase, outcome, wasxfail in nested_records} == {
        (phase, "passed", None) for phase in ("setup", "call", "teardown")
    }


def test_rust_boundary_cache_probe_imports_from_worktree() -> None:
    result = benchmark_rust_boundary.isolated_cache_probe(
        Namespace(capacity_files=1, large_files=1, large_file_functions=2),
        "entry-capacity",
    )

    assert result["analyzed_files"] == 1


def test_evidence_comparison_reads_loaded_criterion_medians() -> None:
    comparison = benchmark_evidence.compare_target_medians(
        {"median": {"point_estimate": 4.0}},
        {"median": {"point_estimate": 10.0}},
    )

    assert comparison == {
        "candidate_median_ns": 4.0,
        "baseline_median_ns": 10.0,
        "baseline_over_candidate": 2.5,
    }
