"""Tests for the asyncio benchmark's parity gates."""

from __future__ import annotations

from pathlib import Path

import coverage
import pytest
from benchmarks.benchmark_asyncio import (
    collection_command,
    collection_ids,
    create_fixture,
    environment,
    normalized_data,
    run,
    workflow_command,
)


def test_collection_ids_ignore_pytest_elapsed_time() -> None:
    node_id = "test_async.py::test_value[param collected in label]"
    first = f"{node_id}\n\n1 test collected in 0.00s\n"
    second = f"{node_id}\n\n1 test collected in 0.06s\n"

    assert collection_ids(first) == collection_ids(second) == (node_id,)


@pytest.mark.parametrize("count", [1, 17])
def test_parametrized_workload_collects_the_requested_number_of_tests(
    tmp_path: Path, count: int
) -> None:
    test_file = create_fixture(tmp_path, "parametrized", count, "vfy", "async_fixture")
    output = run(
        collection_command(test_file, "vfy"),
        cwd=tmp_path,
        env=environment(tmp_path / ".coverage"),
    )
    assert len(collection_ids(output)) == count


def test_missing_coverage_cannot_pass_the_benchmark_parity_gate(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="coverage data was not produced"):
        normalized_data(tmp_path / ".coverage", tmp_path)


def test_empty_coverage_cannot_pass_the_benchmark_parity_gate(tmp_path: Path) -> None:
    path = tmp_path / ".coverage"
    data = coverage.CoverageData(basename=str(path))
    data.add_arcs({})
    data.write()
    with pytest.raises(RuntimeError, match="per-test execution contexts"):
        normalized_data(path, tmp_path)


def test_full_workflow_uses_a_fresh_serial_complete_vfy_run(tmp_path: Path) -> None:
    test_file = tmp_path / "test_workload.py"
    history = tmp_path / ".vfy/test-history.json"

    vfy = workflow_command(test_file, "vfy", history)
    upstream = workflow_command(test_file, "pytest-asyncio", history)

    assert vfy[3:9] == ["test", "--engine=pytest-native", "--all", "-j", "1", "--history"]
    assert str(test_file) not in vfy
    assert "pytest_asyncio.plugin" in upstream
    assert str(test_file) in upstream
