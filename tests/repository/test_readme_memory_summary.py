"""Combine retained full-suite serial and native routes without hiding failed evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from scripts.summarize_readme_benchmarks import build_summary

from tests.repository.test_memory_benchmark_summary import _fixture as serial_fixture
from tests.repository.test_parallel_evidence_record import _fixture as parallel_fixture


def _write(path: Path, value: dict[str, Any]) -> str:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _serial(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name / "serial"
    root.mkdir(parents=True)
    path = serial_fixture(root)
    record = json.loads(path.read_text())
    record["project"]["key"] = name
    record["project"]["name"] = name.title() if name != "attrs" else "attrs"
    record["environment"] = {
        "python": "3.12.14",
        "implementation": "CPython",
        "pytest_cov": "7.1.0",
        "vfy": "0.0.1",
    }
    record["provenance"]["environment"] = {
        "tools": {"python": "3.12.14", "pytest": "9.1.1", "pytest-cov": "7.1.0", "vfy": "0.0.1"}
    }
    method = record["methodology"]["resources"]
    method["method"] = "linux-process-tree-smaps-rollup"
    method["rss_scope"] = "sampled sum of simultaneous live child and descendant PSS"
    parity_path = Path(record["resource_parity_artifact"]["path"])
    parity = json.loads(parity_path.read_text())
    parity["project"] = record["project"]["name"]
    parity["methodology"] = record["methodology"]
    parity["provenance"] = record["provenance"]
    parity["tests"] = ["tests"]
    record["resource_parity_artifact"]["sha256"] = _write(parity_path, parity)
    outcome = parity_path.parent / "pytest-cov-serial-outcomes.jsonl"
    outcome.write_text(
        "".join(
            json.dumps({"nodeid": "tests/test_case.py::test_ok", "when": when, "outcome": "passed"})
            + "\n"
            for when in ("setup", "call", "teardown")
        ),
        encoding="utf-8",
    )
    for tool, series in record["resources"].items():
        record[tool]["median_ms"] = 10.5
        peaks = []
        for item in series["records"]:
            raw_path = Path(item["path"])
            raw = json.loads(raw_path.read_text())
            raw.update(
                method="linux-process-tree-smaps-rollup",
                tree_scope="sum of simultaneous live child and descendant PSS; collector excluded",
                sample_interval_ms=20,
                snapshot_count=3,
                peak_tree_pss_bytes=raw["peak_rss_bytes"] // 2,
                peak_tree_rss_bytes=raw["peak_rss_bytes"],
                peak_process_count=1,
                tracked_process_count=1,
                max_sample_gap_ms=20.0,
            )
            peaks.append(raw["peak_tree_pss_bytes"])
            item.update(raw)
            item["sha256"] = _write(raw_path, raw)
        series["samples_peak_tree_pss_bytes"] = peaks
        series["median_peak_tree_pss_bytes"] = sum(peaks) / 2
        assert tool in {"plain pytest", "vfy", "pytest-cov"}
    _write(path, record)
    return path


def _parallel(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name / "parallel"
    root.mkdir(parents=True)
    path = parallel_fixture(root, name, "d" * 64)
    record = json.loads(path.read_text())
    record["method"].update(
        timing_routes=["stock-xdist-4", "pytest-native-j4"],
        override_upstream_capture=True,
        resource_method="linux-process-tree-smaps-rollup",
    )
    record["source_input_sha256"]["validation/pytest_runner/benchmark_child_resources.py"] = (
        "f" * 64
    )
    identity_path = path.parent / name / "identity.json"
    identity = json.loads(identity_path.read_text())
    identity["build"]["native_profile"] = "release"
    identity["build"]["source_revision"] = "b" * 40
    identity["prepared"]["revision"] = "c" * 40
    identity["installed"] = {
        "python": "3.12.14",
        "implementation": "CPython",
        "vfy": "0.0.1",
        "dependencies": [["pytest", "9.1.1"], ["pytest-cov", "7.1.0"]],
    }
    _write(identity_path, identity)
    project = record["projects"][0]
    samples = [
        sample
        for sample in project["timing"]["samples"]
        if sample["route"] in {"stock-xdist-4", "pytest-native-j4"}
    ]
    project["timing"]["samples"] = samples
    project["timing"]["summary"].pop("pytest-native-xdist-4")
    for sample in samples:
        route, index = sample["route"], sample["index"]
        directory = path.parent / name / "timing"
        command = record["projects"][0]["parity"][route]["artifacts"]["command.json"]
        argv = json.loads(Path(command["path"]).read_text())["argv"]
        status = directory / f"{route}-{index}-status.json"
        command_path = directory / f"{route}-{index}-command.json"
        raw_path = directory / f"{route}-{index}-resources.json"
        raw = {
            "schema_version": 1,
            "method": "linux-process-tree-smaps-rollup",
            "tree_scope": "sum of simultaneous live child and descendant PSS; collector excluded",
            "sample_interval_ms": 20,
            "snapshot_count": 3,
            "peak_tree_pss_bytes": 1000 + index * 200,
            "peak_tree_rss_bytes": 2000 + index * 200,
            "peak_process_count": 2,
            "tracked_process_count": 2,
            "max_sample_gap_ms": 20.0,
            "collector_sha256": "f" * 64,
            "child_exit_status": 0,
            "child_wall_ms": sample["seconds"] * 1000,
            "child_command": argv,
        }
        sample["resources"] = raw
        sample["artifacts"].update(
            {
                "status.json": {
                    "path": str(status),
                    "retained_sha256": _write(status, {"completed": True, "exit_status": 0}),
                },
                "command.json": {
                    "path": str(command_path),
                    "retained_sha256": _write(command_path, {"argv": argv}),
                },
                "resources.json": {"path": str(raw_path), "retained_sha256": _write(raw_path, raw)},
            }
        )
    _write(path.parent / name / "timing" / "samples.json", {"samples": samples})
    _write(path, record)
    return path


def _paired(tmp_path: Path) -> tuple[list[Path], list[Path]]:
    names = ("packaging", "rich", "attrs")
    return (
        [_serial(tmp_path, name) for name in names],
        [_parallel(tmp_path, name) for name in names],
    )


def test_readme_summary_contains_only_five_parity_checked_routes(tmp_path: Path) -> None:
    serial, parallel = _paired(tmp_path)
    summary = build_summary(serial, parallel)
    assert summary["schema"] == 1
    assert summary["metric"] == "peak_tree_pss_bytes"
    assert summary["method"] == "linux-process-tree-smaps-rollup"
    assert summary["sample_interval_ms"] == 20
    assert [row["project"] for row in summary["rows"]] == ["Packaging", "Rich", "attrs"]
    for row in summary["rows"]:
        assert row["cases"] == 1
        assert row["parity"] == "exact"
        assert set(row["routes"]) == {
            "plain pytest",
            "vfy",
            "pytest-cov",
            "stock-xdist-4",
            "pytest-native-j4",
        }
        assert all(
            len(route["peak_tree_pss_samples_bytes"]) == 2 for route in row["routes"].values()
        )
        assert "artifacts" not in json.dumps(row)


def test_readme_summary_rejects_tampered_native_sample(tmp_path: Path) -> None:
    serial, parallel = _paired(tmp_path)
    path = parallel[0]
    record = json.loads(path.read_text())
    sample = record["projects"][0]["timing"]["samples"][0]
    sample["resources"]["peak_tree_pss_bytes"] = 999999
    _write(
        path.parent / "packaging" / "timing" / "samples.json",
        {"samples": record["projects"][0]["timing"]["samples"]},
    )
    _write(path, record)
    with pytest.raises(ValueError, match="resource record differs"):
        build_summary(serial, parallel)


def test_readme_summary_rejects_same_count_serial_case_change(tmp_path: Path) -> None:
    serial, parallel = _paired(tmp_path)
    record = json.loads(serial[0].read_text())
    parity_path = Path(record["resource_parity_artifact"]["path"])
    outcomes_path = parity_path.parent / "pytest-cov-serial-outcomes.jsonl"
    outcomes = [json.loads(line) for line in outcomes_path.read_text().splitlines()]
    for outcome in outcomes:
        outcome["nodeid"] = "tests/test_case.py::test_changed"
    outcomes_path.write_text(
        "\n".join(json.dumps(outcome) for outcome in outcomes) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="serial and parallel full-suite identities"):
        build_summary(serial, parallel)
