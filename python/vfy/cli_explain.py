"""Implementations of the explain and config commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from .errors import VfyError
from .native import (
    plan_test_selection,
)
from .run.plan import SelectionPlan, _selection_message
from .store_loader import _load_store

if TYPE_CHECKING:
    from .config_types import Config


class _ProtectionEvidence(TypedDict):
    condition: int
    left_context: str
    left_vector: str
    right_context: str
    right_vector: str


def _explain(args: argparse.Namespace, _config: Config) -> int:
    """Show tests that reach a location and masking MC/DC observation pairs."""
    store = _load_store(args.data_file)
    location = args.location
    plan = SelectionPlan.from_rust(
        plan_test_selection(
            store.to_rust(),
            str(Path(args.history).resolve()),
            [],
            "",
            "balanced",
            location,
        )
    )
    durations = plan.durations
    evidence: list[_ProtectionEvidence] = []
    path_text, line_text = location.rsplit(":", 1)
    path = Path(path_text).resolve()
    line = int(line_text)
    decision_data = store.decision_data()
    definitions = [
        definition
        for definition in decision_data.definitions
        if Path(definition.path).resolve() == path
        and definition.line <= line <= definition.end_line
    ]
    if definitions:
        records = decision_data.evidence_records(
            decisions=[definition.key for definition in definitions]
        )
        evidence = [
            {
                "condition": record.condition + 1,
                "left_context": record.left_context,
                "left_vector": record.left_vector,
                "right_context": record.right_context,
                "right_vector": record.right_vector,
            }
            for record in records
        ]
    result = {
        "location": location,
        "tests": [
            {"nodeid": nodeid, "duration_ms": durations.get(nodeid)} for nodeid in plan.all_tests
        ],
        "selection": {
            "reasons": list(plan.reasons),
            "affected_files": list(plan.affected_files),
            "affected_functions": plan.affected_functions,
            "affected_branches": plan.affected_branches,
        },
        "semantic_evidence": evidence,
    }
    if args.json:
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(_selection_message(plan))
        for nodeid in plan.all_tests:
            duration = durations.get(nodeid)
            suffix = "" if duration is None else f"  {duration / 1000:.3f}s"
            print(f"  {nodeid}{suffix}")
        if definitions:
            print(f"MC/DC observation pairs: {len(evidence)} pair(s)")
            for record in evidence:
                print(
                    f"  condition {record['condition']}: "
                    f"{record['left_context']} ({record['left_vector']}) -> "
                    f"{record['right_context']} ({record['right_vector']})"
                )
    # Direct API collection can retain MC/DC witnesses without test contexts.
    # Missing reachability data must not hide those witnesses.
    if not result["tests"] and not evidence:
        raise VfyError(f"no historical test reaches {location}")
    return 0


def _config_command(args: argparse.Namespace, config: Config) -> int:
    source = args.config_source
    if args.config_command == "validate":
        if args.config_discovery == "isolated":
            print("Configuration files are ignored; defaults are valid.")
        elif source is None:
            print("No configuration found; defaults are valid.")
        else:
            print(f"Configuration is valid: {source}")
        return 0
    payload = {
        "source": str(source) if source is not None else None,
        "settings": config,
        "provenance": {
            "configuration": str(source) if source is not None else "defaults",
            "discovery": args.config_discovery,
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0
