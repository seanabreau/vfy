"""Admission and observer policy for diagnostic real-package timings."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from validation.pytest_runner.real_packages import benchmark

from tests.repository.test_real_package_corpus import (
    corpus as corpus,
    recorded_pair as recorded_pair,
)


@pytest.fixture
def matching_receipts(recorded_pair: Path, tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    """Start with the existing real pytest/vfy receipt, then model the pruned route."""
    full = recorded_pair
    bare = tmp_path / "bare"
    shutil.copytree(full, bare)
    inventory_path = full / benchmark.corpus.INVENTORY
    inventory = json.loads(inventory_path.read_text())
    wheel = {"sha256": "wheel-digest", "files": inventory["artifact"]["files"]}
    for directory in (full, bare):
        path = directory / benchmark.corpus.INVENTORY
        record = json.loads(path.read_text())
        record["artifact"] = wheel
        path.write_text(json.dumps(record))
    path = bare / "example.json"
    record = json.loads(path.read_text())
    record["vfy"]["command"][-2:] = [
        f"--junitxml={(bare / 'example.vfy.xml').resolve()}",
        f"--native-result-json={(bare / 'example.native.json').resolve()}",
    ]
    provenance = record["vfy"]["provenance"]
    provenance["pytest_available"] = False
    provenance["pruned"] = ["pytest"]
    provenance["distributions"].pop("pytest", None)
    path.write_text(json.dumps(record))
    return full, bare, wheel


def _example_entry() -> dict[str, object]:
    spec = importlib.util.find_spec("vfy")
    assert spec is not None
    assert spec.origin is not None
    source = Path(spec.origin).resolve().parent.parent
    return {
        "name": "example",
        "env": {"PYTHONPATH": os.pathsep.join((str(source), sysconfig.get_path("purelib")))},
    }


def test_gate_requires_matching_current_ordered_outcomes_and_bare_environment(
    matching_receipts: tuple[Path, Path, dict[str, object]],
) -> None:
    full, bare, wheel = matching_receipts
    entry = _example_entry()
    accepted = benchmark._route_gate("example", entry, full, bare, wheel)
    assert set(accepted) == {"pytest", "vfy-full", "vfy-bare"}
    path = bare / "example.json"
    result = json.loads(path.read_text())
    result["vfy"]["cases"][0][1] = "different_case"
    path.write_text(json.dumps(result))
    with pytest.raises(ValueError, match="JUnit report differs"):
        benchmark._route_gate("example", entry, full, bare, wheel)
    result["vfy"]["cases"][0][1] = accepted["vfy-bare"]["cases"][0][1]
    result["vfy"]["provenance"]["pytest_available"] = True
    path.write_text(json.dumps(result))
    with pytest.raises(ValueError, match="pytest availability"):
        benchmark._route_gate("example", entry, full, bare, wheel)


def test_gate_rejects_real_measurement_but_not_an_unused_benchmark_plugin(
    matching_receipts: tuple[Path, Path, dict[str, object]],
) -> None:
    full, bare, wheel = matching_receipts
    for directory, routes in ((full, ("pytest", "vfy")), (bare, ("vfy",))):
        path = directory / "example.json"
        record = json.loads(path.read_text())
        for route in routes:
            provenance = record[route]["provenance"]
            if directory == full:
                provenance["distributions"]["pytest-benchmark"] = "5.3.1"
            else:
                provenance["distributions"].pop("pytest-benchmark", None)
                provenance["pruned"].append("pytest-benchmark")
        path.write_text(json.dumps(record))
    stock_log = full / "example.pytest.log"
    original = stock_log.read_text()
    benchmark._route_gate("example", _example_entry(), full, bare, wheel)
    stock_log.write_text(original + "\x1b[33m---- benchmark: 15 tests ----\x1b[0m\n")
    with pytest.raises(ValueError, match="measured benchmarks"):
        benchmark._route_gate("example", _example_entry(), full, bare, wheel)


def test_benchmark_guard_rejects_suppressed_or_instrumented_measurements() -> None:
    stock = {"pytest-benchmark": "5.3.1", "pytest-codspeed": "5.0.3"}
    benchmark._check_benchmark_options([], {}, stock, {})
    for options in (["--benchmark-quiet"], ["--no-summary"], ["--benchmark-cprofile=call"]):
        with pytest.raises(ValueError, match="measurement cannot be ruled out"):
            benchmark._check_benchmark_options(options, {}, stock, {})
    for options, environment in ((["--codspeed"], {}), ([], {"CODSPEED_ENV": ""})):
        with pytest.raises(ValueError, match="CodSpeed measurement is enabled"):
            benchmark._check_benchmark_options(options, environment, stock, {})


def test_tooling_identity_changes_when_the_process_helper_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = (
        "validation/pytest_runner/real_packages/benchmark.py",
        "validation/pytest_runner/real_packages/corpus.py",
        "scripts/process_tree.py",
        "scripts/pytest_execution_record.py",
        "benchmarks/benchmark_harness.py",
    )
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark, "__file__", str(tmp_path / files[0]))
    monkeypatch.setattr(benchmark.corpus, "__file__", str(tmp_path / files[1]))

    def git(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert list(command) == ["git", "-C", str(tmp_path), "rev-parse", "HEAD"]
        return subprocess.CompletedProcess(command, 0, "revision\n", "")

    monkeypatch.setattr(benchmark.subprocess, "run", git)
    before = benchmark._tooling_identity()
    (tmp_path / "scripts/process_tree.py").write_text("changed")
    after = benchmark._tooling_identity()
    assert before["controller_revision"] == "revision"
    assert before["sha256"]["scripts/process_tree.py"] != after["sha256"]["scripts/process_tree.py"]
    assert before["sha256"][files[0]] == after["sha256"][files[0]]
    (tmp_path / files[0]).write_text("updated runner")
    assert benchmark._tooling_identity()["sha256"][files[0]] != after["sha256"][files[0]]


def test_timing_removes_only_corpus_reports_and_rejects_configured_observers(
    recorded_pair: Path, tmp_path: Path
) -> None:
    result = json.loads((recorded_pair / "example.json").read_text())
    stock = result["pytest"]
    command = benchmark._timed_command("pytest", stock, Path(stock["command"][0]))
    assert command == stock["command"][:-1]
    vfy = result["vfy"]
    vfy_command = benchmark._timed_command("vfy-full", vfy, Path(vfy["command"][0]))
    assert vfy_command == vfy["command"][:-2]
    python = Path(sys.executable)
    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\n# addopts = --junitxml=ignored.xml\naddopts = -q\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "--junitxml=unused.xml"\n'
    )
    benchmark._check_addopts(python, tmp_path, "pytest", command, environment)
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -q --junitxml=observed.xml\n")
    with pytest.raises(ValueError, match="observer or result file"):
        benchmark._check_addopts(python, tmp_path, "pytest", command, environment)
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -q\n")
    with pytest.raises(ValueError, match="observer or result file"):
        benchmark._check_addopts(
            python,
            tmp_path,
            "pytest",
            [*command, "-o", "addopts=--native-result-json=x"],
            environment,
        )
    benchmark._check_addopts(
        python,
        tmp_path,
        "pytest",
        [*command, "-o", "addopts=--junitxml=unused.xml", "-o", "addopts=-q"],
        environment,
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --cov=example\n")
    with pytest.raises(ValueError, match="enables coverage"):
        benchmark._check_addopts(python, tmp_path, "pytest", command, environment)
    benchmark._check_addopts(python, tmp_path, "pytest", [*command, "--no-cov"], environment)


def test_timing_honors_recipe_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def captured(
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        subprocess_umask: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert timeout == 600
        return subprocess.CompletedProcess(command, 0, "1 passed in 0.01s\n", "")

    monkeypatch.setattr(benchmark, "run_captured", captured)
    benchmark._sample(
        "example",
        {"timeout": 600},
        tmp_path,
        tmp_path,
        tmp_path,
        "pytest",
        {"command": ["fake"], "env": {}},
        {"passed": 1},
        "sample",
        0,
    )


def test_timing_rejects_changed_counts_and_retains_diagnostic_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def captured(
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        subprocess_umask: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "1 passed in 0.01s\n", "")

    monkeypatch.setattr(benchmark, "run_captured", captured)
    project = tmp_path / "project"
    (project / ".vfy").mkdir(parents=True)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="outcome counts changed"):
        benchmark._sample(
            "example",
            {},
            project,
            project,
            output,
            "vfy-full",
            {"command": ["fake"], "env": {}},
            {"passed": 2},
            "sample",
            0,
        )
    destination = output / "example/sample-00-vfy-full"
    assert json.loads((destination / "result.json").read_text())["counts"] == {"passed": 1}
    assert (destination / "stdout.txt").read_text() == "1 passed in 0.01s\n"
    assert not (project / ".vfy").exists()

    def measured(
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        subprocess_umask: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 0, "---- benchmark 'group: variant': 2 tests ----\n1 passed in 0.01s\n", ""
        )

    monkeypatch.setattr(benchmark, "run_captured", measured)
    with pytest.raises(ValueError, match="sample measured benchmarks"):
        benchmark._sample(
            "example",
            {},
            project,
            project,
            output,
            "pytest",
            {"command": ["fake"], "env": {}, "benchmark_table_guard": True},
            {"passed": 1},
            "sample",
            0,
        )
