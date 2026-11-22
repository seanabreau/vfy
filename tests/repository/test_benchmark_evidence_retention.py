from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from benchmarks import benchmark_evidence
from benchmarks.benchmark_evidence import bench_executable_record, load_target


def test_load_target_retains_complete_criterion_json(tmp_path: Path) -> None:
    target = tmp_path / "target" / "criterion" / "trace" / "new"
    target.mkdir(parents=True)
    raw = {
        "benchmark.json": {"group_id": "trace", "full_id": "trace/new"},
        "estimates.json": {
            "mean": {"point_estimate": 10.0},
            "median": {"point_estimate": 10.0},
            "unit": "ns",
        },
        "sample.json": {
            "sampling_mode": "Linear",
            "times": [10, 20],
            "iters": [1, 2],
            "version": "0.5.1",
        },
    }
    for filename, payload in raw.items():
        (target / filename).write_text(json.dumps(payload), encoding="utf-8")

    result = load_target(tmp_path, Path("trace/new"))

    assert result["criterion_raw"] == raw


def test_bench_executable_record_hashes_selected_compiler_artifact(tmp_path: Path) -> None:
    executable = tmp_path / "target" / "release" / "deps" / "evidence-abc123"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"actual evidence executable")
    events = [
        json.dumps(
            {
                "reason": "compiler-artifact",
                "target": {"name": "vfy-core", "kind": ["lib"]},
                "executable": str(tmp_path / "wrong"),
            }
        ),
        "Criterion output that is not Cargo JSON",
        json.dumps(
            {
                "reason": "compiler-artifact",
                "target": {"name": "evidence", "kind": ["bench"]},
                "executable": str(executable),
            }
        ),
    ]

    record = bench_executable_record(tmp_path, "\n".join(events))

    assert record == {
        "target": "evidence",
        "kind": ["bench"],
        "path": "target/release/deps/evidence-abc123",
        "sha256": hashlib.sha256(b"actual evidence executable").hexdigest(),
    }


def test_criterion_uses_checkout_target_even_with_inherited_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "shared"))
    seen: dict[str, object] = {}

    def run(command: object, *, cwd: Path, env: dict[str, str]) -> SimpleNamespace:
        seen.update(command=command, cwd=cwd, target=env["CARGO_TARGET_DIR"])
        return SimpleNamespace(stdout="events")

    monkeypatch.setattr(benchmark_evidence, "run_checked", run)
    monkeypatch.setattr(benchmark_evidence, "bench_executable_record", lambda *_: {})
    monkeypatch.setattr(benchmark_evidence, "discover_targets", lambda *_: {})
    _, executable = benchmark_evidence.run_criterion(tmp_path, ("cargo", "bench"))
    assert seen["target"] == str(tmp_path / "target")
    assert executable["cargo_target_dir"] == seen["target"]
