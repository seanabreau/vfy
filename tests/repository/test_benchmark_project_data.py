"""Sparse benchmark snapshots retain exact context attribution."""

from __future__ import annotations

from pathlib import Path

from benchmarks.benchmark_projects import normalized_data
from coverage import CoverageData


def test_context_snapshot_retains_entry_exit_arcs_and_empty_files(tmp_path: Path) -> None:
    path = tmp_path / ".coverage"
    first, second, empty = (str(tmp_path / name) for name in ("a.py", "b.py", "empty.py"))
    data = CoverageData(str(path))
    data.set_context("test-one")
    data.add_arcs({first: [(-1, 1), (1, -1)], second: [(-1, -1)]})
    data.set_context("test-two")
    data.add_arcs({first: [(-1, 2), (2, -1)]})
    data.touch_file(empty)
    data.write()

    snapshot = normalized_data(path, tmp_path)
    assert snapshot["by_context"] == {
        "test-one": {
            "a.py": {"lines": [1], "arcs": [(-1, 1), (1, -1)]},
            "b.py": {"lines": [], "arcs": [(-1, -1)]},
        },
        "test-two": {"a.py": {"lines": [2], "arcs": [(-1, 2), (2, -1)]}},
    }
    assert set(snapshot["files"]) == {"a.py", "b.py", "empty.py"}
    assert snapshot["files"]["a.py"]["contexts"] == {1: ["test-one"], 2: ["test-two"]}
