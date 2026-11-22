"""Protect exhaustive pytest corpus classification."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
from scripts.pytest_corpus_inventory import PINNED_COMMIT

ROOT = Path(__file__).parents[2]


def load_classification_module() -> ModuleType:
    path = ROOT / "scripts" / "pytest_corpus_classification.py"
    spec = importlib.util.spec_from_file_location("pytest_corpus_classification", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_accounting_module() -> ModuleType:
    path = ROOT / "scripts" / "pytest_execution_accounting.py"
    spec = importlib.util.spec_from_file_location("pytest_execution_accounting", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rule(
    rule_id: str,
    match: str,
    disposition: str = "parity-required",
    **extra: object,
) -> dict[str, object]:
    return {
        "id": rule_id,
        "match": match,
        "disposition": disposition,
        "reason": f"reason for {rule_id}",
        "minimum_matches": 1,
        **extra,
    }


def test_resolver_preserves_inventory_order_and_uses_first_matching_rule() -> None:
    classification = load_classification_module()
    nodeids = ["testing/test_debugging.py::test_pdb", "testing/test_runner.py::test_call"]
    policy = {
        "rules": [
            rule(
                "debugger",
                "testing/test_debugging.py::*",
                "deliberately-unsupported",
                rejection_proof="tests/integration/test_native_rejections.py::test_debugger",
            ),
            rule("required", "*"),
        ]
    }

    resolved, matches = classification.resolve_classifications(nodeids, policy)

    assert [record["nodeid"] for record in resolved] == nodeids
    assert [record["disposition"] for record in resolved] == [
        "deliberately-unsupported",
        "parity-required",
    ]
    assert matches == [{"id": "debugger", "matched": 1}, {"id": "required", "matched": 1}]


def test_resolver_rejects_unclassified_nodes() -> None:
    classification = load_classification_module()

    with pytest.raises(ValueError, match="leaves node IDs unclassified"):
        classification.resolve_classifications(
            ["testing/test_runner.py::test_call"],
            {"rules": [rule("fixtures-only", "testing/test_fixture.py::*")]},
        )


def test_resolver_rejects_stale_and_unproved_rules() -> None:
    classification = load_classification_module()

    with pytest.raises(ValueError, match="matched 0 node IDs"):
        classification.resolve_classifications(
            ["testing/test_runner.py::test_call"],
            {"rules": [rule("stale", "testing/test_missing.py::*"), rule("required", "*")]},
        )

    with pytest.raises(ValueError, match="needs a rejection_proof"):
        classification.resolve_classifications(
            ["testing/test_debugging.py::test_pdb"],
            {"rules": [rule("debugger", "*", "deliberately-unsupported")]},
        )


def test_generated_classification_satisfies_accounting_consumer(tmp_path: Path) -> None:
    classification = load_classification_module()
    accounting = load_accounting_module()
    nodeids = [
        "testing/test_helpconfig.py::test_version_verbose",
        "testing/test_terminal.py::TestTerminalFunctional::test_verbose_reporting_xdist",
        "testing/test_runner.py::test_call",
    ]
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "upstream": {"commit": PINNED_COMMIT},
                "core_suite": {"collection": {"ordered_nodeids": nodeids}},
            }
        ),
        encoding="utf-8",
    )
    policy = json.loads((ROOT / "docs/about/pytest-test-policy.json").read_text(encoding="utf-8"))
    policy["rules"][-1]["minimum_matches"] = 1
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")

    generated = classification.build_output(inventory, policy_path)
    records = accounting.classified_nodes(generated)

    assert [record["nodeid"] for record in records] == nodeids
    assert [record["disposition"] for record in records] == [
        "deliberately-unsupported",
        "reference/platform-skipped",
        "parity-required",
    ]
