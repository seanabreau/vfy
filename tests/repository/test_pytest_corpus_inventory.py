"""Protect the pinned pytest collection inventory parser."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[2]


def load_inventory_module() -> ModuleType:
    path = ROOT / "scripts" / "pytest_corpus_inventory.py"
    spec = importlib.util.spec_from_file_location("pytest_corpus_inventory", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_log(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "collection.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_collection_log_preserves_order_and_multiplicity(tmp_path: Path) -> None:
    inventory = load_inventory_module()
    log = write_log(
        tmp_path,
        "testing/test_one.py::test_second",
        "testing/test_one.py::test_first",
        "testing/test_two.py::test_parametrized[value]",
        "3 tests collected in 0.01s",
    )

    result = inventory.validate_collection_log(
        log,
        {"testing/test_one.py", "testing/test_two.py"},
    )

    assert result["ordered_nodeids"] == [
        "testing/test_one.py::test_second",
        "testing/test_one.py::test_first",
        "testing/test_two.py::test_parametrized[value]",
    ]
    assert result["collected_items"] == 3
    assert (
        result["ordered_nodeids_sha256"]
        == "852c231e4d7a270aab6ea98aeed67547fc7d8c180d27b4b19654ba98144ccf9b"
    )


def test_collection_identity_rejects_changed_node_order_and_count(tmp_path: Path) -> None:
    inventory = load_inventory_module()
    sources = {"testing/test_one.py"}
    original = inventory.validate_collection_log(
        write_log(
            tmp_path,
            "testing/test_one.py::test_first",
            "testing/test_one.py::test_second",
            "2 tests collected in 0.01s",
        ),
        sources,
    )
    identity = {
        "schema_version": 1,
        "upstream_commit": inventory.PINNED_COMMIT,
        "node_count": 2,
        "ordered_nodeids_sha256": original["ordered_nodeids_sha256"],
    }
    inventory.validate_collection_identity(original, inventory.PINNED_COMMIT, identity)

    reordered = inventory.validate_collection_log(
        write_log(
            tmp_path,
            "testing/test_one.py::test_second",
            "testing/test_one.py::test_first",
            "2 tests collected in 0.01s",
        ),
        sources,
    )
    with pytest.raises(ValueError, match="ordered node IDs differ"):
        inventory.validate_collection_identity(reordered, inventory.PINNED_COMMIT, identity)

    identity["node_count"] = 3
    with pytest.raises(ValueError, match="node count differs"):
        inventory.validate_collection_identity(original, inventory.PINNED_COMMIT, identity)


def test_collection_identity_rejects_a_different_upstream_revision(
    tmp_path: Path,
) -> None:
    inventory = load_inventory_module()
    collection = inventory.validate_collection_log(
        write_log(tmp_path, "testing/test_one.py::test_case", "1 test collected in 0.01s"),
        {"testing/test_one.py"},
    )
    identity = {
        "schema_version": 1,
        "upstream_commit": inventory.PINNED_COMMIT,
        "node_count": 1,
        "ordered_nodeids_sha256": collection["ordered_nodeids_sha256"],
    }

    with pytest.raises(ValueError, match="identity commit does not match"):
        inventory.validate_collection_identity(collection, "different-revision", identity)


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (
            [
                "testing/test_one.py::test_case",
                "testing/test_one.py::test_case",
                "2 tests collected in 0.01s",
            ],
            "duplicate node IDs",
        ),
        (
            ["testing/test_one.py::test_case", "2 tests collected in 0.01s"],
            "contains 1 node IDs, summary reports 2",
        ),
        (
            ["testing/test_other.py::test_case", "1 test collected in 0.01s"],
            "collection sources differ",
        ),
    ],
)
def test_collection_log_rejects_untrustworthy_inventory(
    tmp_path: Path,
    lines: list[str],
    message: str,
) -> None:
    inventory = load_inventory_module()
    log = write_log(tmp_path, *lines)

    with pytest.raises(ValueError, match=message):
        inventory.validate_collection_log(log, {"testing/test_one.py"})
