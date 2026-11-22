"""Reject incomplete or reordered evidence before native benchmark timing."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from validation.pytest_runner.benchmark_vfy_coverage import (
    check_coverage_report_equal,
    check_lifecycle,
    check_native_result,
    check_ordered_execution,
    command,
    expected_lifecycle_events,
    git_value,
    load_coverage_report,
    outcomes_from_history,
    prepare_run_traces,
    preserve_run_stream,
    preserve_trace,
    record_process_streams,
    route_engine,
    verify_build_revision,
    verify_environment_parity,
    verify_installed_distribution_files,
    verify_manifest_wheel_hash,
    verify_wheel_parity,
    wheel_distribution_files,
)

from tests.support import VfyProject


def test_retained_build_revision_survives_checkout_advancement(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "value = 1\n")
    project.git_init()
    baseline = git_value(tmp_path, "rev-parse", "HEAD")
    project.write("app.py", "value = 2\n")
    project.git_commit("advance checkout")
    current = git_value(tmp_path, "rev-parse", "HEAD")
    assert baseline is not None
    assert current is not None

    verify_build_revision(tmp_path, current)
    verify_build_revision(tmp_path, baseline, retained=True)
    with pytest.raises(RuntimeError, match="source revision changed"):
        verify_build_revision(tmp_path, baseline)
    with pytest.raises(RuntimeError):
        verify_build_revision(tmp_path, "0" * 40, retained=True)


@pytest.mark.parametrize("damage", ["engine", "order", "missing-case", "phase", "failure"])
def test_native_benchmark_rejects_damaged_execution_evidence(tmp_path: Path, damage: str) -> None:
    nodeids = ["test_workload.py::test_case[first]", "test_workload.py::test_case[second]"]
    record: dict[str, Any] = {
        "engine": "pytest-runner-native-v1",
        "cases": [
            {
                "nodeid": nodeid,
                "phases": [
                    {"phase": phase, "outcome": "passed"} for phase in ("setup", "call", "teardown")
                ],
            }
            for nodeid in nodeids
        ],
    }
    path = tmp_path / "native.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    check_native_result(path, nodeids)

    damaged = copy.deepcopy(record)
    cases = damaged["cases"]
    if damage == "engine":
        damaged["engine"] = "pytest"
    elif damage == "order":
        cases.reverse()
    elif damage == "missing-case":
        cases.pop()
    elif damage == "phase":
        cases[0]["phases"].reverse()
    else:
        cases[0]["phases"][1]["outcome"] = "failed"
    path.write_text(json.dumps(damaged), encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"native engine|candidate did not"):
        check_native_result(path, nodeids)


@pytest.mark.parametrize("damage", ["missing", "extra", "failed", "confirmation"])
def test_native_benchmark_requires_complete_primary_history(tmp_path: Path, damage: str) -> None:
    nodeid = "test_workload.py::test_case[first]"
    tests = {nodeid: {"observations": [{"attempt": "primary", "outcome": "passed"}]}}
    path = tmp_path / "history.json"
    path.write_text(json.dumps({"tests": tests}), encoding="utf-8")
    assert outcomes_from_history(path, [nodeid]) == [(nodeid, "passed")]

    if damage == "missing":
        tests.clear()
    elif damage == "extra":
        tests["test_workload.py::test_case[extra]"] = copy.deepcopy(tests[nodeid])
    elif damage == "failed":
        tests[nodeid]["observations"][0]["outcome"] = "failed"
    else:
        tests[nodeid]["observations"].append({"attempt": "confirmation", "outcome": "passed"})
    path.write_text(json.dumps({"tests": tests}), encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"outcomes differ|missing primary result"):
        outcomes_from_history(path, [nodeid])


@pytest.mark.parametrize(
    ("workload", "damage"),
    [("fixtures", "missing-teardown"), ("fixtures", "duplicate-setup"), ("async", "duplicate")],
)
def test_native_benchmark_rejects_damaged_lifecycle_evidence(
    tmp_path: Path, workload: str, damage: str
) -> None:
    nodeids = ["test_workload.py::test_case[first]", "test_workload.py::test_case[second]"]
    events = expected_lifecycle_events(workload, nodeids)
    if workload == "fixtures":
        assert events == [
            "session.setup",
            "module.setup",
            f"function.setup:{nodeids[0]}",
            f"function.teardown:{nodeids[0]}",
            f"function.setup:{nodeids[1]}",
            f"function.teardown:{nodeids[1]}",
            "module.teardown",
            "session.teardown",
        ]
    else:
        assert events == [
            f"async.setup:{nodeids[0]}",
            f"async.teardown:{nodeids[0]}",
            f"async.setup:{nodeids[1]}",
            f"async.teardown:{nodeids[1]}",
        ]
    path = tmp_path / f"{workload}-lifecycle.txt"
    path.write_text("\n".join(events) + "\n", encoding="utf-8")

    summary = check_lifecycle(path, workload, nodeids)
    assert summary["event_count"] == len(events)
    damaged = events.copy()
    if damage == "missing-teardown":
        damaged.pop(3)
    elif damage == "duplicate-setup":
        damaged[3] = damaged[2]
    else:
        damaged.insert(2, damaged[1])
    path.write_text("\n".join(damaged) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="fixture lifecycle differs"):
        check_lifecycle(path, workload, nodeids)


def test_native_benchmark_requires_exact_execution_order(tmp_path: Path) -> None:
    nodeids = [
        "test_workload.py::test_case[first]",
        "test_workload.py::test_case[second]",
    ]
    order_path = tmp_path / "ordered.txt"
    order_path.write_text("\n".join(nodeids) + "\n", encoding="utf-8")
    assert check_ordered_execution(order_path, nodeids)["case_count"] == 2
    order_path.write_text("\n".join(reversed(nodeids)) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="execution order differs"):
        check_ordered_execution(order_path, nodeids)


def test_native_benchmark_compares_complete_coverage_report_after_named_normalization(
    tmp_path: Path,
) -> None:
    stock_root = tmp_path / "stock-root"
    candidate_root = tmp_path / "candidate-root"
    stock_report = tmp_path / "stock.json"
    candidate_report = tmp_path / "candidate.json"
    report_template: dict[str, object] = {
        "meta": {
            "format": 3,
            "version": "7.0.0",
            "branch_coverage": True,
            "show_contexts": True,
        },
        "files": {
            "source/sample.py": {
                "executed_lines": [1, 2],
                "missing_lines": [3],
                "summary": {"covered_lines": 2, "num_statements": 3},
            }
        },
        "totals": {"covered_lines": 2, "num_statements": 3, "percent_covered": 66.67},
        "future_fields": {"stable_marker": "kept"},
    }
    stock_data = copy.deepcopy(report_template)
    candidate_data = copy.deepcopy(report_template)
    stock_meta = stock_data["meta"]
    candidate_meta = candidate_data["meta"]
    stock_files = stock_data["files"]
    candidate_files = candidate_data["files"]
    assert isinstance(stock_meta, dict)
    assert isinstance(candidate_meta, dict)
    assert isinstance(stock_files, dict)
    assert isinstance(candidate_files, dict)
    stock_meta["timestamp"] = "2026-01-01T00:00:00+00:00"
    candidate_meta["timestamp"] = "2026-09-26T12:34:56Z"
    stock_files[str(stock_root / "source" / "sample.py")] = stock_files.pop("source/sample.py")
    candidate_files[str(candidate_root / "source" / "sample.py")] = candidate_files.pop(
        "source/sample.py"
    )
    stock_report.write_text(json.dumps(stock_data), encoding="utf-8")
    candidate_report.write_text(json.dumps(candidate_data), encoding="utf-8")

    normalized_stock = load_coverage_report(stock_report, stock_root)
    normalized_candidate = load_coverage_report(candidate_report, candidate_root)
    check_coverage_report_equal(normalized_stock, normalized_candidate)
    expected_files = {"<ROOT>/source/sample.py": report_template["files"]["source/sample.py"]}
    assert normalized_stock["files"] == expected_files

    relative_candidate = copy.deepcopy(candidate_data)
    relative_files = relative_candidate["files"]
    assert isinstance(relative_files, dict)
    relative_files.pop(str(candidate_root / "source" / "sample.py"))
    relative_files["source/sample.py"] = report_template["files"]["source/sample.py"]
    candidate_report.write_text(json.dumps(relative_candidate), encoding="utf-8")
    normalized_relative = load_coverage_report(candidate_report, candidate_root)
    with pytest.raises(RuntimeError, match="coverage report fields differ"):
        check_coverage_report_equal(normalized_stock, normalized_relative)

    for section, key, changed in (
        ("meta", "branch_coverage", False),
        ("meta", "branch_coverage", 1),
        ("totals", "num_statements", 4),
        ("future_fields", "stable_marker", "changed"),
    ):
        damaged = copy.deepcopy(candidate_data)
        damaged_section = damaged[section]
        assert isinstance(damaged_section, dict)
        damaged_section[key] = changed
        candidate_report.write_text(json.dumps(damaged), encoding="utf-8")
        normalized_damaged = load_coverage_report(candidate_report, candidate_root)
        with pytest.raises(RuntimeError, match="coverage report fields differ"):
            check_coverage_report_equal(normalized_stock, normalized_damaged)

    malformed = copy.deepcopy(stock_data)
    malformed_meta = malformed["meta"]
    assert isinstance(malformed_meta, dict)
    malformed_meta["timestamp"] = "not-a-timestamp"
    stock_report.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(RuntimeError, match="valid report timestamp"):
        load_coverage_report(stock_report, stock_root)


def test_native_benchmark_measurement_traces_are_unique_and_hashed(tmp_path: Path) -> None:
    first_env, first_order, first_lifecycle = prepare_run_traces(
        tmp_path, "stock", "warmup", 1, {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    )
    second_env, second_order, second_lifecycle = prepare_run_traces(
        tmp_path, "stock", "sample", 1, {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    )
    assert first_order != second_order
    assert first_lifecycle != second_lifecycle
    assert first_env["VFY_BENCHMARK_ORDER"] == str(first_order)
    assert second_env["VFY_BENCHMARK_LIFECYCLE"] == str(second_lifecycle)

    order_payload = b"test_workload.py::test_case[first]\n"
    lifecycle_payload = b"fixture.setup:test_workload.py::test_case[first]\n"
    first_order.write_bytes(order_payload)
    first_lifecycle.write_bytes(lifecycle_payload)
    archived_order = preserve_trace(first_order, tmp_path)
    archived_lifecycle = preserve_trace(first_lifecycle, tmp_path)
    order_archive = tmp_path / str(archived_order["path"])
    lifecycle_archive = tmp_path / str(archived_lifecycle["path"])
    assert gzip.decompress(order_archive.read_bytes()) == order_payload
    assert gzip.decompress(lifecycle_archive.read_bytes()) == lifecycle_payload
    assert archived_order["sha256"] == hashlib.sha256(order_payload).hexdigest()
    assert archived_lifecycle["sha256"] == hashlib.sha256(lifecycle_payload).hexdigest()
    with pytest.raises(FileExistsError, match="trace paths already exist"):
        prepare_run_traces(tmp_path, "stock", "warmup", 1, {})


def test_native_benchmark_retains_and_hashes_raw_process_streams(tmp_path: Path) -> None:
    prepare_run_traces(tmp_path, "candidate", "rss", 3, {})
    stdout = ".. benchmark output\n"
    stderr = "Maximum resident set size (kbytes): 12345\n"
    validation: dict[str, object] = {"role": "candidate", "phase": "rss", "pair_index": 3}
    record_process_streams(
        validation, stdout, stderr, tmp_path, "candidate", "rss", 3, exit_status=0
    )

    assert validation["exit_status"] == 0
    for stream, original in (("stdout", stdout), ("stderr", stderr)):
        artifact = validation[f"{stream}_trace"]
        assert isinstance(artifact, dict)
        archive_path = tmp_path / str(artifact["path"])
        original_bytes = original.encode("utf-8")
        compressed = archive_path.read_bytes()
        assert gzip.decompress(compressed) == original_bytes
        assert artifact["path"] == f"run-traces/candidate-rss-03-{stream}.txt.gz"
        assert artifact["sha256"] == hashlib.sha256(original_bytes).hexdigest()
        assert artifact["archive_sha256"] == hashlib.sha256(compressed).hexdigest()
        assert artifact["bytes"] == len(original_bytes)
        assert artifact["archive_bytes"] == len(compressed)
        assert validation[f"{stream}_sha256"] == artifact["sha256"]
        assert validation[f"{stream}_bytes"] == artifact["bytes"]

    with pytest.raises(FileExistsError, match="artifact already exists"):
        preserve_run_stream(stdout, tmp_path, "candidate", "rss", 3, "stdout")
    with pytest.raises(FileExistsError, match="trace paths already exist"):
        prepare_run_traces(tmp_path, "candidate", "rss", 3, {})
    with pytest.raises(RuntimeError, match="status 1"):
        record_process_streams({}, stdout, stderr, tmp_path, "candidate", "rss", 4, exit_status=1)


@pytest.mark.parametrize("distribution", ["vfy", "pytest"])
def test_native_benchmark_requires_byte_identical_comparison_wheels(
    distribution: str,
) -> None:
    artifacts: dict[str, dict[str, object]] = {
        name: {
            "wheels": {
                "vfy": {"sha256": "same-vfy-wheel"},
                "pytest": {"sha256": "same-pytest-wheel"},
            }
        }
        for name in ("stock", "candidate")
    }
    verify_wheel_parity(artifacts)

    candidate_wheels = artifacts["candidate"]["wheels"]
    assert isinstance(candidate_wheels, dict)
    candidate_wheel = candidate_wheels[distribution]
    assert isinstance(candidate_wheel, dict)
    candidate_wheel["sha256"] = "different-wheel-bytes"
    with pytest.raises(RuntimeError, match=f"{distribution} wheel bytes differ"):
        verify_wheel_parity(artifacts)


def test_native_before_after_benchmark_allows_only_the_vfy_wheel_to_change(
    tmp_path: Path,
) -> None:
    old_wheel = tmp_path / "vfy-old.whl"
    new_wheel = tmp_path / "vfy-new.whl"
    old_wheel.write_bytes(b"baseline native wheel")
    new_wheel.write_bytes(b"candidate native wheel")
    old_hash = hashlib.sha256(old_wheel.read_bytes()).hexdigest()
    new_hash = hashlib.sha256(new_wheel.read_bytes()).hexdigest()
    artifacts: dict[str, dict[str, object]] = {
        "stock": {
            "wheels": {
                "vfy": {"sha256": old_hash},
                "pytest": {"sha256": "same-pytest-wheel"},
            }
        },
        "candidate": {
            "wheels": {
                "vfy": {"sha256": new_hash},
                "pytest": {"sha256": "same-pytest-wheel"},
            }
        },
    }
    verify_wheel_parity(artifacts, allow_vfy_difference=True)
    verify_manifest_wheel_hash({"wheel_sha256": old_hash}, old_wheel)
    verify_manifest_wheel_hash({"wheel_sha256": new_hash}, new_wheel)
    with pytest.raises(RuntimeError, match="build manifest wheel does not match"):
        verify_manifest_wheel_hash({"wheel_sha256": old_hash}, new_wheel)

    candidate_wheels = artifacts["candidate"]["wheels"]
    assert isinstance(candidate_wheels, dict)
    pytest_wheel = candidate_wheels["pytest"]
    assert isinstance(pytest_wheel, dict)
    pytest_wheel["sha256"] = "different-pytest-wheel"
    with pytest.raises(RuntimeError, match="pytest wheel bytes differ"):
        verify_wheel_parity(artifacts, allow_vfy_difference=True)


def test_native_benchmark_forwards_engine_and_shared_worker_options(tmp_path: Path) -> None:
    python = tmp_path / "bin" / "python"
    root = tmp_path / "project"
    report = tmp_path / "coverage.json"

    stock_command = command(python, root, report, engine="pytest")
    native_auto_command = command(python, root, report, engine="pytest-native", workers="auto")

    assert "--workers=1" in stock_command
    assert "--engine=pytest" in stock_command
    assert "--workers=auto" in native_auto_command
    assert "--engine=pytest-native" in native_auto_command
    assert route_engine("stock", "pytest") == "pytest"
    assert route_engine("stock", "pytest-native") == "pytest-native"
    assert route_engine("candidate", "pytest") == "pytest-native"


def test_native_benchmark_binds_supplied_vfy_wheel_to_build_manifest(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "vfy.whl"
    wheel.write_bytes(b"supplied wheel bytes")
    supplied_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()
    verify_manifest_wheel_hash({"wheel_sha256": supplied_hash}, wheel)

    with pytest.raises(RuntimeError, match="build manifest wheel does not match"):
        verify_manifest_wheel_hash({"wheel_sha256": "another wheel hash"}, wheel)


def _environment_artifacts(dependencies: list[list[str]]) -> dict[str, dict[str, object]]:
    wheel_hash = "artifact-proven-vfy-wheel"
    return {
        role: {
            "installed": {
                "python": "3.14.7",
                "implementation": "CPython",
                "vfy": "0.0.1",
                "dependencies": copy.deepcopy(dependencies),
            },
            "wheels": {"vfy": {"version": "0.0.1", "sha256": wheel_hash}},
            "build": {"wheel_sha256": wheel_hash},
        }
        for role in ("stock", "candidate")
    }


def test_native_benchmark_accepts_only_artifact_proven_root_editable_in_uv_lock() -> None:
    lockfile = Path(__file__).resolve().parents[2] / "uv.lock"
    artifacts = _environment_artifacts([["vfy", "0.0.1"], ["pytest", "9.1.1"]])
    verify_environment_parity(artifacts, lockfile)


def test_native_benchmark_rejects_unlocked_or_unproven_dependency_versions(
    tmp_path: Path,
) -> None:
    artifacts = _environment_artifacts([["vfy", "0.0.1"], ["pytest", "9.1.0"]])
    lockfile = Path(__file__).resolve().parents[2] / "uv.lock"
    with pytest.raises(RuntimeError, match=r"installed dependency is absent from uv.lock"):
        verify_environment_parity(artifacts, lockfile)

    artifacts = _environment_artifacts([["vfy", "999"], ["pytest", "9.1.1"]])
    with pytest.raises(RuntimeError, match=r"installed dependency is absent from uv.lock"):
        verify_environment_parity(artifacts, lockfile)

    artifacts = _environment_artifacts([["vfy", "0.0.1"], ["pytest", "9.1.1"]])
    artifacts["candidate"]["build"] = {"wheel_sha256": "unproven-wheel"}
    with pytest.raises(RuntimeError, match="build manifest differs from its wheel"):
        verify_environment_parity(artifacts, lockfile)

    artifacts = _environment_artifacts([["vfy", "0.0.1"], ["pytest", "9.1.1"]])
    malformed_lock = tmp_path / "uv.lock"
    malformed_lock.write_text(
        "version = 1\n"
        '[[package]]\nname = "vfy"\nsource = { editable = "." }\n'
        '[[package]]\nname = "pytest"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match=r"uv.lock package 'pytest' has no version"):
        verify_environment_parity(artifacts, malformed_lock)


@pytest.mark.parametrize("damage", ["capture", "missing", "unexpected"])
def test_native_benchmark_checks_complete_pytest_distribution(tmp_path: Path, damage: str) -> None:
    wheel = tmp_path / "pytest-9.1.1-py3-none-any.whl"
    files = {
        "_pytest/runner.py": b"def runtestprotocol(): pass\n",
        "_pytest/capture.py": b"class CaptureManager: pass\n",
        "_pytest/fixtures.py": b"class FixtureManager: pass\n",
        "pytest/__init__.py": b"__version__ = '9.1.1'\n",
        "py.py": b"# pytest helper module\n",
        "pytest-9.1.1.dist-info/METADATA": b"Name: pytest\nVersion: 9.1.1\n",
        "pytest-9.1.1.dist-info/RECORD": b"",
    }
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)

    expected = {
        name: hashlib.sha256(contents).hexdigest()
        for name, contents in files.items()
        if name != "pytest-9.1.1.dist-info/RECORD"
    }
    assert wheel_distribution_files(wheel, "pytest") == expected

    installed: dict[str, object] = {"pytest_files": expected.copy()}
    pytest_files = installed["pytest_files"]
    assert isinstance(pytest_files, dict)
    assert "_pytest/capture.py" in pytest_files
    verify_installed_distribution_files("stock", "pytest", installed, wheel)

    if damage == "capture":
        pytest_files["_pytest/capture.py"] = hashlib.sha256(b"modified capture module").hexdigest()
    elif damage == "missing":
        del pytest_files["_pytest/fixtures.py"]
    else:
        pytest_files["_pytest/unexpected.py"] = hashlib.sha256(
            b"unexpected import module"
        ).hexdigest()
    with pytest.raises(RuntimeError, match="stock installed pytest files differ"):
        verify_installed_distribution_files("stock", "pytest", installed, wheel)
