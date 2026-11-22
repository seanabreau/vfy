"""Publication gates must reject missing evidence, including untouched files."""

from __future__ import annotations

import contextlib
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def edit_loop_benchmark(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "benchmarks"))
    return importlib.import_module("benchmark_edit_loop")


@pytest.fixture
def rust_boundary_benchmark(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "benchmarks"))
    return importlib.import_module("benchmark_rust_boundary")


@pytest.fixture
def selection_persistence_benchmark(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "benchmarks"))
    return importlib.import_module("benchmark_selection_persistence")


def test_selection_persistence_benchmark_checks_outputs(
    tmp_path: Path, selection_persistence_benchmark: ModuleType
) -> None:
    benchmark = selection_persistence_benchmark
    data = benchmark.create_data(tmp_path, files=2, contexts=3, arcs=4)
    expected_all = benchmark.expected_tests(3)
    expected_selected = benchmark.expected_selected_tests(3)
    destination = tmp_path / ".coverage"

    benchmark.measure_selection(data, tmp_path / "history.json", expected_all, expected_selected)
    benchmark.measure_persistence(data, destination)

    assert list(benchmark.CoverageData.read(str(destination)).arc_records()) == list(
        data.arc_records()
    )


def test_selection_benchmark_rejects_missing_arc_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection_persistence_benchmark: ModuleType,
) -> None:
    benchmark = selection_persistence_benchmark
    benchmark.create_data(tmp_path, files=2, contexts=4, arcs=4)
    context_arcs = [(f"test_{index:04d}|run", []) for index in range(4)]
    files = [
        (str(tmp_path / f"module_{index:05d}.py"), [], [], [], context_arcs) for index in range(2)
    ]
    data = benchmark.CoverageData.from_records(True, files, {}, {"has_arcs": "1"})
    monkeypatch.chdir(tmp_path)

    result = benchmark.selection_result(data, tmp_path / "history.json")

    with pytest.raises(RuntimeError, match="arc-dependent selected tests"):
        benchmark.validate_selection(
            result,
            benchmark.expected_tests(4),
            benchmark.expected_selected_tests(4),
            tmp_path / "module_00000.py",
        )


def test_rich_report_benchmark_selects_expected_evidence(
    tmp_path: Path, rust_boundary_benchmark: ModuleType
) -> None:
    benchmark = rust_boundary_benchmark
    store, paths = benchmark.create_rich_store(
        tmp_path,
        file_count=2,
        context_count=4,
        semantic_decisions=8,
    )

    selected = benchmark.build_rows(
        store,
        include=[paths[0]],
        contexts=[benchmark.RICH_CONTEXT_PATTERN],
    )
    all_files = benchmark.build_rows(
        store,
        contexts=[benchmark.RICH_CONTEXT_PATTERN],
    )

    assert benchmark.row_signature(selected) == benchmark.expected_rich_signature([paths[0]])
    assert benchmark.row_signature(all_files) == benchmark.expected_rich_signature(paths)
    assert selected[0].covered_lines == {1}
    assert selected[0].contexts_by_line == {1: ["test_0001", "test_0002", "test_0003"]}


def test_rich_report_benchmark_rejects_too_few_contexts(
    tmp_path: Path, rust_boundary_benchmark: ModuleType
) -> None:
    with pytest.raises(ValueError, match="requires at least four contexts"):
        rust_boundary_benchmark.create_rich_store(
            tmp_path,
            file_count=1,
            context_count=3,
            semantic_decisions=1,
        )


def test_partial_cache_benchmark_reanalyzes_only_the_changed_source(
    tmp_path: Path, rust_boundary_benchmark: ModuleType
) -> None:
    benchmark = rust_boundary_benchmark
    store = benchmark.CoverageStore.read(benchmark.create_fixture(tmp_path, 3))
    signature = benchmark.row_signature(benchmark.build_rows(store))

    samples, records = benchmark.measure_analysis(
        store,
        warmups=1,
        runs=2,
        changed_source=tmp_path / "module_00000.py",
        expected_signature=signature,
    )

    assert len(samples["one_source_changed"]) == 2
    assert [
        tuple(
            changed - reused
            for changed, reused in zip(
                record["one_source_changed"][:2], record["reused"][:2], strict=True
            )
        )
        for record in records
    ] == [(2, 1), (2, 1)]


def test_partial_cache_benchmark_rejects_stale_rows_with_matching_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rust_boundary_benchmark: ModuleType,
) -> None:
    benchmark = rust_boundary_benchmark
    store = benchmark.CoverageStore.read(benchmark.create_fixture(tmp_path, 3))
    rows = benchmark.build_rows(store)
    source = tmp_path / "module_00000.py"
    original = source.read_text(encoding="utf-8")
    monkeypatch.setattr(benchmark, "build_rows", lambda _store: rows)

    with pytest.raises(RuntimeError, match="cache hits=0, misses=0"):
        benchmark.measure_analysis(
            store,
            warmups=0,
            runs=1,
            changed_source=source,
            expected_signature=benchmark.row_signature(rows),
        )

    assert source.read_text(encoding="utf-8") != original


@pytest.mark.parametrize(
    ("exit_code", "outcome"),
    [(0, "failed"), (1, "passed"), (2, "failed"), (3, "failed"), (4, "failed"), (5, "failed")],
)
def test_breaking_edit_requires_test_failure_evidence(
    tmp_path: Path, edit_loop_benchmark: ModuleType, exit_code: int, outcome: str
) -> None:
    report = tmp_path / "outcomes.jsonl"
    report.write_text(
        "\n".join(
            json.dumps(["test_value", phase, outcome if phase == "call" else "passed"])
            for phase in ("setup", "call", "teardown")
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="did not produce a pytest test failure"):
        edit_loop_benchmark.assert_test_failure(
            subprocess.CompletedProcess(["pytest"], exit_code), report
        )


@pytest.mark.parametrize("phases", [[], ["call"], ["setup", "call"], ["setup", "call", "call"]])
def test_incomplete_test_protocol_cannot_validate_a_sample(
    tmp_path: Path, edit_loop_benchmark: ModuleType, phases: list[str]
) -> None:
    report = tmp_path / "outcomes.jsonl"
    report.write_text(
        "\n".join(json.dumps(["test_value", phase, "passed"]) for phase in phases),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError):
        edit_loop_benchmark.executed_outcomes(report)


def test_missing_databases_cannot_pass_coverage_parity(
    tmp_path: Path, edit_loop_benchmark: ModuleType
) -> None:
    with pytest.raises(RuntimeError, match="coverage data is missing"):
        edit_loop_benchmark.normalized_data(tmp_path / ".coverage", tmp_path)


@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_fixture_errors_cannot_prove_breaking_edit_detection(
    tmp_path: Path, edit_loop_benchmark: ModuleType, phase: str
) -> None:
    phases = {"setup": "passed", "call": "passed", "teardown": "passed"}
    phases[phase] = "failed"
    if phase == "setup":
        phases.pop("call")
    report = tmp_path / "outcomes.jsonl"
    report.write_text(
        "\n".join(json.dumps(["test_value", name, value]) for name, value in phases.items())
    )
    with pytest.raises(RuntimeError, match="fixture failure"):
        edit_loop_benchmark.assert_test_failure(subprocess.CompletedProcess(["pytest"], 1), report)


def test_completed_test_failure_proves_breaking_edit_detection(
    tmp_path: Path, edit_loop_benchmark: ModuleType
) -> None:
    report = tmp_path / "outcomes.jsonl"
    report.write_text(
        "\n".join(
            json.dumps(["test_value", phase, "failed" if phase == "call" else "passed"])
            for phase in ("setup", "call", "teardown")
        )
    )
    edit_loop_benchmark.assert_test_failure(subprocess.CompletedProcess(["pytest"], 1), report)


def test_call_after_skipped_setup_is_invalid_evidence(
    tmp_path: Path, edit_loop_benchmark: ModuleType
) -> None:
    report = tmp_path / "outcomes.jsonl"
    report.write_text(
        "\n".join(
            json.dumps(["test_value", phase, "skipped" if phase == "setup" else "passed"])
            for phase in ("setup", "call", "teardown")
        )
    )
    with pytest.raises(RuntimeError, match="pytest protocol"):
        edit_loop_benchmark.executed_outcomes(report)


def test_rust_boundary_publication_validate_only_without_build_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rust_boundary_benchmark: ModuleType
) -> None:
    benchmark = rust_boundary_benchmark
    monkeypatch.delenv("VFY_BENCHMARK_BUILD_MANIFEST", raising=False)
    monkeypatch.setattr(sys, "argv", ["boundary", "--validate-only", "--files", "1"])
    monkeypatch.setattr(benchmark, "require_clean", lambda _: SimpleNamespace(revision="a" * 40))
    monkeypatch.setattr(benchmark, "build_profile", lambda: "release")
    monkeypatch.setattr(benchmark, "parser_backend", lambda: "parser")
    monkeypatch.setattr(
        benchmark.tempfile, "TemporaryDirectory", lambda **_: contextlib.nullcontext(str(tmp_path))
    )
    monkeypatch.setattr(benchmark, "create_fixture", lambda *_a, **_k: tmp_path / "coverage")
    monkeypatch.setattr(
        benchmark, "CoverageStore", SimpleNamespace(read=lambda _: SimpleNamespace(files=[]))
    )
    visits: list[str] = []
    monkeypatch.setattr(benchmark, "build_rows", lambda *_a, **_k: visits.append("rows") or [])
    monkeypatch.setattr(benchmark, "row_signature", lambda _: ())
    monkeypatch.setattr(benchmark, "measure_rich_evidence", lambda *_: visits.append("rich") or {})
    monkeypatch.setattr(benchmark, "hardware_details", dict)
    monkeypatch.setattr(
        benchmark, "build_provenance", lambda *_a, **kw: {"publication": kw["publication"]}
    )
    result: dict[str, object] = {}
    monkeypatch.setattr(benchmark, "write_result", lambda _a, record, _r: result.update(record))

    assert benchmark.main() == 0
    assert visits.count("rows") == 4
    assert "rich" in visits
    assert result["validation_only"] is True
    assert result["provenance"] == {"publication": False}
