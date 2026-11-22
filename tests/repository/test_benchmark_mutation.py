"""Focused protocol checks for the stateful mutation benchmark."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from typing import cast

import pytest
from benchmarks import benchmark_mutation
from benchmarks.benchmark_harness import BenchmarkMode, benchmark_environment, summarize
from benchmarks.benchmark_mutation import (
    MutmutSample,
    VfySample,
    _benchmark_exit_code,
    _KillerProofs,
    _mutation_methodology,
    _mutmut_cached_scope_is_exact,
    _validate_state_signatures,
    compare_fixture_outcomes,
    parse_args,
    ratio_stats,
    run_fixture_parity,
    validate_stateful_flow,
    vfy_catalog,
)
from benchmarks.cli import compare_results

from tests.support.vfy_project import VfyProject


def _args(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> Namespace:
    monkeypatch.setattr(sys, "argv", ["benchmark_mutation", *arguments])
    return parse_args()


def test_mutmut_measurement_retains_signal_timeouts_and_rejects_incomplete_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exits = dict(
        zip(
            (f"app.x_value__mutmut_{i}" for i in range(8)),
            (-24, 24, 152, 255, 36, 0, None, 99),
            strict=True,
        )
    )

    def run_mutmut(
        command: list[str], **_kwargs: object
    ) -> tuple[float, subprocess.CompletedProcess[str]]:
        generated = tmp_path / "mutants"
        generated.mkdir()
        (generated / "mutmut-stats.json").write_text("{}", encoding="utf-8")
        (generated / "app.meta").write_text(
            json.dumps({"exit_code_by_key": exits}), encoding="utf-8"
        )
        return 1.0, subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(benchmark_mutation, "run_timed", run_mutmut)
    report = benchmark_mutation.measured_mutmut(
        tmp_path, Namespace(mutmut="mutmut", workers=1), list(exits), retain_process=True
    )
    assert list(report["summary"]["outcomes"].values()) == [
        "timeout",
        "timeout",
        "timeout",
        "timeout",
        "timeout",
        "survived",
        "not_checked",
        "suspicious",
    ]
    assert report["summary"]["stats"] == {
        "total": 8,
        "timeout": 5,
        "survived": 1,
        "not_checked": 1,
        "suspicious": 1,
    }
    assert report["process"]["raw_exit_codes"] == exits
    with pytest.raises(RuntimeError, match="infrastructure mutmut outcome"):
        benchmark_mutation._validated_mutmut_signature(
            report, expected_names=list(exits), label="incomplete"
        )


def test_scoped_vfy_measurement_does_not_collect_unselected_test_modules(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("src/app.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_app.py", "from app import value\n\ndef test_value():\n    assert value() == 1\n"
    )
    project.write(
        "tests/test_other.py", "raise AssertionError('unselected test module imported')\n"
    )
    report = benchmark_mutation.measured_vfy(
        tmp_path,
        Namespace(workers=1, timeout=10, test_isolation="serial"),
        source_paths=(tmp_path / "src/app.py",),
        pytest_args=("tests/test_app.py::test_value",),
    )
    assert report["summary"] == {"protected": 1}
    assert report["mutations"][0]["tests"] == ["tests/test_app.py::test_value"]
    assert report["mutations"][0]["killer"] == "tests/test_app.py::test_value"


@pytest.mark.parametrize("isolation", [None, "default", "serial", "file"])
def test_measurement_uses_native_default_only_when_isolation_is_not_explicit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolation: str | None
) -> None:
    project = VfyProject(tmp_path)
    project.write("src/app.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_app.py", "from app import value\n\ndef test_value():\n    assert value() == 1\n"
    )
    options = () if isolation is None else ("--test-isolation", isolation)
    args = _args(
        monkeypatch,
        "--project",
        str(project.root),
        "--output",
        str(tmp_path / "result.json"),
        "--workers",
        "1",
        *options,
    )
    report = benchmark_mutation.measured_vfy(
        project.root,
        args,
        source_paths=(project.root / "src/app.py",),
        pytest_args=("tests/test_app.py::test_value",),
        retain_process=True,
    )
    command = cast(list[str], report["process"]["command"])
    methodology = cast(dict[str, object], _mutation_methodology(args, timed=True)["test_isolation"])
    assert report["summary"] == {"protected": 1}
    assert "--engine=pytest-native" in command
    assert methodology["mode"] == (isolation or "default")
    if isolation in {None, "default"}:
        assert "--test-isolation" not in command
        assert methodology["cli_arguments"] == []
        assert "native engine isolation default" in cast(str, methodology["scope"])
    else:
        option = command.index("--test-isolation")
        assert command[option + 1] == isolation
        assert methodology["cli_arguments"] == ["--test-isolation", isolation]


def test_killer_observer_avoids_project_benchmarks_package(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    project.write("benchmarks/__init__.py", "raise RuntimeError('project package imported')\n")
    project.write("test_value.py", "def test_value():\n    assert True\n")
    proofs = _KillerProofs(tmp_path / "proofs", python=sys.executable, timeout=10)
    result = proofs._run_test(project.root, "test_value.py::test_value", tmp_path / "evidence")
    assert result["returncode"] == 0, result
    outcomes = cast(list[dict[str, object]], result["outcomes"])
    assert any(row["when"] == "call" and row["outcome"] == "passed" for row in outcomes)


def test_killer_proofs_reuse_unchanged_fixtures_across_generated_baselines_and_recheck_real_inputs(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path / "project")
    project.write("src/app.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_app.py", "from app import value\n\ndef test_value():\n    assert value() == 1\n"
    )
    report = benchmark_mutation.measured_vfy(
        project.root,
        Namespace(workers=1, timeout=10, test_isolation="serial"),
        source_paths=(project.root / "src/app.py",),
        pytest_args=("tests/test_app.py::test_value",),
    )
    assert report["summary"] == {"protected": 1}
    mutation = cast(dict[str, object], report["mutations"][0])
    killer = "tests/test_app.py::test_value"
    proofs = _KillerProofs(tmp_path / "proofs", python=sys.executable, timeout=10)
    proofs.prove(project.root, mutation, [killer], label="initial")
    assert len(proofs.records()) == 1

    generated = (
        ".coverage-vfy",
        ".coverage-vfy-wal",
        ".coverage-vfy-shm",
        ".coverage-vfy-journal",
        ".coverage-vfy.child",
    )
    for name in generated:
        project.write(name, f"generated state at {project.root}\n")
    clone = tmp_path / "clone"
    benchmark_mutation.copy_project(project.root, clone)
    assert all(not (clone / name).exists() for name in generated)
    for name in generated:
        (clone / name).write_text(f"different generated state at {clone}\n", encoding="utf-8")
    copied_mutation = {**mutation, "path": str(clone / "src/app.py")}
    proofs.prove(clone, copied_mutation, [killer], label="new baseline")
    assert len(proofs.records()) == 1

    (clone / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf-8")
    proofs.prove(clone, copied_mutation, [killer], label="changed configuration")
    assert len(proofs.records()) == 2

    (clone / "tests/test_app.py").write_text(
        "from app import value\n\ndef test_value():\n    assert value() in (1, 2)\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="killer proof failed"):
        proofs.prove(clone, copied_mutation, [killer], label="weakened test")

    (clone / "src/app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="killer proof source hash changed"):
        proofs.prove(clone, copied_mutation, [killer], label="changed source")


@pytest.mark.parametrize("route", ["--validate-only", "--cold-only"])
def test_partial_validation_forces_diagnostic_sampling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, route: str
) -> None:
    (tmp_path / "src").mkdir()
    args = _args(
        monkeypatch,
        "--project",
        str(tmp_path),
        "--output",
        str(tmp_path / "result.json"),
        route,
        "--runs",
        "1",
    )
    assert args.mode == "diagnostic"
    methodology = _mutation_methodology(args, timed=route == "--cold-only")
    assert methodology["publishable"] is False
    if route == "--cold-only":
        assert methodology["complete_catalog"] == "not run in cold-only diagnostics"


def test_datasette_only_route_does_not_require_mutmut_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = _args(
        monkeypatch,
        "--datasette-only",
        "--datasette-root",
        str(tmp_path / "datasette"),
        "--datasette-python",
        str(tmp_path / "python"),
        "--datasette-preparation-manifest",
        str(tmp_path / "manifest.json"),
        "--datasette-artifacts",
        str(tmp_path / "artifacts"),
        "--output",
        str(tmp_path / "result.json"),
        "--validate-only",
        "--runs",
        "1",
    )
    assert args.datasette_only is True
    assert args.project is None


def test_publication_gate_failure_is_always_a_command_failure() -> None:
    assert _benchmark_exit_code("publication", {"semantic_parity": False}) == 1
    assert _benchmark_exit_code("diagnostic", {"semantic_parity": False}) == 0


def test_mutation_result_envelope_round_trips_through_comparator(tmp_path: Path) -> None:
    args = Namespace(
        mode="diagnostic",
        warmups=0,
        runs=2,
        validate_only=False,
        datasette_only=False,
    )

    def write_result(path: Path, revision: str, samples: list[float]) -> None:
        path.write_text(
            json.dumps(
                {
                    "schema": "vfy.mutation-benchmark",
                    "benchmark": "mutation",
                    "methodology": _mutation_methodology(args, timed=True),
                    "provenance": {
                        "source": {"revision": revision, "dirty": False},
                        "validation": {"status": "passed"},
                        "workload": {"project": "benchmark_1k", "common_catalog": 1},
                        "environment": {
                            "platform": "test-platform",
                            "machine": "test-machine",
                            "python": "3.12.0",
                            "tools": {"python": "3.12.0", "pytest": "8.0"},
                        },
                    },
                    "vfy_first_run": {
                        **summarize(samples),
                    },
                }
            ),
            encoding="utf-8",
        )

    candidate = tmp_path / "candidate.json"
    baseline = tmp_path / "baseline.json"
    write_result(candidate, "a" * 40, [1.0, 1.2])
    write_result(baseline, "b" * 40, [1.1, 1.3])
    report = compare_results(candidate, baseline, mode=BenchmarkMode.DIAGNOSTIC)
    assert report["candidate_revision"] == "a" * 40
    assert report["baseline_revision"] == "b" * 40
    records = report["records"]
    assert isinstance(records, dict)
    assert "vfy_first_run" in records
    diagnostic_validation = _mutation_methodology(
        Namespace(
            mode="diagnostic",
            warmups=3,
            runs=30,
            validate_only=True,
            datasette_only=False,
        ),
        timed=False,
    )
    assert diagnostic_validation["validation_only"] is True
    assert diagnostic_validation["publishable"] is False


def test_publication_requires_thirty_mutation_samples(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "src").mkdir()
    with pytest.raises(SystemExit) as error:
        _args(
            monkeypatch,
            "--project",
            str(tmp_path),
            "--output",
            str(tmp_path / "result.json"),
            "--runs",
            "15",
        )
    assert error.value.code == 2


def test_ratio_uncertainty_is_unavailable_for_one_sample() -> None:
    result = ratio_stats([2.0], [1.0])
    assert result["median"] == 2.0
    assert result["lower_95_percent_bound"] is None
    assert result["uncertainty"]["resamples"] == 0


def test_fixture_outcomes_require_matching_real_call_failure() -> None:
    key = ("src/app.py", 0, 1, "x", "y")
    vfy = {
        "mutations": [{"id": "vfy-id", "status": "protected", "killer": "test_app"}],
        "summary": {"protected": 1},
    }
    mutmut = {
        "wall_ms": 1.0,
        "summary": {
            "stats": {"total": 1, "killed": 1},
            "phase_timings": {},
            "outcomes": {"mutmut-id": "killed"},
        },
    }
    result = compare_fixture_outcomes(
        cast(VfySample, vfy),
        cast(MutmutSample, mutmut),
        common_catalog={key},
        vfy_ids={key: "vfy-id"},
        mutmut_names={key: "mutmut-id"},
    )
    assert result["common_mutations"] == 1
    assert result["real_call_failure_observed"] is True


def test_state_validation_rejects_divergent_status_and_missing_killer(tmp_path: Path) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("x\n", encoding="utf-8")
    key = ("src/app.py", 0, 1, "x", "y")

    def report(status: str, killer: str | None) -> VfySample:
        return cast(
            VfySample,
            {
                "mutations": [
                    {
                        "id": "vfy-id",
                        "path": str(source),
                        "start": 0,
                        "end": 1,
                        "original": "x",
                        "replacement": "y",
                        "status": status,
                        "killer": killer,
                        "tests": [killer] if killer is not None else [],
                    }
                ],
                "summary": {
                    "protected": int(status == "protected"),
                    "survived": int(status == "survived"),
                    "unreached": int(status == "unreached"),
                },
            },
        )

    with pytest.raises(RuntimeError, match="differs from baseline state"):
        _validate_state_signatures(
            [("baseline", report("protected", "test")), ("unchanged", report("survived", None))],
            root=tmp_path,
            expected_catalog={key},
        )
    with pytest.raises(RuntimeError, match="no killer"):
        _validate_state_signatures(
            [("baseline", report("protected", None)), ("unchanged", report("protected", "test"))],
            root=tmp_path,
            expected_catalog={key},
        )


def test_state_validation_accepts_reranked_killer_only_after_invalidation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("x\n", encoding="utf-8")
    key = ("src/app.py", 0, 1, "x", "y")

    def report(killer: str, status: str = "protected") -> VfySample:
        return cast(
            VfySample,
            {
                "mutations": [
                    {
                        "id": "vfy-id",
                        "path": str(source),
                        "start": 0,
                        "end": 1,
                        "original": "x",
                        "replacement": "y",
                        "status": status,
                        "killer": killer if status == "protected" else None,
                        "tests": ["test_even", "test_odd"],
                        "owner": {"phase": "body"},
                    }
                ],
                "summary": {
                    "protected": int(status == "protected"),
                    "survived": int(status == "survived"),
                    "unreached": int(status == "unreached"),
                },
            },
        )

    states = [
        ("baseline", report("test_even")),
        ("verified", report("test_even")),
        ("unchanged", report("test_even")),
        ("invalidated", report("test_odd")),
    ]
    with pytest.raises(RuntimeError, match="differs from its reference"):
        _validate_state_signatures(states, root=tmp_path, expected_catalog={key})

    with pytest.raises(RuntimeError, match="differs from its reference"):
        _validate_state_signatures(
            [*states[:-1], ("invalidated", report("test_odd", "survived"))],
            root=tmp_path,
            expected_catalog={key},
        )

    with pytest.raises(RuntimeError, match="not among its selected tests"):
        _validate_state_signatures(
            [*states[:-1], ("invalidated", report("other_test"))],
            root=tmp_path,
            expected_catalog={key},
        )


def test_complete_catalog_keeps_timeouts_separate_from_timed_semantic_parity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "app.py"
    source.write_text("a % b\n", encoding="utf-8")
    row: dict[str, object] = {
        "path": str(source),
        "start": 2,
        "end": 3,
        "original": "%",
        "replacement": "*",
        "status": "timeout",
        "killer": None,
        "returncode": None,
    }
    report = cast(VfySample, {"mutations": [row], "summary": {"timeout": 1}})
    key = ("app.py", 2, 3, "%", "*")
    validate = benchmark_mutation._validated_vfy_signature
    with pytest.raises(RuntimeError, match="infrastructure mutation outcome"):
        validate(report, root=tmp_path, expected_catalog={key}, label="common")

    def validate_complete() -> object:
        return validate(
            report,
            root=tmp_path,
            expected_catalog={key},
            label="complete",
            scope=benchmark_mutation._CatalogScope.COMPLETE,
        )

    assert validate_complete() == ((key, "timeout", None),)
    row["killer"] = "tests/test_app.py::test_gcd"
    with pytest.raises(RuntimeError, match="non-protected mutation has a killer"):
        validate_complete()
    row["killer"] = None
    row["returncode"] = 1
    with pytest.raises(RuntimeError, match="completed process exit status"):
        validate_complete()
    row["returncode"] = None
    for status in ("error", "invalid", "unstable"):
        row["status"] = status
        with pytest.raises(RuntimeError, match="infrastructure mutation outcome"):
            validate_complete()


def test_state_validation_allows_definition_scope_without_context_nodeids(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("x\n", encoding="utf-8")
    key = ("src/app.py", 0, 1, "x", "y")
    report = cast(
        VfySample,
        {
            "mutations": [
                {
                    "id": "vfy-id",
                    "path": str(source),
                    "start": 0,
                    "end": 1,
                    "original": "x",
                    "replacement": "y",
                    "status": "protected",
                    "killer": "tests/test_app.py::test_default",
                    "tests": [],
                    "owner": {"phase": "definition"},
                }
            ],
            "summary": {"protected": 1},
        },
    )

    assert _validate_state_signatures(
        [(label, report) for label in ("baseline", "verified", "unchanged", "invalidated")],
        root=tmp_path,
        expected_catalog={key},
    ) == ((key, "protected", "tests/test_app.py::test_default"),)

    changed_killer = cast(
        VfySample,
        {
            "mutations": [
                {
                    "id": "vfy-id",
                    "path": str(source),
                    "start": 0,
                    "end": 1,
                    "original": "x",
                    "replacement": "y",
                    "status": "protected",
                    "killer": "tests/test_app.py::test_other_default",
                    "tests": [],
                    "owner": {"phase": "definition"},
                }
            ],
            "summary": {"protected": 1},
        },
    )
    with pytest.raises(RuntimeError, match="differs from its reference"):
        _validate_state_signatures(
            [
                ("baseline", report),
                ("verified", report),
                ("unchanged", report),
                ("invalidated", changed_killer),
            ],
            root=tmp_path,
            expected_catalog={key},
        )


def test_contextless_body_killer_requires_verified_full_scope_and_real_failure(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path / "project")
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "test_app.py", "from app import value\n\ndef test_value():\n    assert value() == 1\n"
    )
    source = project.root / "app.py"
    body = source.read_bytes()
    start = body.index(b"1")
    key = ("app.py", start, start + 1, "1", "2")
    killer = "test_app.py::test_value"
    mutation = {
        "path": str(source),
        "start": start,
        "end": start + 1,
        "original": "1",
        "replacement": "2",
        "status": "protected",
        "killer": killer,
        "tests": [],
        "owner": {"phase": "body"},
        "source_hash": hashlib.sha256(body).hexdigest(),
    }
    report = cast(VfySample, {"mutations": [mutation], "summary": {"protected": 1}})
    validate = benchmark_mutation._validated_vfy_signature
    for keys, inventory in ((frozenset(), frozenset({killer})), (frozenset({key}), frozenset())):
        with pytest.raises(RuntimeError, match="not among its selected tests"):
            validate(
                report,
                root=project.root,
                expected_catalog={key},
                label="test",
                full_suite_keys=keys,
                baseline_test_ids=inventory,
            )
    assert validate(
        report,
        root=project.root,
        expected_catalog={key},
        label="test",
        full_suite_keys=frozenset({key}),
        baseline_test_ids=frozenset({killer}),
    ) == ((key, "protected", killer),)
    proofs = _KillerProofs(tmp_path / "proofs", python=sys.executable, timeout=10)
    with pytest.raises(RuntimeError, match="full-suite killer is not in"):
        proofs.prove(
            project.root, mutation, [killer], label="test", full_suite_tests=frozenset({"other"})
        )
    proofs.prove(
        project.root, mutation, [killer], label="test", full_suite_tests=frozenset({killer})
    )
    assert len(proofs.records()) == 1


def test_definition_killer_proof_requires_full_scope_and_real_failure(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    project.write("app.py", "def value(default=1):\n    return default\n")
    project.write(
        "test_app.py", "from app import value\n\ndef test_value():\n    assert value() == 1\n"
    )
    source = project.root / "app.py"
    body = source.read_bytes()
    start = body.index(b"1")
    killer = "test_app.py::test_value"
    mutation = {
        "path": str(source),
        "start": start,
        "end": start + 1,
        "original": "1",
        "replacement": "2",
        "status": "protected",
        "killer": killer,
        "tests": [],
        "owner": {"phase": "definition"},
        "source_hash": hashlib.sha256(body).hexdigest(),
    }
    proofs = _KillerProofs(tmp_path / "proofs", python=sys.executable, timeout=10)
    with pytest.raises(RuntimeError, match="verified full-suite mutation"):
        proofs.prove(project.root, mutation, [killer], label="test")
    with pytest.raises(RuntimeError, match="full-suite killer is not in"):
        proofs.prove(
            project.root, mutation, [killer], label="test", full_suite_tests=frozenset({"other"})
        )
    proofs.prove(
        project.root, mutation, [killer], label="test", full_suite_tests=frozenset({killer})
    )
    assert len(proofs.records()) == 1


def test_repeated_parity_must_match_the_first_oracle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key = ("src/app.py", 0, 1, "x", "y")
    vfy: benchmark_mutation.VfySignature = ((key, "protected", "test"),)
    mutmut: benchmark_mutation.MutmutSignature = (("mutant", "killed"),)
    fixture: dict[str, object] = {"common_mutations": 1, "real_call_failure_observed": True}
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    calls = 0

    def fake_parity(
        *args: object,
    ) -> tuple[
        dict[str, object],
        benchmark_mutation.VfySignature,
        benchmark_mutation.MutmutSignature,
        VfySample,
    ]:
        nonlocal calls
        calls += 1
        return (
            fixture,
            vfy if calls == 1 else ((key, "survived", None),),
            mutmut,
            cast(VfySample, {}),
        )

    monkeypatch.setattr(benchmark_mutation, "parity", fake_parity)

    with pytest.raises(RuntimeError, match="semantic mutation result differs"):
        run_fixture_parity(
            project,
            Namespace(parity_runs=2),
            {key},
            {key: "mutant"},
        )
    assert calls == 2


def test_reranked_body_killers_require_real_public_call_proofs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = VfyProject(tmp_path / "project")
    project.write("pytest.ini", "[pytest]\npythonpath = src\n")
    source = project.write(
        "src/app.py", "def classify(value: int) -> bool:\n    return value >= 2\n"
    )
    project.write(
        "tests/test_app.py",
        "from app import classify\n\n"
        "def test_even() -> None:\n    assert classify(2) is True\n\n"
        "def test_odd() -> None:\n    assert classify(2) is True\n\n"
        "def test_below() -> None:\n    assert classify(1) is False\n",
    )

    completed = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "src",
        "--operator",
        "comparison_boundary",
        "--no-cache",
        "-j",
        "1",
        "--format",
        "json",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    completed.expect(0)
    baseline = cast(VfySample, json.loads(completed.stdout))
    mutations = baseline["mutations"]
    assert isinstance(mutations, list)
    assert len(mutations) == 1
    mutation = cast(dict[str, object], mutations[0])
    assert mutation["status"] == "protected"
    assert mutation["source_hash"] == hashlib.sha256(source.read_bytes()).hexdigest()
    killer = cast(str, mutation["killer"])
    selected = cast(list[str], mutation["tests"])
    even = "tests/test_app.py::test_even"
    odd = "tests/test_app.py::test_odd"
    passing_test = "tests/test_app.py::test_below"
    assert killer in {even, odd}
    alternate = odd if killer == even else even
    assert {even, odd, passing_test}.issubset(selected)
    key = benchmark_mutation._mutation_catalog_key(mutation, project.root)

    invalidated = cast(VfySample, json.loads(json.dumps(baseline)))
    invalidated_rows = cast(list[dict[str, object]], invalidated["mutations"])
    invalidated_rows[0]["killer"] = alternate
    monkeypatch.chdir(tmp_path)
    proofs = _KillerProofs(Path("proofs"), python=sys.executable, timeout=30.0)

    signature = _validate_state_signatures(
        [
            ("baseline", baseline),
            ("verified", baseline),
            ("unchanged", baseline),
            ("invalidated", invalidated),
        ],
        root=project.root,
        expected_catalog={key},
        killer_proofs=proofs,
    )

    assert signature == ((key, "protected", killer),)
    proof_records = proofs.records()
    assert {record["killer"] for record in proof_records} == {killer, alternate}
    for record in proof_records:
        proof = json.loads(Path(cast(str, record["proof"])).read_text(encoding="utf-8"))
        assert proof["valid"] is True
        assert proof["source_hash"] == mutation["source_hash"]
        for phase in ("clean", "mutant"):
            evidence = proof[phase]
            assert evidence["command"][-1] == record["killer"]
            assert evidence["cwd"]
            assert [item["outcome"] for item in evidence["outcomes"]] == [
                "passed",
                "passed" if phase == "clean" else "failed",
                "passed",
            ]
            proof_path = record["proof"]
            assert isinstance(proof_path, str)
            phase_dir = Path(proof_path).parent / phase
            assert (phase_dir / "stdout.txt").is_file()
            assert (phase_dir / "stderr.txt").is_file()
            assert (phase_dir / "outcomes.jsonl").is_file()

    incorrectly_claimed = cast(VfySample, json.loads(json.dumps(baseline)))
    incorrectly_claimed_rows = cast(list[dict[str, object]], incorrectly_claimed["mutations"])
    incorrectly_claimed_rows[0]["killer"] = passing_test
    with pytest.raises(RuntimeError, match="killer proof failed"):
        _validate_state_signatures(
            [
                ("baseline", baseline),
                ("verified", baseline),
                ("unchanged", baseline),
                ("invalidated", incorrectly_claimed),
            ],
            root=project.root,
            expected_catalog={key},
            killer_proofs=proofs,
        )


@pytest.mark.parametrize("tree_resources", [False, True])
def test_first_timing_sample_must_match_parity_signature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tree_resources: bool
) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("x\n", encoding="utf-8")
    key = ("src/app.py", 0, 1, "x", "y")
    report = cast(
        VfySample,
        {
            "mutations": [
                {
                    "id": "vfy-id",
                    "path": str(source),
                    "start": 0,
                    "end": 1,
                    "original": "x",
                    "replacement": "y",
                    "status": "protected",
                    "killer": "second-nodeid",
                    "tests": ["second-nodeid"],
                    "owner": {"phase": "body"},
                }
            ],
            "summary": {"protected": 1},
            "metrics": {},
            "wall_ms": 1.0,
        },
    )
    if tree_resources:
        report["resources"] = {
            "method": "linux-process-tree-smaps-rollup",
            "peak_tree_pss_bytes": 123,
        }
    measured_options: dict[str, object] = {}

    def measured(*_args: object, **kwargs: object) -> VfySample:
        measured_options.update(kwargs)
        return report

    monkeypatch.setattr(benchmark_mutation, "measured_vfy", measured)
    evidence = tmp_path / "samples"
    parity_signature = ((key, "protected", "first-nodeid"),)
    with pytest.raises(RuntimeError, match="pre-timing parity"):
        benchmark_mutation._sample_vfy(
            tmp_path,
            Namespace(tree_resources=tree_resources, output=tmp_path / "result.json"),
            {key},
            label="round-001-vfy-cold",
            expected_signature=parity_signature,
            evidence_dir=evidence,
        )
    failure = evidence / "round-001-vfy-cold.failure.json"
    assert failure.is_file()
    retained = json.loads(failure.read_text(encoding="utf-8"))
    assert retained["value"]["mutations"][0]["killer"] == "second-nodeid"
    assert measured_options["resource_path"] == (
        tmp_path / "result.artifacts/resources/round-001-vfy-cold.json" if tree_resources else None
    )
    if tree_resources:
        assert retained["value"]["resources"] == report["resources"]


def test_pinned_mutmut_named_and_unnamed_cached_commands_have_different_boundaries(
    tmp_path: Path,
) -> None:
    repository_value = os.environ.get("MUTMUT_REPOSITORY", "/tmp/mutmut")
    repository = Path(repository_value)
    executable = Path(
        os.environ.get("MUTMUT_EXECUTABLE", str(repository / ".venv" / "bin" / "mutmut"))
    )
    if not repository.is_dir() or not executable.is_file():
        message = f"the pinned mutmut checkout is unavailable: {repository} / {executable}"
        if os.environ.get("MUTMUT_BOUNDARY_REQUIRED") == "1":
            pytest.fail(message)
        pytest.skip(message)
    revision = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert revision.returncode == 0
    assert revision.stdout.strip() == benchmark_mutation.MUTMUT_REVISION

    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def answer() -> int:\n    return 42\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import answer\n\n\ndef test_answer() -> None:\n    assert answer() == 42\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        """[project]
name = "mutmut-cache-boundary"
version = "0.1.0"
requires-python = ">=3.12"

[tool.mutmut]
source_paths = ["src/"]
process_isolation = "hot-fork"
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(tmp_path / "src")

    def run(*names: str) -> str:
        completed = subprocess.run(
            [str(executable), "run", "--max-children", "1", *names],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr[-4000:]
        return completed.stdout + completed.stderr

    run_output = run()
    mutant_names: list[str] = []
    for metadata_path in (tmp_path / "mutants").rglob("*.meta"):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        entries = metadata.get("exit_code_by_key", {})
        if isinstance(entries, dict):
            mutant_names.extend(str(name) for name in entries)
    assert mutant_names
    named_output = run(mutant_names[0])
    unnamed_output = run()

    def throughput(output: str) -> float:
        match = re.search(r"([0-9]+(?:\.[0-9]+)?) mutations/second", output)
        assert match is not None, output[-4000:]
        return float(match.group(1))

    assert throughput(run_output) > 0.0
    assert throughput(named_output) > 0.0
    assert throughput(unnamed_output) == 0.0
    catalog_key = ("src/app.py", 0, 1, "42", "43")
    assert _mutmut_cached_scope_is_exact({catalog_key}, {catalog_key}) is True
    assert (
        _mutmut_cached_scope_is_exact(
            {catalog_key, ("src/app.py", 0, 1, "42", "44")}, {catalog_key}
        )
        is False
    )


def test_stateful_flow_passes_each_command_history_to_the_next(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text(
        "def test_value() -> None:\n    assert True\n", encoding="utf-8"
    )
    catalog, _elapsed, _ids = vfy_catalog(tmp_path, (source,))
    key = next(iter(catalog))
    history_path = tmp_path / ".vfy" / "test-history.json"
    observed: list[str | None] = []
    calls = 0

    def fake_measured(*args: object, **kwargs: object) -> VfySample:
        nonlocal calls
        observed.append(
            history_path.read_text(encoding="utf-8") if history_path.is_file() else None
        )
        calls += 1
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(f"history-{calls}", encoding="utf-8")
        path, start, end, original, replacement = key
        return cast(
            VfySample,
            {
                "mutations": [
                    {
                        "id": "vfy-id",
                        "path": str(tmp_path / path),
                        "start": start,
                        "end": end,
                        "original": original,
                        "replacement": replacement,
                        "status": "protected",
                        "killer": "test_value",
                        "tests": ["test_value"],
                        "owner": {"phase": "body"},
                    }
                ],
                "summary": {"protected": 1},
                "metrics": {"cache_hits": float(calls in (2, 3))},
                "wall_ms": 1.0,
            },
        )

    monkeypatch.setattr(benchmark_mutation, "measured_vfy", fake_measured)
    result = validate_stateful_flow(
        tmp_path,
        Namespace(workers=1, timeout=30.0),
        {key},
        source_paths=(source,),
        pytest_args=(),
        copy_fixture=False,
    )

    assert observed == [None, "history-1", "history-2", "history-3"]
    assert result["cache_hits"] == [0.0, 1.0, 1.0, 0.0]


def test_real_tiny_stateful_boundary_preserves_exact_semantics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text(
        "from app import value\n\n\ndef test_value() -> None:\n    assert value() == 1\n",
        encoding="utf-8",
    )
    catalog, _elapsed, _ids = vfy_catalog(tmp_path, (source,))
    args = Namespace(workers=1, timeout=30.0, test_isolation="default")
    commands: list[list[str]] = []
    run_timed = benchmark_mutation.run_timed

    def observe_command(
        command: list[str],
        *,
        root: Path,
        env: dict[str, str],
        timeout: float | None = None,
        allowed_returncodes: frozenset[int] = frozenset({0}),
    ) -> tuple[float, subprocess.CompletedProcess[str]]:
        commands.append(command)
        return run_timed(
            command,
            root=root,
            env=env,
            timeout=timeout,
            allowed_returncodes=allowed_returncodes,
        )

    monkeypatch.setattr(benchmark_mutation, "run_timed", observe_command)
    result = validate_stateful_flow(
        tmp_path,
        args,
        {next(iter(catalog))},
        source_paths=(source,),
        pytest_args=("tests/test_app.py::test_value",),
        python=sys.executable,
        evidence_dir=tmp_path / "evidence",
    )
    assert result["cache_hits"] == [0.0, 1.0, 1.0, 0.0]
    assert len(commands) == 4
    assert all("--engine=pytest-native" in command for command in commands)
    assert all("--test-isolation" not in command for command in commands)
    assert result["states"] == [
        "baseline",
        "verified_baseline",
        "unchanged_cached",
        "invalidated_edit",
    ]


def test_benchmark_environment_removes_controller_pointers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "poison")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/controller")
    monkeypatch.setenv("VIRTUAL_ENV", "/controller")
    monkeypatch.setenv("NO_COLOR", "1")
    environment = benchmark_environment({"PYTHONPATH": "fixture"})
    assert environment["PYTHONPATH"] == "fixture"
    assert "UV_PROJECT_ENVIRONMENT" not in environment
    assert "VIRTUAL_ENV" not in environment
    assert "NO_COLOR" not in environment
    assert os.environ["PYTHONPATH"] == "poison"
