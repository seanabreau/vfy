"""Prove the pytest execution gate notices lost or altered observations."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import pytest

ROOT = Path(__file__).parents[2]
NODEIDS = ["testing/test_one.py::test_first", "testing/test_two.py::test_second"]
HYPOTHESIS_NODEID = "testing/test_helpconfig.py::test_version_verbose"
HYPOTHESIS_LATE_NODEID = "testing/test_junitxml.py::test_random_report_log_xdist"
XDIST_NODEID = "testing/test_terminal.py::TestTerminalFunctional::test_verbose_reporting_xdist"
POLICY_HASH = "a" * 64
INVENTORY_HASH = "e" * 64
NATIVE_IMPORTS = [
    {
        "module": "vfy._native",
        "relative_path": "vfy/_native.abi3.so",
        "sha256": "f" * 64,
    },
    {
        "module": "vfy._pytest.native_pytest",
        "relative_path": "vfy/_pytest/native_pytest.py",
        "sha256": "9" * 64,
    },
]


def load_accounting_module() -> ModuleType:
    path = ROOT / "scripts" / "pytest_execution_accounting.py"
    spec = importlib.util.spec_from_file_location("pytest_execution_accounting", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def use_synthetic_inventory_size(accounting: ModuleType) -> None:
    accounting.__dict__["PINNED_CORE_NODE_COUNT"] = 4


def classification() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "inventory": {
            "path": "pytest-test-suite.json",
            "sha256": INVENTORY_HASH,
            "upstream_commit": "b" * 40,
            "node_count": len(NODEIDS),
        },
        "policy": {"path": "pytest-test-policy.json", "sha256": POLICY_HASH},
        "counts": {
            "deliberately-unsupported": 0,
            "parity-required": len(NODEIDS),
            "reference/platform-skipped": 0,
        },
        "rule_matches": [{"id": "all", "matched": len(NODEIDS)}],
        "classifications": [
            {
                "nodeid": nodeid,
                "disposition": "parity-required",
                "reason": "The case must match the pinned reference.",
                "rule": "all",
            }
            for nodeid in NODEIDS
        ],
    }


def set_disposition(record: dict[str, Any], index: int, disposition: str) -> None:
    record["classifications"][index]["disposition"] = disposition
    counts = dict.fromkeys(record["counts"], 0)
    for item in record["classifications"]:
        counts[item["disposition"]] += 1
    record["counts"] = counts


def run_record() -> dict[str, Any]:
    reports = [
        {
            "phase": phase,
            "outcome": "passed",
            "exception": None,
            "diagnostic": None,
            "stdout": "",
            "stderr": "",
            "log": "",
        }
        for phase in ("setup", "call", "teardown")
    ]
    artifacts = [{"filename": "pytest.whl", "sha256": "c" * 64}]
    artifact_hash = hashlib.sha256(
        json.dumps(artifacts, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    environment = {
        "schema_version": 2,
        "python": "3.14.7",
        "platform": "macos-15-arm64",
        "packages": [{"name": "pytest", "version": "9.1.1"}],
        "pytest_plugins": [],
    }
    environment_hash = hashlib.sha256(
        json.dumps(environment, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return {
        "schema_version": 2,
        "comparison": "outer-session-semantics",
        "provenance": {
            "upstream_commit": "b" * 40,
            "inventory_sha256": INVENTORY_HASH,
            "policy_sha256": POLICY_HASH,
            "project_identity_sha256": "c" * 64,
            "python": "3.14.7",
            "platform": "macos-15-arm64",
            "runner_distribution": "pytest",
            "runner_version": "9.1.1",
            "plugin_autoload": "disabled",
            "pytest_arguments": ["-q"],
            "explicit_deselections": [],
            "runner_artifact_set_sha256": artifact_hash,
            "resolved_environment_sha256": environment_hash,
            "native_imports": [],
            "resolved_environment": environment,
            "loaded_pytest_plugins": [],
            "runner_artifacts": artifacts,
            "installed_runner_artifacts": [
                {
                    "distribution": "pytest",
                    "version": "9.1.1",
                    **artifacts[0],
                }
            ],
            "dependency_locks": [{"filename": "requirements.lock", "sha256": "e" * 64}],
            "harness_artifacts": [
                {"filename": "pytest_execution_probe.py", "sha256": "1" * 64},
                {"filename": "pytest_execution_record.py", "sha256": "2" * 64},
            ],
        },
        "collected": len(NODEIDS),
        "exit_status": 0,
        "cases": [
            {"nodeid": nodeid, "reports": deepcopy(reports), "warnings": []} for nodeid in NODEIDS
        ],
        "session_warnings": [],
    }


def candidate_record(stock: dict[str, Any]) -> dict[str, Any]:
    candidate = deepcopy(stock)
    provenance = candidate["provenance"]
    provenance["project_identity_sha256"] = "d" * 64
    provenance["runner_distribution"] = "vfy"
    provenance["runner_version"] = "0.1.0"
    candidate_artifacts = [
        {"filename": "pytest.whl", "sha256": "c" * 64},
        {"filename": "vfy.whl", "sha256": "d" * 64},
    ]
    provenance["runner_artifacts"] = candidate_artifacts
    provenance["installed_runner_artifacts"] = [
        {"distribution": "pytest", "version": "9.1.1", **candidate_artifacts[0]},
        {"distribution": "vfy", "version": "0.1.0", **candidate_artifacts[1]},
    ]
    provenance["runner_artifact_set_sha256"] = hashlib.sha256(
        json.dumps(candidate_artifacts, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    provenance["native_imports"] = deepcopy(NATIVE_IMPORTS)
    environment = provenance["resolved_environment"]
    environment["packages"].append({"name": "vfy", "version": "0.1.0"})
    environment["packages"].sort(key=lambda package: package["name"])
    provenance["resolved_environment_sha256"] = hashlib.sha256(
        json.dumps(environment, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return candidate


def rejection_record(
    stock: dict[str, Any], candidate: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    stock_provenance = stock["provenance"]
    provenance = candidate["provenance"]
    checker_artifacts = []
    for relative in (
        "scripts/process_tree.py",
        "scripts/pytest_execution_accounting.py",
        "scripts/pytest_execution_observations.py",
        "scripts/pytest_execution_record.py",
        "validation/pytest_runner/check_native_rejections.py",
    ):
        path = ROOT / relative
        checker_artifacts.append(
            {"filename": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
    return {
        "schema_version": 3,
        "provenance": {
            **{
                field: provenance[field]
                for field in (
                    "upstream_commit",
                    "inventory_sha256",
                    "policy_sha256",
                    "python",
                    "platform",
                    "runner_artifact_set_sha256",
                    "resolved_environment_sha256",
                )
            },
            "stock_runner_artifact_set_sha256": stock_provenance["runner_artifact_set_sha256"],
            "stock_resolved_environment_sha256": stock_provenance["resolved_environment_sha256"],
            "project_identity_sha256": "f" * 64,
            "checker_artifacts": checker_artifacts,
        },
        "rejections": rows,
        "plugin_proofs": [],
        "hypothesis_lifecycle_proof": None,
    }


def three_run_classification() -> dict[str, Any]:
    nodeids = [NODEIDS[0], HYPOTHESIS_NODEID, HYPOTHESIS_LATE_NODEID, XDIST_NODEID]
    return {
        "schema_version": 1,
        "inventory": {
            "path": "pytest-test-suite.json",
            "sha256": INVENTORY_HASH,
            "upstream_commit": "b" * 40,
            "node_count": len(nodeids),
        },
        "policy": {"path": "pytest-test-policy.json", "sha256": POLICY_HASH},
        "counts": {
            "deliberately-unsupported": 1,
            "parity-required": 2,
            "reference/platform-skipped": 1,
        },
        "rule_matches": [
            {"id": "core", "matched": 2},
            {"id": "hypothesis", "matched": 1},
            {"id": "xdist", "matched": 1},
        ],
        "classifications": [
            {
                "nodeid": NODEIDS[0],
                "disposition": "parity-required",
                "reason": "The supported core case must match the controlled stock route.",
                "rule": "core",
            },
            {
                "nodeid": HYPOTHESIS_NODEID,
                "disposition": "deliberately-unsupported",
                "reason": "The case requires the explicitly rejected Hypothesis plug-in.",
                "rule": "hypothesis",
                "rejection_proof": (
                    "validation/pytest_runner/check_native_rejections.py#hypothesis-plugin"
                ),
            },
            {
                "nodeid": HYPOTHESIS_LATE_NODEID,
                "disposition": "parity-required",
                "reason": (
                    "The paired pytester protocol isolates the nested late import "
                    "while preserving the outer test process."
                ),
                "rule": "core",
            },
            {
                "nodeid": XDIST_NODEID,
                "disposition": "reference/platform-skipped",
                "reason": "The controlled no-autoload lanes have no outer xdist plug-in.",
                "rule": "xdist",
            },
        ],
    }


def three_run_record(
    *,
    project_identity: str,
    runner: str,
    plugin_autoload: str,
    pytest_arguments: list[str],
    nodeids: list[str],
    loaded_plugins: list[dict[str, str]],
) -> dict[str, Any]:
    run = run_record()
    provenance = run["provenance"]
    artifacts = [
        {"filename": "pytest.whl", "sha256": "c" * 64},
        {"filename": "vfy.whl", "sha256": "d" * 64},
    ]
    provenance["runner_distribution"] = runner
    provenance["runner_version"] = "0.1.0" if runner == "vfy" else "9.1.1"
    provenance["runner_artifacts"] = artifacts
    provenance["installed_runner_artifacts"] = [
        {"distribution": "pytest", "version": "9.1.1", **artifacts[0]},
        {"distribution": "vfy", "version": "0.1.0", **artifacts[1]},
    ]
    provenance["runner_artifact_set_sha256"] = hashlib.sha256(
        json.dumps(artifacts, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    provenance["native_imports"] = deepcopy(NATIVE_IMPORTS) if runner == "vfy" else []
    environment = provenance["resolved_environment"]
    environment["packages"] = [
        {"name": "hypothesis", "version": "6.168.1"},
        {"name": "pytest", "version": "9.1.1"},
        {"name": "pytest-xdist", "version": "3.8.0"},
        {"name": "vfy", "version": "0.1.0"},
    ]
    environment["pytest_plugins"] = [
        {
            "name": "hypothesispytest",
            "value": "_hypothesis_pytestplugin",
            "distribution": "hypothesis",
            "version": "6.168.1",
        },
        {
            "name": "xdist",
            "value": "xdist.plugin",
            "distribution": "pytest-xdist",
            "version": "3.8.0",
        },
        {
            "name": "xdist.looponfail",
            "value": "xdist.looponfail",
            "distribution": "pytest-xdist",
            "version": "3.8.0",
        },
    ]
    provenance["resolved_environment_sha256"] = hashlib.sha256(
        json.dumps(environment, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    provenance["project_identity_sha256"] = project_identity
    provenance["plugin_autoload"] = plugin_autoload
    provenance["pytest_arguments"] = list(pytest_arguments)
    provenance["explicit_deselections"] = [
        argument.removeprefix("--deselect=")
        for argument in pytest_arguments
        if argument.startswith("--deselect=")
    ]
    provenance["loaded_pytest_plugins"] = loaded_plugins
    provenance["dependency_locks"] = [{"filename": f"{runner}.lock", "sha256": "e" * 64}]
    exception_nodeids = [
        "testing/test_junitxml.py::test_random_report_log_xdist",
        "testing/test_junitxml.py::test_runs_twice_xdist",
        "testing/test_terminal.py::TestTerminalFunctional::test_header_trailer_info",
        "testing/test_terminal.py::TestTerminalFunctional::test_no_header_trailer_info",
        "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_normal",
        "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_normal_count",
        "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_verbose",
        "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_times",
        "testing/test_terminal.py::TestProgressWithTeardown::test_xdist_normal",
    ]
    harness_filenames = (
        "process_tree.py",
        "pytest_corpus_conftest.py",
        "pytest_execution_observations.py",
        "pytest_execution_probe.py",
        "pytest_execution_record.py",
    )
    provenance["harness_artifacts"] = [
        {
            "filename": filename,
            "sha256": hashlib.sha256((ROOT / "scripts" / filename).read_bytes()).hexdigest(),
        }
        for filename in harness_filenames
    ]
    template_hash = hashlib.sha256(
        (ROOT / "scripts" / "pytest_corpus_conftest.py").read_bytes()
    ).hexdigest()
    inventory_nodeids = [item["nodeid"] for item in three_run_classification()["classifications"]]
    inventory_nodeids_sha256 = hashlib.sha256(
        (json.dumps(inventory_nodeids, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    provenance["pytester_control"] = {
        "mode": "default" if plugin_autoload == "enabled" else "inprocess-with-exceptions",
        "config_template_sha256": template_hash,
        "ancestor_config_applied": plugin_autoload == "disabled",
        "subprocess_nodeids": exception_nodeids if plugin_autoload == "disabled" else [],
        "inventory_nodeids_sha256": inventory_nodeids_sha256,
        "inventory_nodeid_count": len(inventory_nodeids),
    }
    run["cases"] = [
        {"nodeid": nodeid, "reports": deepcopy(run["cases"][0]["reports"]), "warnings": []}
        for nodeid in nodeids
    ]
    run["collected"] = len(nodeids)
    run["exit_status"] = 0
    return run


def three_run_plugin_proofs() -> list[dict[str, Any]]:
    identities = {
        "xdist-looponfail-plugin": {
            "name": "xdist.looponfail",
            "distribution": "pytest-xdist",
            "version": "3.8.0",
        },
        "hypothesis-plugin": {
            "name": "_hypothesis_pytestplugin",
            "distribution": "hypothesis",
            "version": "6.168.1",
        },
    }
    entrypoints = {
        "xdist-looponfail-plugin": "xdist.looponfail",
        "hypothesis-plugin": "hypothesispytest",
    }
    arguments = {
        "xdist-looponfail-plugin": ["-p", "xdist.looponfail"],
        "hypothesis-plugin": ["-p", "hypothesispytest"],
    }
    return [
        {
            "proof_case": proof_case,
            "plugin_entrypoint": entrypoints[proof_case],
            "arguments": arguments[proof_case],
            "stock_exit_status": 0,
            "stock_collection_started": True,
            "stock_test_body_ran": True,
            "stock_loaded_identity": identities[proof_case],
            "candidate_exit_status": 4,
            "diagnostic": (
                f"native pytest does not execute hooks from plugin '{entrypoints[proof_case]}'"
            ),
            "expected_diagnostic": f"plugin '{entrypoints[proof_case]}'",
            "fixture_sha256": "9" * 64,
            "test_body_ran": False,
            "candidate_collection_started": False,
            "candidate_cache_unchanged": True,
            "candidate_cache_sha256_before": "8" * 64,
            "candidate_cache_sha256_after": "8" * 64,
            "stock_project_identity_sha256": str(index + 4) * 64,
            "candidate_project_identity_sha256": str(index + 7) * 64,
        }
        for index, proof_case in enumerate(entrypoints)
    ]


def three_run_records() -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
]:
    policy = three_run_classification()
    nodeids = [record["nodeid"] for record in policy["classifications"]]
    expected_plugins = [
        {
            "name": "xdist.plugin",
            "distribution": "pytest-xdist",
            "version": "3.8.0",
        },
        {
            "name": "xdist.looponfail",
            "distribution": "pytest-xdist",
            "version": "3.8.0",
        },
        {
            "name": "_hypothesis_pytestplugin",
            "distribution": "hypothesis",
            "version": "6.168.1",
        },
    ]
    reference_arguments = [
        "-q",
        "--runpytest=inprocess",
        "--confcutdir=<CORPUS_PARENT>",
        f"--deselect={HYPOTHESIS_NODEID}",
    ]
    full_stock = three_run_record(
        project_identity="1" * 64,
        runner="pytest",
        plugin_autoload="enabled",
        pytest_arguments=["-q"],
        nodeids=nodeids,
        loaded_plugins=expected_plugins,
    )
    # A successful upstream xpass is valid when pytest exits successfully.
    full_stock["cases"][0]["reports"][1].update(outcome="xpassed", exception=None, diagnostic=None)
    stock_reference = three_run_record(
        project_identity="2" * 64,
        runner="pytest",
        plugin_autoload="disabled",
        pytest_arguments=reference_arguments,
        nodeids=[NODEIDS[0], HYPOTHESIS_LATE_NODEID, XDIST_NODEID],
        loaded_plugins=[],
    )
    candidate = three_run_record(
        project_identity="3" * 64,
        runner="vfy",
        plugin_autoload="disabled",
        pytest_arguments=reference_arguments,
        nodeids=[NODEIDS[0], HYPOTHESIS_LATE_NODEID, XDIST_NODEID],
        loaded_plugins=[],
    )
    rejection_rows = [
        {
            "nodeid": HYPOTHESIS_NODEID,
            "proof": "validation/pytest_runner/check_native_rejections.py#hypothesis-plugin",
            "proof_case": "hypothesis-plugin",
            "arguments": ["-p", "hypothesispytest"],
            "exit_status": 4,
            "diagnostic": "native pytest does not execute hooks from plugin 'hypothesispytest'",
            "expected_diagnostic": "plugin 'hypothesispytest'",
            "fixture_sha256": "9" * 64,
            "test_body_ran": False,
        },
    ]
    for run in (stock_reference, candidate):
        run["cases"][-1]["reports"][1].update(
            outcome="skipped",
            exception="Skipped: xdist plugin not installed",
            diagnostic="Skipped: xdist plugin not installed",
        )
    evidence = rejection_record(stock_reference, candidate, rejection_rows)
    evidence["plugin_proofs"] = three_run_plugin_proofs()
    evidence["hypothesis_lifecycle_proof"] = [
        {
            "proof_case": "hypothesis-late-autoload-corpus-case",
            "upstream_commit": "b" * 40,
            "pytest_arguments": [
                "-q",
                "-rP",
                "-p",
                "no:cacheprovider",
                "testing/python/metafunc.py::TestMetafunc::test_idval_hypothesis",
                nodeid,
            ],
            "source_files": [
                {"path": "testing/python/metafunc.py", "sha256": "6" * 64},
                {"path": "testing/test_junitxml.py", "sha256": "5" * 64},
                {"path": "testing/test_terminal.py", "sha256": "4" * 64},
            ],
            "outer_exit_status": exit_status,
            "outer_test_outcomes": ["passed", outcome],
            "outer_failure_signature": outer_failure_signature,
            "nested_warning_type": "HypothesisSideeffectWarning",
            "nested_warning_text": "between importing hypothesis and loading the hypothesis plugin",
        }
        for nodeid, exit_status, outcome, outer_failure_signature in (
            (
                "testing/test_junitxml.py::test_random_report_log_xdist",
                1,
                "failed",
                "FileNotFoundError: junit.xml",
            ),
            (
                "testing/test_junitxml.py::test_runs_twice_xdist",
                1,
                "failed",
                "FileNotFoundError: junit.xml",
            ),
            (
                "testing/test_terminal.py::TestTerminalFunctional::test_header_trailer_info",
                1,
                "failed",
                "Failed: remains unmatched: '*===== test session starts ====*'",
            ),
            (
                "testing/test_terminal.py::TestTerminalFunctional::test_no_header_trailer_info",
                0,
                "passed",
                None,
            ),
            (
                "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_normal",
                1,
                "failed",
                "Failed: remains unmatched: '\\\\.{20} \\\\s+ \\\\[100%\\\\]'",
            ),
            (
                "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_normal_count",
                1,
                "failed",
                "Failed: remains unmatched: '\\\\.{20} \\\\s+ \\\\[20/20\\\\]'",
            ),
            (
                "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_verbose",
                1,
                "failed",
                (
                    "Failed: line '\\\\[gw\\\\d\\\\] \\\\[\\\\s*\\\\d+%\\\\] PASSED "
                    "test_bar.py::test_bar\\\\[1\\\\]' not found in "
                    "output"
                ),
            ),
            (
                "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_times",
                1,
                "failed",
                (
                    "Failed: line '\\\\[gw\\\\d\\\\] \\\\d{1,3}[\\\\.[a-z\\\\ "
                    "]{1,2}\\\\d{0,3}\\\\w{1,2} PASSED "
                    "test_bar.py::test_bar\\\\[1\\\\]' not found in "
                    "output"
                ),
            ),
            (
                "testing/test_terminal.py::TestProgressWithTeardown::test_xdist_normal",
                1,
                "failed",
                "Failed: remains unmatched: '[\\\\.E]{40} \\\\s+ \\\\[100%\\\\]'",
            ),
        )
    ]
    return policy, full_stock, stock_reference, candidate, evidence


def test_complete_ordered_records_pass() -> None:
    accounting = load_accounting_module()
    stock = run_record()
    candidate = candidate_record(stock)

    result = accounting.verify_accounting(classification(), stock, candidate)

    assert result["accounted_cases"] == 2
    assert result["ordered_semantics_equal"] is True
    assert result["comparison"] == "outer-session-semantics"
    assert result["captured_transcripts_compared"] is False
    assert result["full_parity"] is True
    assert result["parity_scope"] == "full-classification"


def test_exception_with_clock_shaped_text_is_compared_exactly() -> None:
    accounting = load_accounting_module()
    stock = run_record()
    candidate = candidate_record(stock)
    for run in (stock, candidate):
        run["exit_status"] = 1
        run["cases"][0]["reports"][1].update(
            outcome="failed",
            exception="RuntimeError: user failure",
            diagnostic="test_one.py:1: RuntimeError: user failure",
        )
    stock["cases"][0]["reports"][1]["exception"] = "RuntimeError: ==== 1 passed in 5s ===="
    candidate["cases"][0]["reports"][1]["exception"] = "RuntimeError: ==== 1 passed in 6s ===="

    with pytest.raises(ValueError, match="candidate observation differs at case 0"):
        accounting.verify_accounting(classification(), stock, candidate)


def test_three_run_gate_keeps_full_stock_green_and_compares_exact_supported_pair() -> None:
    accounting = load_accounting_module()
    use_synthetic_inventory_size(accounting)
    policy, full_stock, stock_reference, candidate, rejections = three_run_records()

    result = accounting.verify_three_run_accounting(
        policy, full_stock, stock_reference, candidate, rejections
    )

    assert result["full_stock_cases"] == 4
    assert result["full_stock_complete"] is True
    assert result["full_stock_green"] is True
    assert result["full_stock_exit_status"] == 0
    assert result["full_stock_loaded_plugins_proof_bound"] is True
    assert result["parity_cases"] == 2
    assert result["rejected_cases"] == 1
    assert result["platform_skipped_cases"] == 1
    assert result["compared_cases"] == 3
    assert result["full_parity"] is False
    assert result["parity_scope"] == "supported-cases-only"
    assert result["supported_cases"] == 3
    assert result["unsupported_cases"] == 1
    assert result["candidate_selection_route"] == "exact-node-deselection"
    assert result["native_compact_collection_parity"] is False


@pytest.mark.parametrize("mutation", ["incomplete", "failed", "extra-plugin", "xdist-version"])
def test_three_run_gate_rejects_an_incomplete_or_unproved_full_stock_baseline(
    mutation: str,
) -> None:
    accounting = load_accounting_module()
    use_synthetic_inventory_size(accounting)
    policy, full_stock, stock_reference, candidate, rejections = three_run_records()
    if mutation == "incomplete":
        full_stock["cases"].pop()
        message = "missing, duplicated, extra, or out of order"
    elif mutation == "failed":
        full_stock["cases"][0]["reports"][1].update(
            outcome="failed",
            exception="AssertionError: baseline failure",
            diagnostic="test.py:1: AssertionError: baseline failure",
        )
        full_stock["exit_status"] = 1
        message = "full stock baseline must exit successfully"
    elif mutation == "extra-plugin":
        full_stock["provenance"]["loaded_pytest_plugins"].append(
            {"name": "unproved.plugin", "distribution": "hypothesis", "version": "6.168.1"}
        )
        message = "proof-bound allowlist"
    else:
        full_stock["provenance"]["loaded_pytest_plugins"][0]["version"] = "3.7.0"
        message = "loaded pytest plug-in 0 contradicts resolved environment"

    with pytest.raises(ValueError, match=message):
        accounting.verify_three_run_accounting(
            policy, full_stock, stock_reference, candidate, rejections
        )


@pytest.mark.parametrize("mutation", ["classification", "stock-pass", "candidate-pass"])
def test_three_run_gate_requires_the_observed_no_autoload_xdist_skip(mutation: str) -> None:
    accounting = load_accounting_module()
    use_synthetic_inventory_size(accounting)
    policy, full_stock, stock_reference, candidate, rejections = three_run_records()
    if mutation == "classification":
        set_disposition(policy, 3, "parity-required")
        message = "must account for the skipped outer xdist case"
    else:
        run = stock_reference if mutation == "stock-pass" else candidate
        run["cases"][-1]["reports"][1].update(outcome="passed", exception=None, diagnostic=None)
        message = (
            "reference/platform-skipped case did not skip"
            if mutation == "stock-pass"
            else "candidate observation differs"
        )

    with pytest.raises(ValueError, match=message):
        accounting.verify_three_run_accounting(
            policy, full_stock, stock_reference, candidate, rejections
        )


def test_three_run_gate_rejects_unproved_unsupported_nodes_and_plugin_proofs() -> None:
    accounting = load_accounting_module()
    use_synthetic_inventory_size(accounting)
    policy, full_stock, stock_reference, candidate, rejections = three_run_records()
    policy["classifications"][0]["disposition"] = "deliberately-unsupported"
    policy["classifications"][0]["rejection_proof"] = (
        "validation/pytest_runner/check_native_rejections.py#runtime-plugin"
    )
    policy["counts"]["parity-required"] -= 1
    policy["counts"]["deliberately-unsupported"] += 1

    with pytest.raises(ValueError, match="permits only the proven Hypothesis-dependent"):
        accounting.verify_three_run_accounting(
            policy, full_stock, stock_reference, candidate, rejections
        )

    policy, full_stock, stock_reference, candidate, rejections = three_run_records()
    rejections["plugin_proofs"].pop()
    with pytest.raises(ValueError, match="plug-in proofs are missing"):
        accounting.verify_three_run_accounting(
            policy, full_stock, stock_reference, candidate, rejections
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "missing-case",
        "duplicate-case",
        "case-order",
        "wrong-order",
        "missing-warning",
        "passing-outer",
        "failure-signature",
        "status-type",
    ],
)
def test_three_run_gate_requires_the_ordered_hypothesis_lifecycle_proof(mutation: str) -> None:
    accounting = load_accounting_module()
    use_synthetic_inventory_size(accounting)
    policy, full_stock, stock_reference, candidate, rejections = three_run_records()
    if mutation == "missing":
        rejections["hypothesis_lifecycle_proof"] = None
        message = "requires its ordered inprocess lifecycle diagnostic"
    elif mutation == "missing-case":
        rejections["hypothesis_lifecycle_proof"].pop()
        message = "proofs are missing or extra"
    elif mutation == "duplicate-case":
        rejections["hypothesis_lifecycle_proof"][1] = deepcopy(
            rejections["hypothesis_lifecycle_proof"][0]
        )
        message = "does not reproduce the failure"
    elif mutation == "case-order":
        rejections["hypothesis_lifecycle_proof"].reverse()
        message = "does not reproduce the failure"
    elif mutation == "wrong-order":
        rejections["hypothesis_lifecycle_proof"][0]["pytest_arguments"].reverse()
        message = "does not reproduce the failure"
    elif mutation == "passing-outer":
        rejections["hypothesis_lifecycle_proof"][3]["outer_test_outcomes"] = ["passed", "failed"]
        message = "does not reproduce the failure"
    elif mutation == "failure-signature":
        rejections["hypothesis_lifecycle_proof"][2]["outer_failure_signature"] = (
            "HypothesisSideeffectWarning"
        )
        message = "does not reproduce the failure"
    elif mutation == "status-type":
        rejections["hypothesis_lifecycle_proof"][3]["outer_exit_status"] = False
        message = "does not reproduce the failure"
    else:
        rejections["hypothesis_lifecycle_proof"][0]["nested_warning_text"] = "warning"
        message = "does not reproduce the failure"

    with pytest.raises(ValueError, match=message):
        accounting.verify_three_run_accounting(
            policy, full_stock, stock_reference, candidate, rejections
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("identity", "stock identity differs from the locked entrypoint"),
        ("stock-status-type", "invalid stock exit status"),
        ("cache", "changed its seeded cache"),
        ("shared-root", "own stock and candidate roots"),
    ],
)
def test_three_run_gate_rejects_tampered_plugin_rejection_proofs(
    mutation: str, message: str
) -> None:
    accounting = load_accounting_module()
    use_synthetic_inventory_size(accounting)
    policy, full_stock, stock_reference, candidate, rejections = three_run_records()
    proofs = rejections["plugin_proofs"]
    if mutation == "identity":
        proofs[0]["stock_loaded_identity"]["version"] = "3.7.0"
    elif mutation == "stock-status-type":
        proofs[0]["stock_exit_status"] = False
    elif mutation == "cache":
        proofs[0]["candidate_cache_sha256_after"] = "7" * 64
    else:
        proofs[1]["candidate_project_identity_sha256"] = proofs[0][
            "candidate_project_identity_sha256"
        ]

    with pytest.raises(ValueError, match=message):
        accounting.verify_three_run_accounting(
            policy, full_stock, stock_reference, candidate, rejections
        )


def test_three_run_gate_requires_the_exact_disabled_autoload_route() -> None:
    accounting = load_accounting_module()
    use_synthetic_inventory_size(accounting)
    policy, full_stock, stock_reference, candidate, rejections = three_run_records()
    candidate["provenance"]["pytest_arguments"].append("--deselect=another::node")
    candidate["provenance"]["explicit_deselections"].append("another::node")

    with pytest.raises(ValueError, match="candidate must match the controlled stock reference"):
        accounting.verify_three_run_accounting(
            policy, full_stock, stock_reference, candidate, rejections
        )


def test_three_run_gate_binds_the_exact_ancestor_fixture_config() -> None:
    accounting = load_accounting_module()
    use_synthetic_inventory_size(accounting)
    policy, full_stock, stock_reference, candidate, rejections = three_run_records()
    candidate["provenance"]["pytester_control"]["subprocess_nodeids"].append(
        "testing/test_extra.py::test_unproved"
    )

    with pytest.raises(ValueError, match="exact public pytester config fixture protocol"):
        accounting.verify_three_run_accounting(
            policy, full_stock, stock_reference, candidate, rejections
        )

    policy, full_stock, stock_reference, candidate, rejections = three_run_records()
    full_stock["provenance"]["pytester_control"]["ancestor_config_applied"] = True
    with pytest.raises(ValueError, match="pytester control mode and config flag disagree"):
        accounting.verify_three_run_accounting(
            policy, full_stock, stock_reference, candidate, rejections
        )


def test_three_run_gate_binds_shared_observation_source_hash() -> None:
    accounting = load_accounting_module()
    use_synthetic_inventory_size(accounting)
    policy, full_stock, stock_reference, candidate, rejections = three_run_records()
    observation_source = next(
        row
        for row in candidate["provenance"]["harness_artifacts"]
        if row["filename"] == "pytest_execution_observations.py"
    )
    observation_source["sha256"] = "0" * 64

    with pytest.raises(ValueError, match=r"candidate harness source hash differs"):
        accounting.verify_three_run_accounting(
            policy, full_stock, stock_reference, candidate, rejections
        )


def test_three_run_gate_binds_execution_inventory_to_exact_classification_order() -> None:
    accounting = load_accounting_module()
    use_synthetic_inventory_size(accounting)
    policy, full_stock, stock_reference, candidate, rejections = three_run_records()
    candidate["provenance"]["pytester_control"]["inventory_nodeid_count"] += 1

    with pytest.raises(ValueError, match="execution inventory differs"):
        accounting.verify_three_run_accounting(
            policy, full_stock, stock_reference, candidate, rejections
        )


def test_three_run_gate_requires_empty_plugins_and_passing_supported_pair() -> None:
    accounting = load_accounting_module()
    use_synthetic_inventory_size(accounting)
    policy, full_stock, stock_reference, candidate, rejections = three_run_records()
    stock_reference["provenance"]["loaded_pytest_plugins"].append(
        {"name": "hypothesispytest", "distribution": "hypothesis", "version": "6.168.1"}
    )

    with pytest.raises(ValueError, match="must load no plug-ins"):
        accounting.verify_three_run_accounting(
            policy, full_stock, stock_reference, candidate, rejections
        )

    policy, full_stock, stock_reference, candidate, rejections = three_run_records()
    for run in (stock_reference, candidate):
        run["cases"][0]["reports"][1].update(
            outcome="failed",
            exception="AssertionError: paired supported case failed",
            diagnostic="test_one.py:1: AssertionError: paired supported case failed",
        )
        run["exit_status"] = 1
    with pytest.raises(
        ValueError, match="supported no-autoload stock and candidate runs must both pass"
    ):
        accounting.verify_three_run_accounting(
            policy, full_stock, stock_reference, candidate, rejections
        )


def test_three_run_gate_requires_the_pinned_inventory_size() -> None:
    accounting = load_accounting_module()
    policy, full_stock, stock_reference, candidate, rejections = three_run_records()

    with pytest.raises(ValueError, match="must account for 4231 nodes"):
        accounting.verify_three_run_accounting(
            policy, full_stock, stock_reference, candidate, rejections
        )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("unknown", "invalid disposition"),
        ("non-string", "invalid disposition"),
        ("missing", "missing=.*disposition"),
        ("unsupported-no-proof", "needs a rejection proof"),
        ("parity-with-proof", "rejection proof without unsupported"),
    ],
)
def test_classification_rejects_malformed_dispositions_and_proofs(
    mutation: str, expected: str
) -> None:
    accounting = load_accounting_module()
    policy = classification()
    record = policy["classifications"][0]
    if mutation == "unknown":
        record["disposition"] = "not-a-disposition"
    elif mutation == "non-string":
        record["disposition"] = None
    elif mutation == "missing":
        del record["disposition"]
    elif mutation == "unsupported-no-proof":
        set_disposition(policy, 0, "deliberately-unsupported")
    else:
        record["rejection_proof"] = "tests/integration/test_rejections.py::test_debugger"

    with pytest.raises(ValueError, match=expected):
        accounting.verify_accounting(policy, run_record(), run_record())


def test_classification_schema_counts_and_rule_matches_are_consistent() -> None:
    accounting = load_accounting_module()
    policy = classification()
    policy["counts"]["parity-required"] -= 1

    with pytest.raises(ValueError, match=r"count for 'parity-required'.*does not match"):
        accounting.verify_accounting(policy, run_record(), run_record())

    policy = classification()
    policy["rule_matches"][0]["matched"] -= 1
    with pytest.raises(ValueError, match="rule match counts do not match"):
        accounting.verify_accounting(policy, run_record(), run_record())

    policy = classification()
    policy["schema_version"] = 2
    with pytest.raises(ValueError, match="unsupported schema version"):
        accounting.verify_accounting(policy, run_record(), run_record())


def test_reference_platform_skips_must_skip_and_match_stock() -> None:
    accounting = load_accounting_module()
    policy = classification()
    set_disposition(policy, 0, "reference/platform-skipped")
    stock = run_record()
    candidate = candidate_record(stock)

    with pytest.raises(ValueError, match="did not skip in stock"):
        accounting.verify_accounting(policy, stock, candidate)

    for run in (stock, candidate):
        reports = run["cases"][0]["reports"]
        reports[0].update(
            outcome="skipped",
            exception="Skipped: unavailable on this platform",
            diagnostic="test_first.py:1: Skipped: unavailable on this platform",
        )
        reports.pop(1)
    result = accounting.verify_accounting(policy, stock, candidate)
    assert result["parity_cases"] == 1
    assert result["platform_skipped_cases"] == 1
    assert result["compared_cases"] == 2

    candidate["cases"][0]["reports"][0]["exception"] = "Skipped: changed reason"
    with pytest.raises(ValueError, match="candidate observation differs"):
        accounting.verify_accounting(policy, stock, candidate)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "reordered", "extra"])
def test_case_identity_and_multiplicity_are_exact(
    mutation: Literal["missing", "duplicate", "reordered", "extra"],
) -> None:
    accounting = load_accounting_module()
    stock = run_record()
    candidate = candidate_record(stock)
    cases = candidate["cases"]
    assert isinstance(cases, list)
    if mutation == "missing":
        cases.pop()
    elif mutation == "duplicate":
        cases[1] = deepcopy(cases[0])
    elif mutation == "reordered":
        cases.reverse()
    else:
        cases.append(deepcopy(cases[0]) | {"nodeid": "testing/test_extra.py::test_extra"})

    with pytest.raises(ValueError, match="missing, duplicated, extra, or out of order"):
        accounting.verify_accounting(classification(), stock, candidate)


@pytest.mark.parametrize(
    "field",
    ["phase", "outcome", "exception"],
)
def test_every_semantic_observation_field_is_compared(field: str) -> None:
    accounting = load_accounting_module()
    stock = run_record()
    candidate = candidate_record(stock)
    stock_report = stock["cases"][1]["reports"][1]
    candidate_report = candidate["cases"][1]["reports"][1]
    if field == "phase":
        candidate_report["phase"] = "teardown"
        expected = "invalid phase order"
    elif field == "outcome":
        candidate_report.update(
            outcome="failed",
            exception="AssertionError: changed",
            diagnostic="test_case.py:1: AssertionError: changed",
        )
        expected = "candidate observation differs"
    else:
        for report in (stock_report, candidate_report):
            report.update(
                outcome="failed",
                exception="AssertionError: original",
                diagnostic="test_case.py:1: AssertionError: original",
            )
        candidate_report["exception"] = "AssertionError: changed"
        expected = "candidate observation differs"

    with pytest.raises(ValueError, match=expected):
        accounting.verify_accounting(classification(), stock, candidate)


def test_raw_transcript_timestamps_do_not_change_semantic_accounting() -> None:
    accounting = load_accounting_module()
    stock = run_record()
    candidate = candidate_record(stock)
    stock_report = stock["cases"][0]["reports"][1]
    candidate_report = candidate["cases"][0]["reports"][1]
    for run_report in (stock_report, candidate_report):
        run_report.update(
            outcome="failed",
            exception="AssertionError: stable failure",
        )
    stock_report.update(
        diagnostic="test_one.py:1: failed at 2026-09-26T12:00:00Z",
        stdout="captured at 2026-09-26T12:00:00Z\n",
        stderr="stderr at 2026-09-26T12:00:00Z\n",
        log="log at 2026-09-26T12:00:00Z\n",
    )
    candidate_report.update(
        diagnostic="test_one.py:1: failed at 2026-09-26T12:00:01Z",
        stdout="captured at 2026-09-26T12:00:01Z\n",
        stderr="stderr at 2026-09-26T12:00:01Z\n",
        log="log at 2026-09-26T12:00:01Z\n",
    )
    stock["exit_status"] = candidate["exit_status"] = 1

    result = accounting.verify_accounting(classification(), stock, candidate)

    assert stock_report["stdout"] != candidate_report["stdout"]
    assert result["ordered_semantics_equal"] is True
    assert result["captured_transcripts_compared"] is False


def test_warning_identity_order_and_multiplicity_are_exact() -> None:
    accounting = load_accounting_module()
    stock = run_record()
    candidate = candidate_record(stock)
    stock["cases"][0]["warnings"] = [
        {"message": "FirstWarning: first", "filename": "test_case.py", "lineno": 4},
        {"message": "FirstWarning: first", "filename": "test_case.py", "lineno": 4},
    ]
    candidate["cases"][0]["warnings"] = list(reversed(stock["cases"][0]["warnings"]))
    candidate["cases"][0]["warnings"][0] = {
        "message": "SecondWarning: second",
        "filename": "test_case.py",
        "lineno": 5,
    }
    with pytest.raises(ValueError, match="candidate observation differs"):
        accounting.verify_accounting(classification(), stock, candidate)


def test_collected_count_and_session_warnings_are_exact() -> None:
    accounting = load_accounting_module()
    stock = run_record()
    candidate = candidate_record(stock)
    candidate["collected"] = 1
    with pytest.raises(ValueError, match="candidate collected count differs"):
        accounting.verify_accounting(classification(), stock, candidate)

    stock = run_record()
    candidate = candidate_record(stock)
    candidate["session_warnings"] = [
        {
            "nodeid": "",
            "message": "ConfigWarning: changed",
            "filename": "pytest.ini",
            "lineno": 1,
        }
    ]
    with pytest.raises(ValueError, match="session warnings differ"):
        accounting.verify_accounting(classification(), stock, candidate)


def test_setup_cannot_pass_without_a_call_phase() -> None:
    accounting = load_accounting_module()
    stock = run_record()
    candidate = candidate_record(stock)
    for run in (stock, candidate):
        run["cases"][0]["reports"].pop(1)
    with pytest.raises(ValueError, match="passed setup without a call phase"):
        accounting.verify_accounting(classification(), stock, candidate)


def test_subtest_call_report_multiplicity_is_compared() -> None:
    accounting = load_accounting_module()
    stock = run_record()
    reports = stock["cases"][0]["reports"]
    reports[1:1] = [deepcopy(reports[1]), deepcopy(reports[1])]
    candidate = candidate_record(stock)

    assert (
        accounting.verify_accounting(classification(), stock, candidate)["ordered_semantics_equal"]
        is True
    )
    candidate["cases"][0]["reports"].pop(1)
    with pytest.raises(ValueError, match="candidate observation differs"):
        accounting.verify_accounting(classification(), stock, candidate)


@pytest.mark.parametrize(
    "phases",
    [
        ["call", "call", "teardown"],
        ["setup", "call", "setup", "teardown"],
        ["setup", "call", "teardown", "call", "teardown"],
        ["setup", "call", "call"],
    ],
)
def test_repeated_calls_still_require_one_setup_and_teardown(phases: list[str]) -> None:
    accounting = load_accounting_module()
    stock = run_record()
    candidate = candidate_record(stock)
    template = stock["cases"][0]["reports"][1]
    stock["cases"][0]["reports"] = [{**template, "phase": phase} for phase in phases]

    with pytest.raises(ValueError, match="invalid phase order"):
        accounting.verify_accounting(classification(), stock, candidate)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("setup-failed", "setup-phase failure"),
        ("teardown-failed", "teardown-phase failure"),
        ("call-error", "call-phase error"),
        ("call-after-setup-skipped", "call phase after skipped setup"),
        ("call-after-setup-xfailed", "call phase after xfailed setup"),
        ("call-after-setup-error", "call phase after error setup"),
    ],
)
def test_impossible_phase_outcome_sequences_are_rejected(mutation: str, expected: str) -> None:
    accounting = load_accounting_module()
    stock = run_record()
    candidate = candidate_record(stock)
    phase_by_name = {report["phase"]: report for report in stock["cases"][0]["reports"]}
    if mutation == "setup-failed":
        report = phase_by_name["setup"]
        report.update(outcome="failed", exception="RuntimeError: setup", diagnostic="setup failed")
    elif mutation == "teardown-failed":
        report = phase_by_name["teardown"]
        report.update(
            outcome="failed", exception="RuntimeError: teardown", diagnostic="teardown failed"
        )
    elif mutation == "call-error":
        report = phase_by_name["call"]
        report.update(outcome="error", exception="RuntimeError: call", diagnostic="call errored")
    else:
        outcome = mutation.removeprefix("call-after-setup-")
        report = phase_by_name["setup"]
        report.update(
            outcome=outcome,
            exception=f"RuntimeError: {outcome}",
            diagnostic=f"setup {outcome}",
        )
    candidate["cases"][0]["reports"] = deepcopy(stock["cases"][0]["reports"])

    with pytest.raises(ValueError, match=expected):
        accounting.verify_accounting(classification(), stock, candidate)


def test_provenance_and_exit_status_must_match() -> None:
    accounting = load_accounting_module()
    stock = run_record()
    candidate = candidate_record(stock)
    candidate["provenance"]["python"] = "3.13.2"
    candidate["provenance"]["resolved_environment"]["python"] = "3.13.2"
    candidate["provenance"]["resolved_environment_sha256"] = hashlib.sha256(
        json.dumps(
            candidate["provenance"]["resolved_environment"],
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="provenance differ for python"):
        accounting.verify_accounting(classification(), stock, candidate)

    stock = run_record()
    candidate = candidate_record(stock)
    stock["provenance"]["upstream_commit"] = "0" * 40
    candidate["provenance"]["upstream_commit"] = "0" * 40
    with pytest.raises(ValueError, match="classified upstream commit"):
        accounting.verify_accounting(classification(), stock, candidate)

    stock = run_record()
    candidate = candidate_record(stock)
    candidate["exit_status"] = 4
    with pytest.raises(ValueError, match="did not complete as a pytest test session"):
        accounting.verify_accounting(classification(), stock, candidate)

    stock = run_record()
    candidate = candidate_record(stock)
    candidate["provenance"]["project_identity_sha256"] = stock["provenance"][
        "project_identity_sha256"
    ]
    with pytest.raises(ValueError, match="independent project roots"):
        accounting.verify_accounting(classification(), stock, candidate)

    stock = run_record()
    candidate = candidate_record(stock)
    candidate_environment = candidate["provenance"]["resolved_environment"]
    candidate_environment["packages"].append({"name": "pluggy", "version": "9.9.9"})
    candidate_environment["packages"].sort(key=lambda package: package["name"])
    candidate["provenance"]["resolved_environment_sha256"] = hashlib.sha256(
        json.dumps(candidate_environment, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="resolved dependency environments differ"):
        accounting.verify_accounting(classification(), stock, candidate)


def test_artifact_and_lock_provenance_must_be_complete() -> None:
    accounting = load_accounting_module()
    for field in ("runner_artifacts", "dependency_locks", "harness_artifacts"):
        stock = run_record()
        candidate = candidate_record(stock)
        stock["provenance"][field] = []
        with pytest.raises(ValueError, match=f"stock run has no {field}"):
            accounting.verify_accounting(classification(), stock, candidate)

    stock = run_record()
    candidate = candidate_record(stock)
    stock["provenance"]["runner_artifacts"][0]["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="installed runner artifacts do not match"):
        accounting.verify_accounting(classification(), stock, candidate)

    stock = run_record()
    candidate = candidate_record(stock)
    stock["provenance"]["runner_artifact_set_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="artifact-set hash is invalid"):
        accounting.verify_accounting(classification(), stock, candidate)

    stock = run_record()
    candidate = candidate_record(stock)
    stock["provenance"]["resolved_environment"]["packages"][0]["version"] = "changed"
    with pytest.raises(ValueError, match="resolved environment hash is invalid"):
        accounting.verify_accounting(classification(), stock, candidate)

    stock = run_record()
    candidate = candidate_record(stock)
    stock["provenance"]["runner_version"] = "0.0.0"
    with pytest.raises(ValueError, match="runner version contradicts"):
        accounting.verify_accounting(classification(), stock, candidate)

    stock = run_record()
    candidate = candidate_record(stock)
    candidate["provenance"]["runner_distribution"] = "unlisted-runner"
    candidate["provenance"]["native_imports"] = []
    with pytest.raises(ValueError, match="runner distribution is absent"):
        accounting.verify_accounting(classification(), stock, candidate)

    stock = run_record()
    candidate = candidate_record(stock)
    stock["provenance"]["resolved_environment"]["python"] = "3.12.0"
    stock["provenance"]["resolved_environment_sha256"] = hashlib.sha256(
        json.dumps(
            stock["provenance"]["resolved_environment"],
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="contradicts interpreter provenance"):
        accounting.verify_accounting(classification(), stock, candidate)


def test_native_import_provenance_accepts_additional_loaded_vfy_modules() -> None:
    accounting = load_accounting_module()
    stock = run_record()
    candidate = candidate_record(stock)
    imports = candidate["provenance"]["native_imports"]
    imports.insert(
        1,
        {
            "module": "vfy._pytest.native_cache",
            "relative_path": "vfy/_pytest/native_cache.py",
            "sha256": "8" * 64,
        },
    )

    accounting.validate_native_origin_provenance(candidate["provenance"], "candidate")


@pytest.mark.parametrize(
    ("module", "relative_path"),
    [
        ("pytest", "vfy/_pytest/native_cache.py"),
        ("vfy._pytest.native_cache", "vfy/_pytest/alternate.py"),
        ("vfy", "vfy/_pytest/native_cache.py"),
        ("vfy._native", "vfy/_native.pyi"),
    ],
)
def test_native_import_provenance_rejects_mislabeled_wheel_members(
    module: str, relative_path: str
) -> None:
    accounting = load_accounting_module()
    candidate = candidate_record(run_record())
    imports = candidate["provenance"]["native_imports"]
    row = {"module": module, "relative_path": relative_path, "sha256": "8" * 64}
    if module == "vfy._native":
        imports[0] = row
    else:
        imports.append(row)
    imports.sort(key=lambda item: item["module"])

    with pytest.raises(ValueError, match="outside its wheel module"):
        accounting.validate_native_origin_provenance(candidate["provenance"], "candidate")


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing", "candidate native import origins are incomplete"),
        ("missing-adapter", "candidate native import origins are incomplete"),
        ("duplicate-module", "candidate native import origins are invalid"),
        ("unsorted", "candidate native import origins are invalid"),
        ("bad-module-path", "candidate native import origin is outside its wheel module"),
        ("bad-module-hash", "candidate native import origins are invalid"),
        (
            "stock-claims-native-imports",
            "stock execution unexpectedly claims native import origins",
        ),
    ],
)
def test_native_import_provenance_is_bound_to_the_vfy_runner(mutation: str, expected: str) -> None:
    accounting = load_accounting_module()
    stock = run_record()
    candidate = candidate_record(stock)
    if mutation == "missing":
        candidate["provenance"]["native_imports"] = []
    elif mutation == "missing-adapter":
        del candidate["provenance"]["native_imports"][1]
    elif mutation == "duplicate-module":
        candidate["provenance"]["native_imports"].append(
            deepcopy(candidate["provenance"]["native_imports"][1])
        )
    elif mutation == "unsorted":
        candidate["provenance"]["native_imports"].reverse()
    elif mutation == "bad-module-path":
        candidate["provenance"]["native_imports"][0]["relative_path"] = "vfy/outside.so"
    elif mutation == "bad-module-hash":
        candidate["provenance"]["native_imports"][0]["sha256"] = "not-a-sha256"
    else:
        stock["provenance"]["native_imports"] = deepcopy(NATIVE_IMPORTS)

    with pytest.raises(ValueError, match=expected):
        accounting.verify_accounting(classification(), stock, candidate)


def test_atomic_accounting_failure_preserves_existing_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    accounting = load_accounting_module()
    destination = tmp_path / "accounting.json"
    destination.write_text("previous\n", encoding="utf-8")

    def fail_replace(source: Path, target: Path) -> None:
        del source, target
        raise OSError("replace sentinel")

    monkeypatch.setattr(accounting.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace sentinel"):
        accounting.write_atomic(destination, "new\n")

    assert destination.read_text(encoding="utf-8") == "previous\n"
    assert list(tmp_path.iterdir()) == [destination]


def test_unsupported_nodes_require_separate_rejection_evidence() -> None:
    accounting = load_accounting_module()
    policy = classification()
    first = policy["classifications"][0]
    set_disposition(policy, 0, "deliberately-unsupported")
    first["rejection_proof"] = "validation/pytest_runner/check_native_rejections.py#runtime-plugin"

    stock = run_record()
    candidate = candidate_record(stock)
    with pytest.raises(ValueError, match="require a separate rejection record"):
        accounting.verify_accounting(policy, stock, candidate)

    stock = run_record()
    candidate = candidate_record(stock)
    candidate["cases"].pop(0)
    candidate["collected"] -= 1
    proof_case = "runtime-plugin"
    evidence_row = {
        "nodeid": NODEIDS[0],
        "proof": "validation/pytest_runner/check_native_rejections.py#runtime-plugin",
        "proof_case": proof_case,
        "exit_status": 4,
        "diagnostic": "native pytest does not support runtime_plugin",
        "expected_diagnostic": "runtime_plugin",
        "fixture_sha256": "9" * 64,
        "test_body_ran": False,
    }
    evidence = rejection_record(stock, candidate, [evidence_row])
    result = accounting.verify_accounting(policy, stock, candidate, evidence)
    assert result["parity_cases"] == 1
    assert result["rejected_cases"] == 1
    assert result["full_parity"] is False
    assert result["parity_scope"] == "supported-cases-only"

    evidence["rejections"][0]["test_body_ran"] = True
    with pytest.raises(ValueError, match="unsupported test body ran"):
        accounting.verify_accounting(policy, stock, candidate, evidence)


@pytest.mark.parametrize(
    ("field", "expected_error"),
    [
        ("runner_artifact_set_sha256", "provenance differs for runner_artifact_set_sha256"),
        ("resolved_environment_sha256", "provenance differs for resolved_environment_sha256"),
        ("project_identity_sha256", "independent project root"),
    ],
)
def test_rejection_evidence_is_bound_to_candidate_provenance(
    field: str, expected_error: str
) -> None:
    accounting = load_accounting_module()
    policy = classification()
    set_disposition(policy, 0, "deliberately-unsupported")
    policy["classifications"][0]["rejection_proof"] = (
        "validation/pytest_runner/check_native_rejections.py#runtime-plugin"
    )
    stock = run_record()
    candidate = candidate_record(stock)
    candidate["cases"].pop(0)
    candidate["collected"] -= 1
    evidence = rejection_record(
        stock,
        candidate,
        [
            {
                "nodeid": NODEIDS[0],
                "proof": "validation/pytest_runner/check_native_rejections.py#runtime-plugin",
                "proof_case": "runtime-plugin",
                "exit_status": 4,
                "diagnostic": "native pytest does not support runtime_plugin",
                "expected_diagnostic": "runtime_plugin",
                "fixture_sha256": "9" * 64,
                "test_body_ran": False,
            }
        ],
    )
    if field == "project_identity_sha256":
        evidence["provenance"][field] = candidate["provenance"][field]
    else:
        evidence["provenance"][field] = "0" * 64

    with pytest.raises(ValueError, match=expected_error):
        accounting.verify_accounting(policy, stock, candidate, evidence)


def test_rejection_evidence_verifies_checker_source_hashes() -> None:
    accounting = load_accounting_module()
    policy = classification()
    set_disposition(policy, 0, "deliberately-unsupported")
    policy["classifications"][0]["rejection_proof"] = (
        "validation/pytest_runner/check_native_rejections.py#runtime-plugin"
    )
    stock = run_record()
    candidate = candidate_record(stock)
    candidate["cases"].pop(0)
    candidate["collected"] -= 1
    evidence = rejection_record(stock, candidate, [])
    evidence["provenance"]["checker_artifacts"][0]["sha256"] = "0" * 64

    with pytest.raises(ValueError, match="checker source hash differs"):
        accounting.verify_accounting(policy, stock, candidate, evidence)


def test_rejection_evidence_binds_shared_observation_source() -> None:
    accounting = load_accounting_module()
    policy = classification()
    set_disposition(policy, 0, "deliberately-unsupported")
    policy["classifications"][0]["rejection_proof"] = (
        "validation/pytest_runner/check_native_rejections.py#runtime-plugin"
    )
    stock = run_record()
    candidate = candidate_record(stock)
    candidate["cases"].pop(0)
    candidate["collected"] -= 1
    evidence = rejection_record(stock, candidate, [])
    observation_source = next(
        row
        for row in evidence["provenance"]["checker_artifacts"]
        if row["filename"] == "scripts/pytest_execution_observations.py"
    )
    observation_source["sha256"] = "0" * 64

    with pytest.raises(ValueError, match=r"checker source hash differs"):
        accounting.verify_accounting(policy, stock, candidate, evidence)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extra", "reordered"])
def test_rejection_records_require_exact_order_and_multiplicity(mutation: str) -> None:
    accounting = load_accounting_module()
    policy = classification()
    expected_rows = []
    for index, item in enumerate(policy["classifications"]):
        set_disposition(policy, index, "deliberately-unsupported")
        proof = "validation/pytest_runner/check_native_rejections.py#runtime-plugin"
        item["rejection_proof"] = proof
        expected_rows.append(
            {
                "nodeid": item["nodeid"],
                "proof": "validation/pytest_runner/check_native_rejections.py#runtime-plugin",
                "proof_case": "runtime-plugin",
                "exit_status": 4,
                "diagnostic": "native pytest async-test rejected this test before its body ran",
                "expected_diagnostic": "runtime-plugin",
                "fixture_sha256": "9" * 64,
                "test_body_ran": False,
            }
        )
    evidence_rows = deepcopy(expected_rows)
    if mutation == "missing":
        evidence_rows.pop()
    elif mutation == "duplicate":
        evidence_rows[1] = deepcopy(evidence_rows[0])
    elif mutation == "extra":
        evidence_rows.append(
            {
                "nodeid": "testing/test_extra.py::test_extra",
                "proof": "validation/pytest_runner/check_native_rejections.py#runtime-plugin",
                "proof_case": "runtime-plugin",
                "exit_status": 4,
                "diagnostic": "native pytest async-test rejected this test before its body ran",
                "expected_diagnostic": "runtime-plugin",
                "fixture_sha256": "9" * 64,
                "test_body_ran": False,
            }
        )
    else:
        evidence_rows.reverse()

    stock = run_record()
    candidate = candidate_record(stock)
    with pytest.raises(
        ValueError,
        match="rejections are missing, duplicated, extra, or out of order",
    ):
        accounting.verify_accounting(
            policy,
            stock,
            candidate,
            rejection_record(stock, candidate, evidence_rows),
        )


def test_completed_negative_baseline_with_exit_one_is_accounted() -> None:
    accounting = load_accounting_module()
    stock = run_record()
    candidate = candidate_record(stock)
    for run in (stock, candidate):
        report = run["cases"][0]["reports"][1]
        report.update(
            outcome="failed",
            exception="AssertionError: expected negative fixture",
            diagnostic="test_fixture.py:1: AssertionError: expected negative fixture",
        )
        run["exit_status"] = 1

    result = accounting.verify_accounting(classification(), stock, candidate)

    assert result["exit_status"] == 1
    assert result["ordered_semantics_equal"] is True


@pytest.mark.parametrize("status", [2, 3, 4, 5, 6])
def test_incomplete_pytest_exit_status_cannot_be_a_corpus_baseline(status: int) -> None:
    accounting = load_accounting_module()
    stock = run_record()
    candidate = candidate_record(stock)
    stock["exit_status"] = status

    with pytest.raises(ValueError, match="did not complete as a pytest test session"):
        accounting.verify_accounting(classification(), stock, candidate)
