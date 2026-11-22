"""Publication analysis rejects changed artifacts, routes, and paired blocks."""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path
from typing import Any

import pytest
from scripts.native_perf_evidence import Reader, _junit, validate_command, validate_rotation


def _samples(*, memory: bool = False) -> list[dict[str, Any]]:
    orders = (
        ("stock-xdist-4", "pytest-native-xdist-4", "pytest-native-j4"),
        ("pytest-native-xdist-4", "pytest-native-j4", "stock-xdist-4"),
        ("pytest-native-j4", "stock-xdist-4", "pytest-native-xdist-4"),
    )
    rows = []
    for phase, count in (("warmup", 3), ("sample", 15)):
        for index in range(count):
            for route in orders[index % 3]:
                row: dict[str, Any] = {
                    "phase": phase,
                    "index": index,
                    "route": route,
                    "seconds": 1.25,
                    "native_environment": {},
                }
                if memory:
                    row["resources"] = {
                        "method": "linux-process-tree-smaps-rollup",
                        "sample_interval_ms": 20,
                        "child_exit_status": 0,
                        "child_wall_ms": 1250,
                        "peak_tree_pss_bytes": 1_048_576,
                    }
                rows.append(row)
    return rows


@pytest.mark.parametrize("memory", [False, True])
def test_publication_blocks_reject_duplicate_measured_round(memory: bool) -> None:
    rows = _samples(memory=memory)
    validate_rotation(rows, memory=memory)
    rows[-1]["index"] = 13
    with pytest.raises(ValueError, match="experimental blocks"):
        validate_rotation(rows, memory=memory)


def test_publication_blocks_reject_changed_route_rotation() -> None:
    rows = _samples()
    rows[10], rows[11] = rows[11], rows[10]
    with pytest.raises(ValueError, match="experimental blocks"):
        validate_rotation(rows, memory=False)


def test_memory_rows_cannot_enter_timing_only_cohort() -> None:
    rows = _samples()
    rows[-1]["resources"] = _samples(memory=True)[-1]["resources"]
    with pytest.raises(ValueError, match="cannot be mixed"):
        validate_rotation(rows, memory=False)


def test_missing_memory_row_cannot_enter_sampled_cohort() -> None:
    rows = _samples(memory=True)
    del rows[-1]["resources"]
    with pytest.raises(ValueError, match="cannot be mixed"):
        validate_rotation(rows, memory=True)


def test_same_command_with_different_native_flags_cannot_be_paired() -> None:
    rows = _samples()
    rows[-1]["native_environment"] = {"VFY_BENCHMARK_FLAG": "1"}
    with pytest.raises(ValueError, match="experiment flags"):
        validate_rotation(rows, memory=False)


def _command(worker_options: list[str]) -> dict[str, Any]:
    return {
        "cwd": "/project",
        "argv": [
            "/env/bin/python",
            "-m",
            "vfy",
            "test",
            *worker_options,
            "--no-cov",
            "--capture=no",
            "--junitxml=/artifacts/junit.xml",
            "-o",
            "junit_logging=all",
            "-o",
            "junit_log_passing_tests=true",
            "tests",
        ],
    }


def test_file_scheduler_cannot_be_reported_as_bridge() -> None:
    command = _command(["--workers=4"])
    validate_command(command, "pytest-native-j4", ["tests"])
    with pytest.raises(ValueError, match="declared route"):
        validate_command(command, "pytest-native-xdist-4", ["tests"])


def test_bridge_requires_four_actual_xdist_workers() -> None:
    validate_command(
        _command(["--numprocesses", "4", "--dist=load"]), "pytest-native-xdist-4", ["tests"]
    )
    with pytest.raises(ValueError, match="declared route"):
        validate_command(
            _command(["--numprocesses", "8", "--dist=load"]), "pytest-native-xdist-4", ["tests"]
        )
    with pytest.raises(ValueError, match="declared route"):
        validate_command(
            _command(["--numprocesses", "4", "--numprocesses=8", "--dist=load"]),
            "pytest-native-xdist-4",
            ["tests"],
        )


def test_compressed_artifact_requires_matching_raw_and_retained_hashes(tmp_path: Path) -> None:
    payload = b'<testsuite><testcase classname="tests.test_case" name="test_ok"/></testsuite>'
    retained = gzip.compress(payload)
    path = tmp_path / "junit.xml.gz"
    path.write_bytes(retained)
    record = {
        "path": str(path),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "retained_bytes": len(retained),
        "retained_sha256": hashlib.sha256(retained).hexdigest(),
    }
    assert Reader().artifact(record) == payload
    record["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="raw artifact changed"):
        Reader().artifact(record)


def test_junit_gate_keeps_duplicate_cases_properties_and_diagnostics() -> None:
    case = (
        '<testcase classname="tests.test_case" name="test_case">'
        '<properties><property name="key" value="value"/></properties>'
        '<failure message="failed">assert expected == actual</failure>'
        "<system-out>captured output</system-out></testcase>"
    )
    command = _command(["--workers=4"])
    baseline = _junit(f"<testsuite>{case}{case}</testsuite>".encode(), command)
    diagnostic = case.replace("expected == actual", "different diagnostic")
    for changed in (
        f"<testsuite>{case}</testsuite>",
        f"<testsuite>{case}{case.replace('value=', 'other=')}</testsuite>",
        f"<testsuite>{case}{diagnostic}</testsuite>",
        f"<testsuite>{case}{case.replace('captured output', 'different capture')}</testsuite>",
    ):
        assert _junit(changed.encode(), command) != baseline
