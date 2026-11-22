"""Resolve the pinned pytest node inventory into an exhaustive compatibility policy."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, cast

DISPOSITIONS = {
    "parity-required",
    "reference/platform-skipped",
    "deliberately-unsupported",
}


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory_nodeids(inventory: dict[str, Any]) -> list[str]:
    try:
        nodeids = inventory["core_suite"]["collection"]["ordered_nodeids"]
    except (KeyError, TypeError) as error:
        raise ValueError("inventory has no ordered core collection") from error
    if not isinstance(nodeids, list) or not all(isinstance(nodeid, str) for nodeid in nodeids):
        raise ValueError("inventory ordered node IDs must be a list of strings")
    duplicates = sorted(nodeid for nodeid, count in Counter(nodeids).items() if count > 1)
    if duplicates:
        raise ValueError(f"inventory contains duplicate node IDs: {duplicates}")
    if not nodeids:
        raise ValueError("inventory contains no node IDs")
    return cast(list[str], nodeids)


def validate_rule(raw: object, index: int) -> dict[str, str | int]:
    if not isinstance(raw, dict):
        raise ValueError(f"classification rule {index} must be an object")
    required = {"id", "match", "disposition", "reason", "minimum_matches"}
    missing = sorted(required - raw.keys())
    unknown = sorted(raw.keys() - required - {"rejection_proof"})
    if missing or unknown:
        raise ValueError(f"classification rule {index} has missing={missing}, unknown={unknown}")
    rule_id = raw["id"]
    pattern = raw["match"]
    disposition = raw["disposition"]
    reason = raw["reason"]
    minimum_matches = raw["minimum_matches"]
    if not all(isinstance(value, str) and value for value in (rule_id, pattern, reason)):
        raise ValueError(f"classification rule {index} has an empty id, match, or reason")
    if disposition not in DISPOSITIONS:
        raise ValueError(f"classification rule {rule_id!r} has invalid disposition {disposition!r}")
    if (
        not isinstance(minimum_matches, int)
        or isinstance(minimum_matches, bool)
        or minimum_matches < 1
    ):
        raise ValueError(f"classification rule {rule_id!r} needs a positive minimum_matches")
    rejection_proof = raw.get("rejection_proof")
    if disposition == "deliberately-unsupported":
        if not isinstance(rejection_proof, str) or not rejection_proof:
            raise ValueError(
                f"classification rule {rule_id!r} needs a rejection_proof for unsupported cases"
            )
    elif rejection_proof is not None:
        raise ValueError(
            f"classification rule {rule_id!r} has a rejection_proof without unsupported cases"
        )
    return {key: value for key, value in raw.items() if isinstance(value, str | int)}


def resolve_classifications(
    nodeids: list[str],
    policy: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    raw_rules = policy.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("classification policy must contain at least one rule")
    rules = [validate_rule(raw, index) for index, raw in enumerate(raw_rules)]
    ids = [str(rule["id"]) for rule in rules]
    duplicate_ids = sorted(rule_id for rule_id, count in Counter(ids).items() if count > 1)
    if duplicate_ids:
        raise ValueError(f"classification policy contains duplicate rule IDs: {duplicate_ids}")

    match_counts = Counter[str]()
    resolved: list[dict[str, str]] = []
    unclassified: list[str] = []
    for nodeid in nodeids:
        matching = [rule for rule in rules if fnmatch.fnmatchcase(nodeid, str(rule["match"]))]
        if not matching:
            unclassified.append(nodeid)
            continue
        rule = matching[0]
        rule_id = str(rule["id"])
        match_counts[rule_id] += 1
        record = {
            "nodeid": nodeid,
            "disposition": str(rule["disposition"]),
            "reason": str(rule["reason"]),
            "rule": rule_id,
        }
        if "rejection_proof" in rule:
            record["rejection_proof"] = str(rule["rejection_proof"])
        resolved.append(record)
    if unclassified:
        preview = unclassified[:10]
        raise ValueError(f"classification policy leaves node IDs unclassified: {preview}")

    rule_results: list[dict[str, object]] = []
    for rule in rules:
        rule_id = str(rule["id"])
        actual = match_counts[rule_id]
        minimum = int(rule["minimum_matches"])
        if actual < minimum:
            detail = f"matched {actual} node IDs, expected at least {minimum}"
            raise ValueError(f"classification rule {rule_id!r} {detail}")
        rule_results.append({"id": rule_id, "matched": actual})
    return resolved, rule_results


def build_output(inventory_path: Path, policy_path: Path) -> dict[str, object]:
    inventory = load_object(inventory_path)
    policy = load_object(policy_path)
    nodeids = inventory_nodeids(inventory)
    classifications, rules = resolve_classifications(nodeids, policy)
    counts = Counter(record["disposition"] for record in classifications)
    return {
        "schema_version": 1,
        "inventory": {
            "path": inventory_path.name,
            "sha256": file_sha256(inventory_path),
            "upstream_commit": inventory.get("upstream", {}).get("commit"),
            "node_count": len(nodeids),
        },
        "policy": {"path": policy_path.name, "sha256": file_sha256(policy_path)},
        "counts": {disposition: counts[disposition] for disposition in sorted(DISPOSITIONS)},
        "rule_matches": rules,
        "classifications": classifications,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    output = build_output(args.inventory, args.policy)
    rendered = json.dumps(output, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
