"""Validate process-tree resource evidence from the committed Linux collector."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from scripts.process_tree import run_captured

from benchmarks.benchmark_harness import benchmark_environment


def tree_resource_methodology() -> dict[str, object]:
    """Describe the sampled memory boundary separately from workflow timing."""
    return {
        "method": "linux-process-tree-smaps-rollup",
        "sample_interval_ms": 20,
        "memory": "peak sum of simultaneous live public-command and descendant PSS",
        "boundary": "public child command; excludes controller, collector, and helper preparation",
    }


def tree_resource_process(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
    record_path: Path,
    timeout: float = 300,
) -> tuple[float, subprocess.CompletedProcess[str], dict[str, object]]:
    """Retain streams and validate the exact child before returning its status."""
    if sys.platform != "linux":
        raise RuntimeError("process-tree resource measurement requires Linux")
    record_path = record_path.absolute()
    record_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = record_path.with_suffix(".stdout")
    stderr_path = record_path.with_suffix(".stderr")
    for path in (record_path, stdout_path, stderr_path):
        if path.exists():
            raise FileExistsError(f"resource artifact already exists: {path}")
    collector = (
        Path(__file__).resolve().parents[1]
        / "validation/pytest_runner/benchmark_child_resources.py"
    )
    argv = [str(part) for part in command]
    wrapper = [
        sys.executable,
        str(collector),
        "--tree",
        "--sample-interval-ms",
        "20",
        "--record",
        str(record_path),
        "--stdout",
        str(stdout_path),
        "--stderr",
        str(stderr_path),
        "--",
        *argv,
    ]
    completed = run_captured(
        wrapper,
        cwd=cwd,
        env=benchmark_environment() if env is None else dict(env),
        timeout=timeout,
    )
    if not record_path.is_file():
        raise RuntimeError(
            f"process-tree resource collector produced no record: {completed.stderr}"
        )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != 1
        or record.get("method") != "linux-process-tree-smaps-rollup"
        or record.get("collector_sha256") != hashlib.sha256(collector.read_bytes()).hexdigest()
        or record.get("child_command") != argv
        or type(record.get("child_exit_status")) is not int
        or record["child_exit_status"] != completed.returncode
        or record.get("sample_interval_ms") != 20
    ):
        raise RuntimeError("process-tree resource record identifies another child or collector")
    for field in (
        "child_wall_ms",
        "user_cpu_ms",
        "system_cpu_ms",
        "total_cpu_ms",
        "max_sample_gap_ms",
    ):
        value = record.get(field)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise RuntimeError(f"invalid process-tree resource field: {field}")
    for field in (
        "peak_rss_bytes",
        "peak_tree_pss_bytes",
        "peak_tree_rss_bytes",
        "snapshot_count",
        "peak_process_count",
        "tracked_process_count",
    ):
        if type(record.get(field)) is not int or record[field] < 1:
            raise RuntimeError(f"invalid process-tree resource field: {field}")
    if (
        record["child_wall_ms"] <= 0
        or record["peak_tree_rss_bytes"] < record["peak_tree_pss_bytes"]
    ):
        raise RuntimeError("invalid process-tree wall time or memory totals")
    if not math.isclose(record["total_cpu_ms"], record["user_cpu_ms"] + record["system_cpu_ms"]):
        raise RuntimeError("process-tree total CPU differs from user and system CPU")
    child = subprocess.CompletedProcess(
        argv,
        completed.returncode,
        stdout_path.read_text(encoding="utf-8"),
        stderr_path.read_text(encoding="utf-8"),
    )
    return float(record["child_wall_ms"]), child, cast("dict[str, object]", record)
