from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from benchmarks import benchmark_projects, benchmark_workflows
from benchmarks.benchmark_harness import BenchmarkMode
from benchmarks.benchmark_projects import outcome_records
from benchmarks.benchmark_workflows import (
    validate_prepared_root,
)
from benchmarks.cli import compare_results
from benchmarks.project_manifest import manifest

REVISION = "a" * 40


def _args(**values: object) -> SimpleNamespace:
    return SimpleNamespace(**values)


def _fixed_args(value: SimpleNamespace) -> Callable[[], SimpleNamespace]:
    def parse_args() -> SimpleNamespace:
        return value

    return parse_args


def _workflow_args(
    root: Path, python: Path, preparation_manifest: Path, artifacts: Path, output: Path
) -> SimpleNamespace:
    return _args(
        project="rich",
        root=root,
        python=python,
        preparation_manifest=preparation_manifest,
        artifacts=artifacts,
        cov_config=None,
        vfy_revision=REVISION,
        output=output,
        validate_only=False,
        warmups=3,
        runs=15,
        mode=BenchmarkMode.PUBLICATION.value,
    )


def _project_args(
    root: Path, python: Path, preparation_manifest: Path, artifacts: Path, output: Path
) -> SimpleNamespace:
    return _args(
        project="rich",
        phase=None,
        root=root,
        python=str(python),
        preparation_manifest=preparation_manifest,
        warmups=3,
        runs=15,
        mode=BenchmarkMode.PUBLICATION.value,
        output=output,
        artifacts=artifacts,
        vfy_revision=REVISION,
        validate_only=False,
        resources=False,
        tree_resources=False,
        tests=None,
        pytest_args="",
        cov_config=None,
        report=None,
        context=None,
        source=None,
        scope=None,
    )


def _compare_producer_outputs(
    tmp_path: Path, produce: Callable[[Path, Path], None]
) -> dict[str, object]:
    candidate = tmp_path / "candidate.json"
    baseline = tmp_path / "baseline.json"
    produce(candidate, tmp_path / "candidate-artifacts")
    produce(baseline, tmp_path / "baseline-artifacts")
    return compare_results(candidate, baseline)


def _publication_provenance(workload: object) -> dict[str, object]:
    return {
        "source": {"revision": REVISION, "dirty": False},
        "environment": {
            "platform": "test-platform",
            "machine": "test-machine",
            "cpu": "test-cpu",
            "logical_cpus": 8,
            "memory_bytes": 16 * 1024**3,
            "python_implementation": "CPython",
            "python_build": "test-build",
            "python_abi": "cpython-312",
            "python_debug": False,
            "python_gil": "enabled",
            "tools": {"python": "3.12.0", "pytest": "8.0", "pytest-cov": "6.0"},
            "artifact": {
                "native_profile": "release",
                "build_manifest": "/tmp/vfy-build.json",
                "wheel_sha256": "b" * 64,
                "extension_sha256": "c" * 64,
                "source_revision": REVISION,
            },
        },
        "workload": workload,
        "workers": 2,
        "cache": "controlled",
        "engine_source": {"revision": REVISION},
        "validation": {"status": "passed", "evidence": "test"},
    }


def test_subtest_outcomes_keep_distinct_call_identities(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.jsonl"
    records = [
        {"nodeid": "tests/test_example.py::test_one", "when": "setup", "outcome": "passed"},
        {"nodeid": "tests/test_example.py::test_one", "when": "call", "outcome": "passed"},
        {"nodeid": "tests/test_example.py::test_one", "when": "teardown", "outcome": "passed"},
        {
            "nodeid": 'tests/test_example.py::test_one::[subtest]{"kwargs":{"i":"0"},"msg":null}',
            "when": "call",
            "outcome": "passed",
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    parsed = outcome_records(path)
    assert sum("::[subtest]" in nodeid for nodeid, *_rest in parsed) == 1


def test_vfy_sequence_clears_reference_caches_before_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "package" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"cache")
    pytest_cache = tmp_path / ".pytest_cache"
    pytest_cache.mkdir()
    events: list[str] = []
    clear = benchmark_workflows.clear_measured_caches

    def clear_before_timer(root: Path) -> None:
        events.append("clear")
        clear(root)

    monkeypatch.setattr(benchmark_workflows, "clear_measured_caches", clear_before_timer)
    monkeypatch.setattr(
        benchmark_workflows,
        "_vfy_command",
        lambda *args, **kwargs: (
            [],
            {},
            tmp_path / "outcomes.jsonl",
            tmp_path / "report.json",
            tmp_path / "coverage",
        ),
    )
    monkeypatch.setattr(
        benchmark_workflows,
        "_run_child",
        lambda *args, **kwargs: (
            events.append("timed") or 1.0,
            subprocess.CompletedProcess([], 0, "", ""),
        ),
    )
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    benchmark_workflows._run_vfy(
        tmp_path,
        manifest("rich"),
        tmp_path / "python",
        phases=("parallel",),
        artifacts=artifacts,
        label="baseline",
        cov_config=None,
        measure=True,
    )
    assert events == ["clear", "timed"]
    assert not cache.exists()
    assert not pytest_cache.exists()


def test_workflow_producer_output_round_trips_through_compare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    python = tmp_path / "python"
    prepared = {
        "manifest": str(tmp_path / "artifacts" / "manifest.json"),
        "vfy_build": {"source_revision": REVISION},
        "interpreter": {"python": "3.12.0"},
    }
    monkeypatch.setattr(benchmark_workflows, "validate_prepared_root", lambda *args: prepared)
    monkeypatch.setattr(
        benchmark_workflows,
        "prepared_project_provenance",
        lambda _root, **kwargs: _publication_provenance(kwargs["workload"]),
    )
    monkeypatch.setattr(
        benchmark_workflows,
        "run_sequence",
        lambda *args, **kwargs: (
            args[3].mkdir(parents=True, exist_ok=True)
            or {"sequence_elapsed_ms": 10.0 if kwargs.get("measure", True) else 0.0}
        ),
    )

    def produce(output: Path, artifacts: Path) -> None:
        monkeypatch.setattr(
            benchmark_workflows,
            "parse_args",
            _fixed_args(
                _workflow_args(root, python, tmp_path / "manifest.json", artifacts, output)
            ),
        )
        assert benchmark_workflows.main() == 0

    report = _compare_producer_outputs(tmp_path, produce)
    assert report["summary"] == {"chart_labels": ["sequence"]}


def test_project_producer_output_round_trips_through_compare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    generated = root / "generated.txt"
    generated.write_text("original", encoding="utf-8")
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    prepared = {
        "manifest": str(tmp_path / "artifacts" / "manifest.json"),
        "vfy_wheel": {
            "source_revision": REVISION,
            "extension_sha256": "c" * 64,
            "native_profile": "release",
        },
    }
    monkeypatch.setattr(benchmark_projects.platform, "platform", lambda: "test-platform")
    monkeypatch.setattr(benchmark_projects.platform, "machine", lambda: "test-machine")
    monkeypatch.setattr(benchmark_projects.platform, "processor", lambda: "")
    selected = {
        "python": "3.12.0",
        "implementation": "CPython",
        "vfy": "0.1",
        "pytest_cov": "6.0",
        "extension_sha256": "c" * 64,
        "build_profile": "release",
    }
    monkeypatch.setattr(
        benchmark_projects, "selected_interpreter_metadata", lambda *args, **kwargs: selected
    )
    monkeypatch.setattr(
        benchmark_projects, "preparation_metadata", lambda *args, **kwargs: prepared
    )
    monkeypatch.setattr(benchmark_projects, "validate_parity", lambda *args, **kwargs: ({}, []))
    monkeypatch.setattr(
        benchmark_projects,
        "prepared_project_provenance",
        lambda _root, **kwargs: _publication_provenance(kwargs["workload"]),
    )
    monkeypatch.setattr(benchmark_projects, "git_value", lambda *args: REVISION)
    monkeypatch.setitem(
        benchmark_projects.PROJECTS,
        "rich",
        replace(benchmark_projects.PROJECTS["rich"], generated_files=("generated.txt",)),
    )
    observed: list[str] = []

    def fake_timed_sequence(*_args: object, **_kwargs: object) -> tuple[float, dict[str, float]]:
        observed.append(generated.read_text(encoding="utf-8"))
        generated.write_text("changed", encoding="utf-8")
        return 10.0, {"parallel": 10.0}

    monkeypatch.setattr(benchmark_projects, "timed_sequence", fake_timed_sequence)
    monkeypatch.setattr(benchmark_projects, "validate_sample", lambda *args, **kwargs: None)

    def produce(output: Path, artifacts: Path) -> None:
        monkeypatch.setattr(
            benchmark_projects,
            "parse_args",
            _fixed_args(_project_args(root, python, tmp_path / "manifest.json", artifacts, output)),
        )
        assert benchmark_projects.main() == 0

    report = _compare_producer_outputs(tmp_path, produce)
    summary = report["summary"]
    assert isinstance(summary, dict)
    labels = summary["chart_labels"]
    assert labels == [
        "plain pytest",
        "pytest-cov",
        "steps.plain pytest.parallel",
        "steps.pytest-cov.parallel",
        "steps.vfy.parallel",
        "vfy",
    ]
    assert set(observed) == {"original"}
    assert generated.read_text(encoding="utf-8") == "original"


def test_workflow_rejects_artifacts_inside_root_before_reset(tmp_path: Path) -> None:
    destination = tmp_path / "prepared"
    root = destination / "project"
    root.mkdir(parents=True)
    sentinel = root / "keep.txt"
    sentinel.write_text("do not remove", encoding="utf-8")
    manifest_path = destination / "artifacts" / "manifest.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text(json.dumps({"project": "rich"}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="artifacts must be outside"):
        validate_prepared_root(
            manifest("rich"),
            root,
            destination / "environment" / "bin" / "python",
            manifest_path,
            root / "artifacts",
        )
    assert sentinel.read_text(encoding="utf-8") == "do not remove"
