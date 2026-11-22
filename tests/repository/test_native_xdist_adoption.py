"""Prove the optional distributed workload checker's public evidence gates."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from validation.pytest_runner.check_native_adoption_matrix import ROOT, Project
from validation.pytest_runner.check_native_xdist_adoption import (
    Workload,
    compare_routes,
    main,
    native_execution_records,
    run_route,
    run_workload,
)

from tests.support import VfyProject


@pytest.fixture(scope="module")
def stock_record(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("xdist-adoption")
    project = VfyProject(root)
    project.write(
        "test_first.py",
        """import pytest

@pytest.fixture
def noisy():
    print('setup capture')
    yield
    print('teardown capture')

def test_capture(noisy, worker_id, testrun_uid):
    assert worker_id in {'gw0', 'gw1'}
    assert len(testrun_uid) == 32
    print('call capture')

@pytest.mark.skip(reason='reviewed skip')
def test_skip():
    pass
""",
    )
    project.write(
        "test_second.py",
        "def test_second(record_property):\n"
        "    record_property('reviewed', 'value')\n"
        "    print('second capture')\n",
    )
    descriptor = Project("fixture", "", "", ".", (".",), (), plugins=("xdist.plugin",))
    record = run_route(
        descriptor,
        root,
        Path(sys.executable),
        root / "stock",
        root / "pytest-temp",
        "stock",
        workers=2,
        coverage=False,
        timeout=60,
    )
    assert record["exit_status"] == 0, record
    assert record["record_errors"] == [], record
    assert len(record["junit_cases"]) == 3
    return record


def test_gate_accepts_real_distributed_captures_outcomes_and_properties(
    stock_record: dict[str, Any],
) -> None:
    native = copy.deepcopy(stock_record)
    assert compare_routes(stock_record, native, coverage=False)["equal"]


@pytest.mark.parametrize(
    "mutation", ["capture", "skip-reason", "property", "missing", "duplicate", "exit"]
)
def test_gate_rejects_changed_real_distributed_junit_evidence(
    stock_record: dict[str, Any],
    mutation: str,
) -> None:
    candidate = copy.deepcopy(stock_record)
    cases = candidate["junit_cases"]
    if mutation == "exit":
        candidate["exit_status"] = 1
    elif mutation == "missing":
        cases.pop()
    elif mutation == "duplicate":
        cases.append(copy.deepcopy(cases[0]))
    elif mutation == "property":
        target = next(
            child
            for case in cases
            for parent in case["children"]
            for child in parent["children"]
            if child["tag"] == "property"
        )
        target["attributes"]["value"] = "changed property"
    else:
        target = next(
            child
            for case in cases
            for child in case["children"]
            if child["tag"] == ("system-out" if mutation == "capture" else "skipped")
        )
        if mutation == "capture":
            target["text"] += "changed literal capture"
        else:
            target["attributes"]["message"] = "changed skip reason"
    comparison = compare_routes(stock_record, candidate, coverage=False)
    assert not comparison["equal"], comparison


@pytest.mark.parametrize("coverage_skip", [False, True])
@pytest.mark.parametrize("workers", [0, 2])
def test_coverage_phase_preserves_real_uninstrumented_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coverage_skip: bool, workers: int
) -> None:
    project = VfyProject(tmp_path / "upstream")
    project.write("app.py", "def value():\n    return 42\n")
    project.write(
        "test_app.py",
        "import pytest\nimport app\n"
        "def test_always():\n    assert app.value() == 42\n"
        "def test_coverage_skip(request):\n"
        f"    if {coverage_skip!r} and getattr(request.config.option, 'cov_source', None):\n"
        "        pytest.skip('only with coverage')\n"
        "    assert app.value() == 42\n",
    )
    project.git_init()
    descriptor = Project(
        "fixture", "", "", ".", ("test_app.py",), ("--cov=app",), plugins=("xdist.plugin",)
    )
    # Keep workload execution, checkout isolation, JUnit, and report oracles real;
    # replace only network checkout and installation with the prepared local inputs.
    monkeypatch.setattr(
        "validation.pytest_runner.check_native_adoption_matrix.checkout",
        lambda *_args: project.root,
    )
    monkeypatch.setattr(
        "validation.pytest_runner.check_native_adoption_matrix.prepare_python",
        lambda *_args: Path(sys.executable),
    )
    record = run_workload(
        Workload(descriptor, "local reviewed fixture"),
        tmp_path / "evidence",
        tmp_path / "candidate.whl",
        "3.12",
        (workers,),
        60,
        phases=("coverage",),
    )
    assert [run["phase"] for run in record["comparisons"]] == ["outcomes", "coverage"]
    baseline, covered = record["comparisons"]
    assert baseline["comparison"]["equal"], baseline
    for run in (baseline, covered):
        native = run["routes"]["pytest-native"]
        assert native["record_errors"] == [], native
        evidence = native["native_execution"]
        assert len(evidence) == (workers or 1), evidence
        assert sum(worker["cases"] for worker in evidence) == 2, evidence
        assert all(
            worker["engine"] == "pytest-runner-native-v1" and worker["collected"] == 2
            for worker in evidence
        ), evidence
    comparison = covered["comparison"]
    assert comparison["junit_equal"], comparison
    assert comparison["report_oracles_equal"], comparison
    assert comparison["data_equal"], comparison
    assert record["passed"] is (not coverage_skip), record
    for route in ("stock", "pytest-native"):
        outcome = comparison["coverage_outcomes"][route]
        assert outcome["equal"] is (not coverage_skip), outcome
        if coverage_skip:
            assert outcome["difference"] == {
                "index": 1,
                "stock": {"identity": ["test_app", "test_coverage_skip"], "outcomes": []},
                "native": {
                    "identity": ["test_app", "test_coverage_skip"],
                    "outcomes": [{"outcome": "skipped", "type": "pytest.skip"}],
                },
            }
    retained = json.loads((tmp_path / "evidence/partial-result.json").read_text())
    assert retained["comparisons"][1]["comparison"] == comparison
    if not coverage_skip:
        path = Path(baseline["routes"]["pytest-native"]["native_execution"][0]["path"])
        raw = json.loads(path.read_text())
        raw["engine"] = "pytest"
        path.write_text(json.dumps(raw))
        with pytest.raises(ValueError, match="did not publish a pytest-runner-native-v1 result"):
            native_execution_records(
                path.parent,
                project.root,
                Path(sys.executable),
                tmp_path / "pytest-temp",
                workers=workers,
                junit_cases=baseline["routes"]["pytest-native"]["junit_cases"],
            )
        path.unlink()
        with pytest.raises(ValueError, match="missing or unexpected native worker result files"):
            native_execution_records(
                path.parent,
                project.root,
                Path(sys.executable),
                tmp_path / "pytest-temp",
                workers=workers,
                junit_cases=baseline["routes"]["pytest-native"]["junit_cases"],
            )


@pytest.mark.parametrize(
    ("options", "diagnostic"),
    [
        (("--workers", "-1"), "worker counts must be distinct nonnegative integers"),
        (
            ("--workers", "0", "--workers", "0"),
            "worker counts must be distinct nonnegative integers",
        ),
        (("--phase", "coverage", "--phase", "coverage"), "phase selections must be unique"),
    ],
)
def test_invalid_xdist_phase_and_worker_options_fail_before_setup(
    capsys: pytest.CaptureFixture[str], options: tuple[str, ...], diagnostic: str
) -> None:
    with pytest.raises(SystemExit) as failure:
        main(
            [
                "--wheel",
                "candidate.whl",
                "--build-manifest",
                "build.json",
                "--output",
                "result",
                *options,
            ]
        )
    assert failure.value.code == 2
    assert diagnostic in capsys.readouterr().err


@pytest.mark.parametrize("revision", [None, "a" * 39, "A" * 40, "g" * 40, 123])
def test_xdist_manifest_requires_full_source_revision_before_setup(
    tmp_path: Path, revision: object
) -> None:
    wheel = tmp_path / "candidate.whl"
    candidate = b"reviewed candidate bytes"
    wheel.write_bytes(candidate)
    build_manifest = tmp_path / "build.json"
    manifest: dict[str, object] = {
        "wheel_sha256": hashlib.sha256(candidate).hexdigest(),
        "native_profile": "release",
    }
    if revision is not None:
        manifest["source_revision"] = revision
    build_manifest.write_text(json.dumps(manifest))
    output = tmp_path / "evidence"
    with pytest.raises(ValueError, match="build manifest must record a full source revision"):
        main(
            [
                "--wheel",
                str(wheel),
                "--build-manifest",
                str(build_manifest),
                "--output",
                str(output),
                "--project",
                "requests-cache",
                "--phase",
                "outcomes",
            ]
        )
    assert not output.exists()


def test_existing_matrix_xdist_route_reaches_real_installed_artifact_gate(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    wheel = project.write("candidate.whl", "reviewed candidate bytes")
    manifest = project.write("build.json", json.dumps({"wheel_sha256": "0" * 64}))
    result = project.run_python(
        str(ROOT / "validation/pytest_runner/check_native_adoption_matrix.py"),
        "--wheel",
        str(wheel),
        "--build-manifest",
        str(manifest),
        "--output",
        str(tmp_path / "result"),
        "--xdist-project",
        "requests-cache",
        "--xdist-workers",
        "2",
        "--xdist-phase",
        "outcomes",
    )
    assert result.returncode == 1, result
    assert "candidate wheel hash differs from its build manifest" in result.stderr, result
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("selection", [("--required-subset",), ("--project", "black")])
def test_existing_matrix_rejects_mixed_required_or_serial_xdist_selections(
    tmp_path: Path,
    selection: tuple[str, ...],
) -> None:
    result = VfyProject(tmp_path).run_python(
        str(ROOT / "validation/pytest_runner/check_native_adoption_matrix.py"),
        "--wheel",
        "candidate.whl",
        "--build-manifest",
        "build.json",
        "--output",
        "result",
        "--xdist-project",
        "requests-cache",
        *selection,
    )
    assert result.returncode == 2, result
    assert "--xdist-project cannot be combined with --project or --required-subset" in result.stderr
