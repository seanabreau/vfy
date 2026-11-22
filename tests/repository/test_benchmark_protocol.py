"""Benchmark records must not certify absent or contaminated evidence."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from benchmarks import benchmark_provenance
from benchmarks.benchmark_harness import (
    benchmark_environment,
    paired_uncertainty,
    run_checked,
    summarize,
    timed,
)
from benchmarks.cli import compare_results

from tests.support import VfyProject


@pytest.mark.parametrize("timed_run", [False, True])
def test_failed_benchmark_retains_both_output_streams(tmp_path: Path, timed_run: bool) -> None:
    probe = VfyProject(tmp_path).write(
        "failure.py",
        "import sys\nprint('test assertion failed')\n"
        "print('separate warning', file=sys.stderr)\nsys.exit(1)\n",
    )
    runner = timed if timed_run else run_checked
    with pytest.raises(RuntimeError) as failure:
        runner([sys.executable, str(probe)], cwd=tmp_path, timeout=10)
    assert "test assertion failed" in str(failure.value)
    assert "separate warning" in str(failure.value)


def test_benchmark_child_uses_the_supplied_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VFY_REVIEW_AMBIENT", "must-not-leak")
    project = VfyProject(tmp_path)
    probe = project.write(
        "probe.py",
        "import os\nprint(os.environ.get('VFY_REVIEW_AMBIENT', 'absent'))\n",
    )
    completed = run_checked(
        [sys.executable, str(probe)], cwd=tmp_path, env={"PATH": os.defpath}, timeout=10
    )
    assert completed.stdout.strip() == "absent"


def test_benchmark_environment_removes_ambient_tool_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poisoned = (
        "COVERAGE_PROCESS_START",
        "COVERAGE_FILE",
        "PYTEST_ADDOPTS",
        "PYTHONPATH",
        "PYTHONHOME",
        "GIT_DIR",
        "UV_PROJECT_ENVIRONMENT",
        "VIRTUAL_ENV",
    )
    for name in poisoned:
        monkeypatch.setenv(name, "must-not-leak")
    project = VfyProject(tmp_path)
    probe = project.write(
        "probe.py",
        f"import os\nassert not set({poisoned!r}).intersection(os.environ)\n",
    )
    run_checked(
        [sys.executable, str(probe)],
        cwd=tmp_path,
        env=benchmark_environment(),
        timeout=10,
    )


@pytest.mark.parametrize("samples", [[], [-1.0], [float("nan")], [float("inf")]])
def test_invalid_samples_cannot_produce_a_timing_summary(samples: list[float]) -> None:
    with pytest.raises(ValueError, match="sample"):
        summarize(samples)


def test_one_diagnostic_sample_does_not_claim_statistical_certainty() -> None:
    result = paired_uncertainty([1.0], [2.0])
    assert result["ci95_low_ms"] is None
    assert result["ci95_high_ms"] is None


def test_missing_git_status_is_not_a_clean_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def git_value(root: Path, *arguments: str) -> str | None:
        return "a" * 40 if arguments[0] == "rev-parse" else None

    monkeypatch.setattr(benchmark_provenance, "git_value", git_value)
    with pytest.raises(RuntimeError):
        benchmark_provenance.source_state(tmp_path)


def test_publication_rejects_untracked_source_but_allows_ignored_evidence(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", "target/\n")
    project.git_init()
    revision = benchmark_provenance.require_clean(project.root).revision
    project.write("target/evidence.json", "{}\n")
    assert benchmark_provenance.require_clean(project.root).revision == revision

    project.write("python/uncommitted.py", "def changed():\n    return 1\n")
    with pytest.raises(RuntimeError, match="source files must be committed"):
        benchmark_provenance.require_clean(project.root)
    assert project.run("git", "add", "python/uncommitted.py").returncode == 0
    assert project.run("git", "commit", "-qm", "Commit benchmark source").returncode == 0
    assert benchmark_provenance.require_clean(project.root).revision != revision


def test_empty_result_files_cannot_be_compared(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.json"
    baseline = tmp_path / "baseline.json"
    candidate.write_text("{}", encoding="utf-8")
    baseline.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="benchmark"):
        compare_results(candidate, baseline)
