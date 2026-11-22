"""Staged benchmark plug-ins retain canonical sources and reject changed artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from benchmarks.observer_plugins import PLUGIN_RECEIPT, stage_plugins


def test_staged_plugins_keep_verified_canonical_sources(tmp_path: Path) -> None:
    destination = stage_plugins(tmp_path / "observer-path")
    receipt_path = destination / PLUGIN_RECEIPT
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    assert not (destination / "__init__.py").exists()
    assert (destination / receipt["package"] / "__init__.py").read_bytes() == b""
    sources = []
    for file in receipt["files"].values():
        derived = Path(file["derived"]).read_bytes()
        assert hashlib.sha256(derived).hexdigest() == file["derived_sha256"]
        assert file["source_sha256"] == file["derived_sha256"]
        if file["source"] is not None:
            source = Path(file["source"])
            assert source.read_bytes() == derived
            sources.append(source.name)
    assert len(sources) == 4
    assert stage_plugins(destination) == destination
    assert receipt_path.read_bytes() == receipt_bytes


@pytest.mark.parametrize(
    "changed", ["vfy_benchmark_plugins/outcome_plugin.py", PLUGIN_RECEIPT, "__init__.py"]
)
def test_staging_rejects_changed_artifacts_without_repair(tmp_path: Path, changed: str) -> None:
    destination = stage_plugins(tmp_path / "observer-path")
    target = destination / changed
    target.write_bytes(b"changed\n")
    with pytest.raises(ValueError, match=r"changed|must not contain __init__"):
        stage_plugins(destination)
    assert target.read_bytes() == b"changed\n"
