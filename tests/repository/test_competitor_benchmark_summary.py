"""The compact index must retain losses and reject incomplete benchmark evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from scripts.build_selection_chart_summary import PARITY
from scripts.summarize_competitor_benchmarks import build_index

type Record = dict[str, Any]


def memory(wall: float, pss: int = 100) -> Record:
    return {
        "method": "linux-process-tree-smaps-rollup",
        "child_exit_status": 0,
        "child_command": ["python", "-m", "fixture_tool"],
        "collector_sha256": "a" * 64,
        "sample_interval_ms": 20,
        "peak_tree_pss_bytes": pss,
        "peak_tree_rss_bytes": pss * 2,
        "child_wall_ms": wall,
        "snapshot_count": 4,
        "max_sample_gap_ms": 22.5,
    }


def write(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def base() -> Record:
    return {
        "provenance": {
            "source": {"revision": "fixture-controller"},
            "environment": {"machine": "fixture-host"},
        }
    }


def sampling() -> Record:
    return {"mode": "diagnostic", "warmups": 0, "measured_runs": 2}


def native(path: Path) -> Record:
    write(
        path.parent / "example/identity.json",
        {
            "build": {
                "source_revision": "c" * 40,
                "native_profile": "release",
                "wheel_sha256": "b" * 64,
            },
            "prepared": {"revision": "fixture-project", "wheel_sha256": "b" * 64},
        },
    )
    samples = [
        {
            "phase": "sample",
            "index": index,
            "route": name,
            "seconds": value / 1000,
            "resources": memory(value, pss),
        }
        for index in range(2)
        for name, value, pss in (
            ("stock-xdist-4", 10.0 + index * 4, 100),
            ("pytest-native-j4", 20.0 + index * 4, 150),
        )
    ]
    return {
        "controller": {"revision": "fixture-controller"},
        "machine": {"cpu": "fixture"},
        "sampling": sampling(),
        "method": {"gate": "JUnit multiset; session warnings not compared"},
        "projects": [
            {
                "project": "example",
                "status": "measured",
                "cases": 3,
                "parity": {
                    name: {"passed": True, "errors": []}
                    for name in ("stock-xdist-4", "pytest-native-j4")
                },
                "timing": {"samples": samples},
            }
        ],
    }


def selection_sample(index: int, *, timed: bool = True) -> Record:
    coverage = {
        "aggregate_schema7": "exact",
        "per_context_schema7": "exact",
        "different_context_file_pairs": 0,
        "vfy_contexts": 1,
        "oracle_contexts": 1,
        "vfy_only_contexts": [],
        "oracle_only_contexts": [],
        "first_differences": [],
        "vfy_data": "vfy.coverage",
        "oracle_data": "oracle.coverage",
    }
    return {
        "kind": "measured",
        "index": index,
        "parity": PARITY,
        "selection_relation": "exact",
        "baseline_coverage": coverage,
        "affected_coverage": coverage,
        "vfy_tests": ["test_a"],
        "testmon_tests": ["test_a"],
        "full_tests": ["test_a", "test_b"],
        "elapsed_ms": {"vfy": 20.0 if timed else 0, "testmon": 10.0 if timed else 0},
        "resources": {"vfy": memory(20.0, 150), "testmon": memory(10.0)},
    }


def selection() -> Record:
    return {
        **base(),
        "benchmark": "real-selection",
        "project": "Platformdirs",
        "sampling": sampling(),
        "validation": selection_sample(0, timed=False),
        "samples": [selection_sample(0), selection_sample(1)],
    }


def real_mutation() -> Record:
    return {
        **base(),
        "schema": "vfy.real-mutation-benchmark",
        "project": "example",
        "validation": {"status": "passed"},
        "parity": {
            "outcomes": {"src/app.py|0|1|x|y": {"vfy": "protected", "mutmut": "killed"}},
            "real_call_failure_observed": True,
        },
        "scope": {
            "function": "operation",
            "source": "src/app.py",
            "test": "tests/test_app.py::test_a",
        },
        "mutmut_outcomes": {"outcomes": {"mutant": "killed"}},
        "methodology": {**sampling(), "boundary": "full helper call"},
        "samples": [
            {
                "round": index + 1,
                "phase": "measured",
                "vfy_ms": 30.0,
                "mutmut_ms": 25.0,
                "vfy_report": {
                    "resources": memory(20.0, 150),
                    "mutations": [
                        {
                            "path": "/tmp/fixture/src/app.py",
                            "start": 0,
                            "end": 1,
                            "original": "x",
                            "replacement": "y",
                            "status": "protected",
                            "killer": "test_a",
                        }
                    ],
                },
                "mutmut_report": {
                    "resources": memory(15.0),
                    "summary": {"outcomes": {"mutant": "killed"}},
                },
            }
            for index in range(2)
        ],
    }


def edit_loop() -> Record:
    phases = ("baseline", "unchanged", "local-edit", "shared-edit", "breaking", "repair", "full")
    workflows = {
        "affected-selected": 20.0,
        "affected-testmon": 10.0,
        "unchanged-vfy": 20.0,
        "unchanged-testmon": 10.0,
        "cold-vfy": 20.0,
        "cold-testmon": 10.0,
        "default-vfy": 20.0,
        "affected-full": 30.0,
        "cold-plain": 5.0,
        **{f"sequence-{phase}": 20.0 for phase in phases},
    }
    return {
        **base(),
        "benchmark": "edit-loop",
        "methodology": sampling(),
        "selection_vs_testmon": "exact",
        "selected_tests": ["test_a"],
        "developer_sequence": {
            "samples": [
                {
                    "validation": "passed",
                    "phases": [{"phase": phase, "elapsed_ms": 20.0} for phase in phases],
                }
                for _ in range(2)
            ]
        },
        "outcomes": {
            "selected": {"test_a": "passed"},
            "testmon": {"test_a": "passed"},
            "vfy_default": {"test_a": "passed"},
            "full_oracle": {"test_a": "passed", "test_b": "passed", "test_c": "passed"},
            "breaking": {name: {"test_a": "failed"} for name in ("vfy", "testmon")},
        },
        "resources": {
            "samples": [
                {
                    "workflow": workflow,
                    "record": {
                        **memory(wall),
                        "child_exit_status": 1 if workflow == "sequence-breaking" else 0,
                    },
                }
                for _ in range(2)
                for workflow, wall in workflows.items()
            ]
        },
        "selected": {"samples_ms": [20.0, 20.0]},
        "testmon": {"samples_ms": [10.0, 10.0]},
        "vfy_default": {"samples_ms": [20.0, 20.0]},
        "full_oracle": {"samples_ms": [30.0, 30.0]},
        "controls": {
            phase: {
                "vfy": {"samples_ms": [20.0, 20.0]},
                "testmon": {"samples_ms": [10.0, 10.0]},
                "plain_pytest": {"samples_ms": [5.0, 5.0]},
            }
            for phase in ("unchanged", "cold_baseline")
        },
    }


def generated(path: Path) -> Record:
    for index in range(2):
        for suffix, wall in (
            ("vfy-cold", 20.0),
            ("vfy-warm", 20.0),
            ("vfy-cached", 20.0),
            ("mutmut-cold", 10.0),
        ):
            write(
                path.with_suffix(".artifacts") / "resources" / f"round-{index:03d}-{suffix}.json",
                memory(wall),
            )
    return {
        **base(),
        "benchmark": "mutation",
        "methodology": sampling(),
        "gates": {"fixture_outcomes_match": True, "real_call_failure_observed": True},
        "fixture_parity": {"outcomes": {"mutant": "killed"}, "real_call_failure_observed": True},
        **{
            name: {"samples_ms": [wall, wall]}
            for name, wall in (
                ("vfy_first_run", 20.0),
                ("vfy_warm", 20.0),
                ("vfy_incremental", 20.0),
                ("mutmut", 10.0),
            )
        },
    }


@pytest.mark.parametrize("kind", ["native", "selection", "real-mutation", "edit-loop", "mutation"])
def test_index_recomputes_absolute_medians_and_retains_slower_routes(
    tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "result.json"
    data = {
        "native": lambda: native(path),
        "selection": selection,
        "real-mutation": real_mutation,
        "edit-loop": edit_loop,
        "mutation": lambda: generated(path),
    }[kind]()
    write(path, data)
    index = build_index([path], [], "2026-10-17")
    assert index["sources"][0]["sha256"]
    assert index["sources"][0]["provenance"]
    for row in index["results"]:
        assert row["comparisons"][0]["candidate_faster"] is False
        for measured in row["routes"].values():
            assert len(measured["wall_ms"]) == len(measured["peak_tree_pss_bytes"]) == 2
        candidate = row["comparisons"][0]["candidate"]
        assert row["routes"][candidate]["median_wall_ms"] == (
            22.0 if kind == "native" else 30.0 if kind == "real-mutation" else 20.0
        )
    if kind == "real-mutation":
        assert index["results"][0]["routes"]["vfy"]["command_wall_ms"] == [20.0, 20.0]


@pytest.mark.parametrize(
    "missing", ["sample", "memory", "parity", "warmup", "duplicate", "wall", "provenance"]
)
def test_native_index_rejects_incomplete_or_mismatched_evidence(
    tmp_path: Path, missing: str
) -> None:
    path = tmp_path / "result.json"
    data = native(path)
    project = data["projects"][0]
    samples = project["timing"]["samples"]
    if missing == "sample":
        samples.pop()
    elif missing == "memory":
        samples[0].pop("resources")
    elif missing == "parity":
        project["parity"]["pytest-native-j4"]["passed"] = False
    elif missing == "warmup":
        data["sampling"]["warmups"] = 1
    elif missing == "duplicate":
        samples[-1]["index"] = 0
    elif missing == "wall":
        samples[0]["seconds"] = 42
    else:
        data.pop("controller")
        data.pop("machine")
    write(path, data)
    with pytest.raises(ValueError, match=r"sample|resource|parity|wall|provenance"):
        build_index([path], [], "2026-10-17")


@pytest.mark.parametrize("kind", ["selection", "real-mutation", "edit-loop", "mutation"])
def test_index_rejects_missing_semantic_gate_for_each_workflow(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "result.json"
    if kind == "selection":
        data = selection()
        data["samples"][0]["affected_coverage"]["aggregate_schema7"] = "differs"
    elif kind == "real-mutation":
        data = real_mutation()
        data["parity"]["real_call_failure_observed"] = False
    elif kind == "edit-loop":
        data = edit_loop()
        data["outcomes"]["breaking"]["vfy"] = {"test_a": "passed"}
    else:
        data = generated(path)
        data["gates"]["fixture_outcomes_match"] = False
    write(path, data)
    with pytest.raises(ValueError, match=r"coverage|proof|breaking|gate"):
        build_index([path], [], "2026-10-17")


def test_failed_projects_routes_and_external_gate_artifacts_remain_visible(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    data = native(path)
    data["projects"].append(
        {"project": "blocked", "status": "blocked", "error": "inventory mismatch"}
    )
    data["projects"][0]["parity"]["pytest-native-xdist-4"] = {
        "passed": False,
        "errors": ["capture mismatch"],
    }
    write(path, data)
    failed = write(
        tmp_path / "mutation-failed.json", {"project": "another", "error": "killer proof failed"}
    )
    index = build_index([path], [failed], "2026-10-17")
    assert len(index["results"]) == 1
    assert len(index["blocked"]) == 3
    assert index["blocked"][-1]["details"]["error"] == "killer proof failed"


def test_missing_generated_mutation_memory_artifact_prevents_summary(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    data = generated(path)
    write(path, data)
    (path.with_suffix(".artifacts") / "resources/round-001-vfy-cached.json").unlink()
    with pytest.raises(FileNotFoundError):
        build_index([path], [], "2026-10-17")


def test_publication_mode_cannot_relabel_diagnostic_samples(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    data = selection()
    data["sampling"]["mode"] = "publication"
    write(path, data)
    with pytest.raises(ValueError, match="publication sampling is incomplete"):
        build_index([path], [], "2026-10-17")


def test_real_mutation_per_sample_status_drift_rejects_index(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    data = real_mutation()
    data["samples"][1]["vfy_report"]["mutations"][0]["status"] = "survived"
    write(path, data)
    with pytest.raises(ValueError, match="sample differs from the parity outcomes"):
        build_index([path], [], "2026-10-17")


def test_requests_mutation_scopes_remain_distinct_in_the_index(tmp_path: Path) -> None:
    paths = []
    for label, function, test in (
        ("requests", "unquote_unreserved", "tests/test_utils.py::test_unquote_unreserved"),
        ("requests-utils", "utils.py module", "tests/test_utils.py"),
    ):
        data = real_mutation()
        data["project"] = "requests"
        data["scope"] = {"function": function, "source": "src/requests/utils.py", "test": test}
        data["parity"]["outcomes"] = {
            "src/requests/utils.py|0|1|x|y": {"vfy": "protected", "mutmut": "killed"}
        }
        for row in data["samples"]:
            row["vfy_report"]["mutations"][0]["path"] = "/tmp/fixture/src/requests/utils.py"
        paths.append(write(tmp_path / f"{label}.json", data))
    index = build_index(paths, [], "2026-10-17")
    workloads = [row["workload"] for row in index["results"]]
    assert workloads == [
        {
            "project": "requests",
            "scope": "unquote_unreserved",
            "source": "src/requests/utils.py",
            "test": "tests/test_utils.py::test_unquote_unreserved",
        },
        {
            "project": "requests",
            "scope": "utils.py module",
            "source": "src/requests/utils.py",
            "test": "tests/test_utils.py",
        },
    ]


@pytest.mark.parametrize("field", ["project", "function", "source", "test"])
@pytest.mark.parametrize("missing", [True, False])
def test_real_mutation_index_requires_declared_scope_ids(
    tmp_path: Path, field: str, missing: bool
) -> None:
    data = real_mutation()
    target = data if field == "project" else data["scope"]
    if missing:
        target.pop(field)
    else:
        target[field] = ""
    path = write(tmp_path / "result.json", data)
    with pytest.raises(
        ValueError, match="requires nonempty project, function, source, and test IDs"
    ):
        build_index([path], [], "2026-10-17")


def test_consistently_changed_mutmut_outcomes_do_not_replace_the_parity_baseline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "result.json"
    data = real_mutation()
    for row in data["samples"]:
        row["mutmut_report"]["summary"]["outcomes"]["mutant"] = "survived"
    write(path, data)
    with pytest.raises(ValueError, match="mutmut sample differs from the parity outcomes"):
        build_index([path], [], "2026-10-17")


@pytest.mark.parametrize("corruption", ["wheel", "profile", "revision"])
def test_native_identity_rejects_a_different_wheel_or_nonrelease_build(
    tmp_path: Path, corruption: str
) -> None:
    path = tmp_path / "result.json"
    data = native(path)
    write(path, data)
    identity_path = path.parent / "example/identity.json"
    identity = json.loads(identity_path.read_text())
    if corruption == "wheel":
        identity["build"]["wheel_sha256"] = "f" * 64
    elif corruption == "profile":
        identity["build"]["native_profile"] = "debug"
    else:
        identity["build"]["source_revision"] = "unknown"
    write(identity_path, identity)
    with pytest.raises(ValueError, match="native build or pinned project identity is missing"):
        build_index([path], [], "2026-10-17")


@pytest.mark.parametrize("missing", ["missing-control", "wrong-failure-status"])
def test_edit_loop_sequence_resource_controls_are_required(tmp_path: Path, missing: str) -> None:
    path = tmp_path / "result.json"
    data = edit_loop()
    resources = data["resources"]["samples"]
    if missing == "missing-control":
        resources.pop()
    else:
        next(row for row in resources if row["workflow"] == "sequence-breaking")["record"][
            "child_exit_status"
        ] = 0
    write(path, data)
    with pytest.raises(ValueError, match=r"sample count|status"):
        build_index([path], [], "2026-10-17")


@pytest.mark.parametrize(
    ("policy", "vfy_ids", "testmon_ids", "relation"),
    [
        ("exact", ["test_a"], ["test_a"], "equal"),
        ("vfy_conservative", ["test_a"], ["test_a"], "equal"),
        ("vfy_conservative", ["test_a", "test_b"], ["test_a"], "strict_superset"),
    ],
)
def test_edit_loop_index_retains_selection_policy_and_observed_ids(
    tmp_path: Path, policy: str, vfy_ids: list[str], testmon_ids: list[str], relation: str
) -> None:
    path = tmp_path / "result.json"
    data = edit_loop()
    data["selection_vs_testmon"] = policy
    data["selected_tests"] = vfy_ids
    data["outcomes"]["selected"] = dict.fromkeys(vfy_ids, "passed")
    data["outcomes"]["vfy_default"] = dict(data["outcomes"]["selected"])
    data["outcomes"]["testmon"] = dict.fromkeys(testmon_ids, "passed")
    # Historical producer prose can claim equality even when its structured policy does not.
    data["methodology"]["validation"] = "recorded prose claims exact selected pytest IDs"
    write(path, data)
    index = build_index([path], [], "2026-10-17")
    for row in index["results"]:
        limits = row["semantic_limits"]
        selected = limits["affected_selection"]
        assert selected["declared_policy"] == policy
        assert selected["observed_relation"] == relation
        assert selected["vfy_test_ids"] == vfy_ids
        assert selected["testmon_test_ids"] == testmon_ids
        assert selected["vfy_outcomes"] == data["outcomes"]["selected"]
        assert "validation" not in limits["methodology"]


@pytest.mark.parametrize(
    ("policy", "vfy_ids", "testmon_ids"),
    [
        ("exact", ["test_a", "test_b"], ["test_a"]),
        ("vfy_conservative", ["test_a"], ["test_a", "test_b"]),
        ("vfy_conservative", ["test_a"], ["test_b"]),
    ],
)
def test_edit_loop_index_rejects_falsified_exact_or_conservative_selection(
    tmp_path: Path, policy: str, vfy_ids: list[str], testmon_ids: list[str]
) -> None:
    path = tmp_path / "result.json"
    data = edit_loop()
    data["selection_vs_testmon"] = policy
    data["selected_tests"] = vfy_ids
    data["outcomes"]["selected"] = dict.fromkeys(vfy_ids, "passed")
    data["outcomes"]["vfy_default"] = dict(data["outcomes"]["selected"])
    data["outcomes"]["testmon"] = dict.fromkeys(testmon_ids, "passed")
    write(path, data)
    with pytest.raises(ValueError, match=r"exact selection|conservative inclusion"):
        build_index([path], [], "2026-10-17")


def test_edit_loop_index_rejects_selected_ids_that_disagree_with_outcomes(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    data = edit_loop()
    data["selected_tests"] = ["test_b"]
    write(path, data)
    with pytest.raises(ValueError, match="selected test IDs differ from vfy outcomes"):
        build_index([path], [], "2026-10-17")
