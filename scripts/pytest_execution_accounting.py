"""Verify exhaustive ordered execution records for the pinned pytest corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Literal

REQUIRED_REPORT_FIELDS = {
    "phase",
    "outcome",
    "exception",
    "diagnostic",
    "stdout",
    "stderr",
    "log",
}
REQUIRED_WARNING_FIELDS = {"message", "filename", "lineno"}
OUTCOMES = {"passed", "failed", "error", "skipped", "xfailed", "xpassed"}
PINNED_PYTEST_VERSION = "9.1.1"
PINNED_CORE_NODE_COUNT = 4231
CLASSIFICATION_DISPOSITIONS = {
    "parity-required",
    "reference/platform-skipped",
    "deliberately-unsupported",
}
COMPLETED_PYTEST_EXIT_STATUSES = {0, 1}
REJECTION_PROOF_PREFIX = "validation/pytest_runner/check_native_rejections.py#"
REJECTION_CHECKER_FILES = (
    "scripts/process_tree.py",
    "scripts/pytest_execution_accounting.py",
    "scripts/pytest_execution_record.py",
    "scripts/pytest_execution_observations.py",
    "validation/pytest_runner/check_native_rejections.py",
)
THREE_RUN_HARNESS_FILES = (
    "process_tree.py",
    "pytest_corpus_conftest.py",
    "pytest_execution_probe.py",
    "pytest_execution_record.py",
    "pytest_execution_observations.py",
)
PLUGIN_REJECTION_CASES = {
    "xdist-looponfail-plugin": "xdist.looponfail",
    "hypothesis-plugin": "hypothesispytest",
}
PLUGIN_REJECTION_ARGUMENTS = {
    "xdist-looponfail-plugin": ["-p", "xdist.looponfail"],
    "hypothesis-plugin": ["-p", "hypothesispytest"],
}
PLUGIN_DEPENDENT_UNSUPPORTED = {
    "testing/test_helpconfig.py::test_version_verbose": "hypothesis-plugin",
}
XDIST_OUTER_PLUGIN_NODEID = (
    "testing/test_terminal.py::TestTerminalFunctional::test_verbose_reporting_xdist"
)
HYPOTHESIS_LATE_AUTOLOAD_NODEIDS = (
    "testing/test_junitxml.py::test_random_report_log_xdist",
    "testing/test_junitxml.py::test_runs_twice_xdist",
    "testing/test_terminal.py::TestTerminalFunctional::test_header_trailer_info",
    "testing/test_terminal.py::TestTerminalFunctional::test_no_header_trailer_info",
    "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_normal",
    "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_normal_count",
    "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_verbose",
    "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_times",
    "testing/test_terminal.py::TestProgressWithTeardown::test_xdist_normal",
)
HYPOTHESIS_OUTER_FAILURE_SIGNATURES = {
    "testing/test_junitxml.py::test_random_report_log_xdist": "FileNotFoundError: junit.xml",
    "testing/test_junitxml.py::test_runs_twice_xdist": "FileNotFoundError: junit.xml",
    "testing/test_terminal.py::TestTerminalFunctional::test_header_trailer_info": (
        "Failed: remains unmatched: '*===== test session starts ====*'"
    ),
    "testing/test_terminal.py::TestTerminalFunctional::test_no_header_trailer_info": None,
    "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_normal": (
        "Failed: remains unmatched: '\\\\.{20} \\\\s+ \\\\[100%\\\\]'"
    ),
    "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_normal_count": (
        "Failed: remains unmatched: '\\\\.{20} \\\\s+ \\\\[20/20\\\\]'"
    ),
    "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_verbose": (
        "Failed: line '\\\\[gw\\\\d\\\\] \\\\[\\\\s*\\\\d+%\\\\] PASSED "
        "test_bar.py::test_bar\\\\[1\\\\]' not found in output"
    ),
    "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_times": (
        "Failed: line '\\\\[gw\\\\d\\\\] \\\\d{1,3}[\\\\.[a-z\\\\ "
        "]{1,2}\\\\d{0,3}\\\\w{1,2} PASSED "
        "test_bar.py::test_bar\\\\[1\\\\]' not found in output"
    ),
    "testing/test_terminal.py::TestProgressWithTeardown::test_xdist_normal": (
        "Failed: remains unmatched: '[\\\\.E]{40} \\\\s+ \\\\[100%\\\\]'"
    ),
}
HYPOTHESIS_LIFECYCLE_SETUP_NODEID = (
    "testing/python/metafunc.py::TestMetafunc::test_idval_hypothesis"
)
HYPOTHESIS_LIFECYCLE_SOURCE_PATHS = [
    "testing/python/metafunc.py",
    "testing/test_junitxml.py",
    "testing/test_terminal.py",
]
HYPOTHESIS_LIFECYCLE_PROOF_FIELDS = {
    "proof_case",
    "upstream_commit",
    "pytest_arguments",
    "source_files",
    "outer_exit_status",
    "outer_test_outcomes",
    "outer_failure_signature",
    "nested_warning_type",
    "nested_warning_text",
}
PLUGIN_PROOF_FIELDS = {
    "proof_case",
    "plugin_entrypoint",
    "arguments",
    "stock_exit_status",
    "stock_collection_started",
    "stock_test_body_ran",
    "stock_loaded_identity",
    "candidate_exit_status",
    "diagnostic",
    "expected_diagnostic",
    "fixture_sha256",
    "test_body_ran",
    "candidate_collection_started",
    "candidate_cache_unchanged",
    "candidate_cache_sha256_before",
    "candidate_cache_sha256_after",
    "stock_project_identity_sha256",
    "candidate_project_identity_sha256",
}


def normalized_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_classification_header(classification: dict[str, Any]) -> dict[str, Any]:
    expected_fields = {
        "schema_version",
        "inventory",
        "policy",
        "counts",
        "rule_matches",
        "classifications",
    }
    if set(classification) != expected_fields:
        raise ValueError(
            "classification has an invalid schema: "
            f"missing={sorted(expected_fields - classification.keys())}, "
            f"unknown={sorted(classification.keys() - expected_fields)}"
        )
    if classification["schema_version"] != 1 or isinstance(classification["schema_version"], bool):
        raise ValueError("classification has an unsupported schema version")

    inventory = classification["inventory"]
    if (
        not isinstance(inventory, dict)
        or set(inventory) != {"path", "sha256", "upstream_commit", "node_count"}
        or not isinstance(inventory["path"], str)
        or not inventory["path"]
        or not is_sha256(inventory["sha256"])
        or not isinstance(inventory["upstream_commit"], str)
        or re.fullmatch(r"[0-9a-f]{40}", inventory["upstream_commit"]) is None
        or not isinstance(inventory["node_count"], int)
        or isinstance(inventory["node_count"], bool)
        or inventory["node_count"] < 1
    ):
        raise ValueError("classification inventory provenance is invalid")
    policy = classification["policy"]
    if (
        not isinstance(policy, dict)
        or set(policy) != {"path", "sha256"}
        or not isinstance(policy["path"], str)
        or not policy["path"]
        or not is_sha256(policy["sha256"])
    ):
        raise ValueError("classification policy provenance is invalid")
    return inventory


def validate_rule_matches(raw_rules: object) -> dict[str, int]:
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("classification has no rule match records")
    rule_matches: dict[str, int] = {}
    for index, rule in enumerate(raw_rules):
        if (
            not isinstance(rule, dict)
            or set(rule) != {"id", "matched"}
            or not isinstance(rule["id"], str)
            or not rule["id"]
            or not isinstance(rule["matched"], int)
            or isinstance(rule["matched"], bool)
            or rule["matched"] < 1
        ):
            raise ValueError(f"classification rule match {index} is invalid")
        rule_id = rule["id"]
        if rule_id in rule_matches:
            raise ValueError(f"classification has duplicate rule match {rule_id!r}")
        rule_matches[rule_id] = rule["matched"]
    return rule_matches


def validate_classification_record(
    record: object, index: int, rule_matches: dict[str, int]
) -> tuple[dict[str, str], str]:
    if not isinstance(record, dict):
        raise ValueError(f"classification record {index} must be an object")
    required_fields = {"nodeid", "disposition", "reason", "rule"}
    missing = required_fields - record.keys()
    unknown = record.keys() - required_fields - {"rejection_proof"}
    if missing or unknown:
        raise ValueError(
            f"classification record {index} has missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    nodeid = record["nodeid"]
    disposition = record["disposition"]
    if not isinstance(nodeid, str) or not nodeid:
        raise ValueError(f"classification record {index} has an invalid node ID")
    if not isinstance(disposition, str) or disposition not in CLASSIFICATION_DISPOSITIONS:
        raise ValueError(f"classification record {index} has invalid disposition {disposition!r}")
    reason = record["reason"]
    rule_id = record["rule"]
    if not isinstance(reason, str) or not reason:
        raise ValueError(f"classification record {index} has an invalid reason")
    if not isinstance(rule_id, str) or rule_id not in rule_matches:
        raise ValueError(f"classification record {index} has an unknown rule")
    resolved = {"nodeid": nodeid, "disposition": disposition}
    if disposition == "deliberately-unsupported":
        rejection_proof = record.get("rejection_proof")
        if not isinstance(rejection_proof, str) or not rejection_proof:
            raise ValueError(
                f"classification record {index} needs a rejection proof for unsupported cases"
            )
        resolved["rejection_proof"] = rejection_proof
    elif "rejection_proof" in record:
        raise ValueError(
            f"classification record {index} has a rejection proof without unsupported cases"
        )
    return resolved, rule_id


def validate_classification_counts(
    classification: dict[str, Any],
    records: list[dict[str, str]],
    disposition_counts: Counter[str],
    rule_counts: Counter[str],
    rule_matches: dict[str, int],
    inventory: dict[str, Any],
) -> None:
    counts = classification["counts"]
    if not isinstance(counts, dict) or set(counts) != CLASSIFICATION_DISPOSITIONS:
        raise ValueError("classification disposition counts are invalid")
    for disposition in CLASSIFICATION_DISPOSITIONS:
        count = counts[disposition]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"classification count for {disposition!r} is invalid")
        if count != disposition_counts[disposition]:
            raise ValueError(
                f"classification count for {disposition!r} does not match its node records"
            )
    if rule_counts != rule_matches:
        raise ValueError("classification rule match counts do not match node records")
    if inventory["node_count"] != len(records):
        raise ValueError("classification inventory count does not match node records")


def classified_nodes(classification: dict[str, Any]) -> list[dict[str, str]]:
    inventory = validate_classification_header(classification)
    rule_matches = validate_rule_matches(classification["rule_matches"])
    raw = classification["classifications"]
    if not isinstance(raw, list) or not raw:
        raise ValueError("classification has no node records")
    records: list[dict[str, str]] = []
    disposition_counts = Counter[str]()
    rule_counts = Counter[str]()
    for index, record in enumerate(raw):
        resolved, rule_id = validate_classification_record(record, index, rule_matches)
        records.append(resolved)
        disposition_counts[resolved["disposition"]] += 1
        rule_counts[rule_id] += 1
    validate_classification_counts(
        classification, records, disposition_counts, rule_counts, rule_matches, inventory
    )

    duplicates = sorted(
        nodeid
        for nodeid, count in Counter(record["nodeid"] for record in records).items()
        if count > 1
    )
    if duplicates:
        raise ValueError(f"classification contains duplicate node IDs: {duplicates}")
    return records


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def pytest_plugin_key(plugin: dict[str, Any]) -> tuple[str, str, str]:
    return (str(plugin["name"]), str(plugin["value"]), str(plugin["distribution"]))


def validate_resolved_environment(provenance: dict[str, Any], name: str) -> list[tuple[str, str]]:
    resolved = provenance.get("resolved_environment")
    if (
        not isinstance(resolved, dict)
        or set(resolved) != {"schema_version", "python", "platform", "packages", "pytest_plugins"}
        or resolved["schema_version"] != 2
        or not isinstance(resolved["python"], str)
        or not resolved["python"]
        or not isinstance(resolved["platform"], str)
        or not resolved["platform"]
        or not isinstance(resolved["packages"], list)
        or not isinstance(resolved["pytest_plugins"], list)
    ):
        raise ValueError(f"{name} resolved environment is invalid")
    if resolved["python"] != provenance["python"] or resolved["platform"] != provenance["platform"]:
        raise ValueError(f"{name} resolved environment contradicts interpreter provenance")
    package_names = []
    packages: list[tuple[str, str]] = []
    for index, package in enumerate(resolved["packages"]):
        if (
            not isinstance(package, dict)
            or set(package) != {"name", "version"}
            or not all(isinstance(package[field], str) and package[field] for field in package)
        ):
            raise ValueError(f"{name} resolved environment package {index} is invalid")
        package_name = package["name"]
        package_version = package["version"]
        assert isinstance(package_name, str)
        assert isinstance(package_version, str)
        if package_name != normalized_distribution(package_name):
            raise ValueError(f"{name} resolved environment package {index} is not normalized")
        package_names.append(package_name)
        packages.append((package_name, package_version))
    if package_names != sorted(package_names) or len(package_names) != len(set(package_names)):
        raise ValueError(f"{name} resolved environment packages are not unique and ordered")
    plugins = []
    for index, plugin in enumerate(resolved["pytest_plugins"]):
        if (
            not isinstance(plugin, dict)
            or set(plugin) != {"name", "value", "distribution", "version"}
            or not all(isinstance(plugin[field], str) and plugin[field] for field in plugin)
        ):
            raise ValueError(f"{name} resolved environment pytest plug-in {index} is invalid")
        if plugin["distribution"] != normalized_distribution(plugin["distribution"]):
            raise ValueError(
                f"{name} resolved environment pytest plug-in {index} is not normalized"
            )
        plugins.append(plugin)
    if plugins != sorted(plugins, key=pytest_plugin_key):
        raise ValueError(f"{name} resolved environment pytest plug-ins are not ordered")
    canonical = json.dumps(resolved, separators=(",", ":"), sort_keys=True).encode()
    if provenance["resolved_environment_sha256"] != hashlib.sha256(canonical).hexdigest():
        raise ValueError(f"{name} resolved environment hash is invalid")
    return packages


def validate_artifact_rows(provenance: dict[str, Any], name: str, field: str) -> None:
    records = provenance.get(field)
    if not isinstance(records, list) or not records:
        raise ValueError(f"{name} run has no {field}")
    if field == "dependency_locks" and len(records) != 1:
        raise ValueError(f"{name} run must have exactly one dependency lock")
    for index, record in enumerate(records):
        if (
            not isinstance(record, dict)
            or set(record) != {"filename", "sha256"}
            or not isinstance(record["filename"], str)
            or not record["filename"]
            or not is_sha256(record["sha256"])
        ):
            raise ValueError(f"{name} {field} record {index} is invalid")
    ordered = sorted(records, key=lambda record: (record["filename"], record["sha256"]))
    if records != ordered or len({record["filename"] for record in records}) != len(records):
        raise ValueError(f"{name} {field} records are not unique and ordered")


def validate_installed_artifacts(provenance: dict[str, Any], name: str) -> dict[str, str]:
    installed = provenance.get("installed_runner_artifacts")
    if not isinstance(installed, list) or not installed:
        raise ValueError(f"{name} run has no installed runner artifact provenance")
    if installed != sorted(installed, key=installed_artifact_key):
        raise ValueError(f"{name} installed runner artifacts are not deterministically ordered")
    expected = {(record["filename"], record["sha256"]) for record in provenance["runner_artifacts"]}
    artifacts = set()
    versions: dict[str, str] = {}
    for index, record in enumerate(installed):
        if (
            not isinstance(record, dict)
            or set(record) != {"distribution", "version", "filename", "sha256"}
            or not all(isinstance(record[field], str) and record[field] for field in record)
            or not is_sha256(record["sha256"])
        ):
            raise ValueError(f"{name} installed runner artifact {index} is invalid")
        distribution_value = record["distribution"]
        version = record["version"]
        assert isinstance(distribution_value, str)
        assert isinstance(version, str)
        distribution = normalized_distribution(distribution_value)
        if distribution in versions:
            raise ValueError(f"{name} installed runner artifacts contain duplicate distributions")
        artifacts.add((record["filename"], record["sha256"]))
        versions[distribution] = version
    if artifacts != expected:
        raise ValueError(f"{name} installed runner artifacts do not match retained artifacts")
    return versions


def installed_artifact_key(record: object) -> str:
    if not isinstance(record, dict):
        return ""
    return normalized_distribution(str(record.get("distribution", "")))


def validate_runner_binding(
    provenance: dict[str, Any], name: str, installed_versions: dict[str, str]
) -> None:
    runner = normalized_distribution(provenance["runner_distribution"])
    if runner not in installed_versions:
        raise ValueError(f"{name} runner distribution is absent from installed artifacts")
    if installed_versions[runner] != provenance["runner_version"]:
        raise ValueError(f"{name} runner version contradicts installed artifact provenance")
    if runner == "pytest":
        expected_artifact_sets = ({"pytest"}, {"pytest", "vfy"})
        expected_pytest_version = PINNED_PYTEST_VERSION
    elif runner == "vfy":
        expected_artifact_sets = ({"pytest", "vfy"},)
        expected_pytest_version = PINNED_PYTEST_VERSION
    else:
        raise ValueError(f"{name} runner distribution is not a supported corpus runner")
    if installed_versions.get("pytest") != expected_pytest_version:
        raise ValueError(f"{name} run lacks the pinned pytest artifact")
    if set(installed_versions) not in expected_artifact_sets:
        raise ValueError(f"{name} run has unexpected runner artifact distributions")


def validate_loaded_plugins(
    provenance: dict[str, Any], name: str, packages: dict[str, str]
) -> None:
    plugins = provenance.get("loaded_pytest_plugins")
    if not isinstance(plugins, list):
        raise ValueError(f"{name} run has no ordered loaded pytest plug-ins")
    for index, plugin in enumerate(plugins):
        if (
            not isinstance(plugin, dict)
            or set(plugin) != {"name", "distribution", "version"}
            or not all(isinstance(plugin[field], str) and plugin[field] for field in plugin)
        ):
            raise ValueError(f"{name} loaded pytest plug-in {index} is invalid")
        distribution = plugin["distribution"]
        if distribution != normalized_distribution(distribution):
            raise ValueError(f"{name} loaded pytest plug-in {index} is not normalized")
        if packages.get(distribution) != plugin["version"]:
            raise ValueError(
                f"{name} loaded pytest plug-in {index} contradicts resolved environment"
            )


def validate_native_origin_provenance(provenance: dict[str, Any], name: str) -> None:
    imports = provenance.get("native_imports", [])
    if provenance["runner_distribution"] == "vfy":
        required_modules = {"vfy._native", "vfy._pytest.native_pytest"}
        if not isinstance(imports, list):
            raise ValueError(f"{name} native import origins are incomplete")
        seen: list[str] = []
        for row in imports:
            if (
                not isinstance(row, dict)
                or set(row) != {"module", "relative_path", "sha256"}
                or not isinstance(row["module"], str)
                or row["module"] in seen
                or not isinstance(row["relative_path"], str)
                or not is_sha256(row["sha256"])
            ):
                raise ValueError(f"{name} native import origins are invalid")
            seen.append(row["module"])
            relative = row["relative_path"]
            module = row["module"]
            module_path = module.replace(".", "/")
            if (module != "vfy" and not module.startswith("vfy.")) or not all(
                part.isidentifier() for part in module.split(".")
            ):
                raise ValueError(f"{name} native import origin is outside its wheel module")
            if module == "vfy._native":
                matches_module = (
                    relative.startswith("vfy/_native.")
                    and "/" not in relative.removeprefix("vfy/")
                    and relative.endswith((".so", ".pyd"))
                )
            elif module == "vfy._pytest.native_pytest":
                matches_module = relative == "vfy/_pytest/native_pytest.py"
            else:
                matches_module = relative in {f"{module_path}.py", f"{module_path}/__init__.py"}
            if not matches_module:
                raise ValueError(f"{name} native import origin is outside its wheel module")
        if not required_modules <= set(seen):
            raise ValueError(f"{name} native import origins are incomplete")
        if seen != sorted(seen):
            raise ValueError(f"{name} native import origins are invalid")
    elif imports:
        raise ValueError(f"{name} stock execution unexpectedly claims native import origins")


def validate_record_schema(run: dict[str, Any], name: str) -> None:
    if (
        type(run.get("schema_version")) is not int
        or run.get("schema_version") != 2
        or run.get("comparison") != "outer-session-semantics"
    ):
        raise ValueError(f"{name} execution record must use schema 2 outer-session semantics")


def validate_provenance(run: dict[str, Any], name: str) -> dict[str, Any]:
    validate_record_schema(run, name)
    provenance = run.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError(f"{name} run has no provenance object")
    required = {
        "upstream_commit",
        "inventory_sha256",
        "policy_sha256",
        "project_identity_sha256",
        "python",
        "platform",
        "runner_distribution",
        "runner_version",
        "runner_artifact_set_sha256",
        "resolved_environment_sha256",
    }
    missing = sorted(
        key for key in required if not isinstance(provenance.get(key), str) or not provenance[key]
    )
    if missing:
        raise ValueError(f"{name} run has incomplete provenance: {missing}")
    if not is_sha256(provenance["project_identity_sha256"]):
        raise ValueError(f"{name} project identity hash is invalid")
    if provenance.get("plugin_autoload") not in {"enabled", "disabled"}:
        raise ValueError(f"{name} run has an invalid pytest plug-in autoload mode")
    pytest_arguments = provenance.get("pytest_arguments")
    if not isinstance(pytest_arguments, list) or any(
        not isinstance(argument, str) for argument in pytest_arguments
    ):
        raise ValueError(f"{name} run has invalid pytest arguments")
    explicit_deselections = provenance.get("explicit_deselections")
    expected_deselections = [
        argument.removeprefix("--deselect=")
        for argument in pytest_arguments
        if argument.startswith("--deselect=")
    ]
    if explicit_deselections != expected_deselections:
        raise ValueError(f"{name} explicit deselection provenance contradicts pytest arguments")
    for field in ("runner_artifacts", "dependency_locks", "harness_artifacts"):
        validate_artifact_rows(provenance, name, field)
    validate_native_origin_provenance(provenance, name)
    pytester_control = provenance.get("pytester_control")
    if pytester_control is not None:
        if (
            not isinstance(pytester_control, dict)
            or set(pytester_control)
            != {
                "mode",
                "config_template_sha256",
                "ancestor_config_applied",
                "subprocess_nodeids",
                "inventory_nodeids_sha256",
                "inventory_nodeid_count",
            }
            or pytester_control["mode"] not in {"default", "inprocess-with-exceptions"}
            or not is_sha256(pytester_control["config_template_sha256"])
            or not is_sha256(pytester_control["inventory_nodeids_sha256"])
            or not isinstance(pytester_control["inventory_nodeid_count"], int)
            or isinstance(pytester_control["inventory_nodeid_count"], bool)
            or pytester_control["inventory_nodeid_count"] < 0
            or not isinstance(pytester_control["ancestor_config_applied"], bool)
            or not isinstance(pytester_control["subprocess_nodeids"], list)
            or any(not isinstance(nodeid, str) for nodeid in pytester_control["subprocess_nodeids"])
        ):
            raise ValueError(f"{name} pytester control provenance is invalid")
        if pytester_control["ancestor_config_applied"] != (
            pytester_control["mode"] == "inprocess-with-exceptions"
        ):
            raise ValueError(f"{name} pytester control mode and config flag disagree")
        template_rows = [
            row
            for row in provenance["harness_artifacts"]
            if row["filename"] == "pytest_corpus_conftest.py"
        ]
        if (
            len(template_rows) != 1
            or template_rows[0]["sha256"] != pytester_control["config_template_sha256"]
        ):
            raise ValueError(f"{name} pytester config template is not bound to its harness")
    installed_versions = validate_installed_artifacts(provenance, name)
    validate_runner_binding(provenance, name, installed_versions)
    canonical_artifacts = json.dumps(
        provenance["runner_artifacts"], separators=(",", ":"), sort_keys=True
    ).encode()
    artifact_hash = hashlib.sha256(canonical_artifacts).hexdigest()
    if provenance["runner_artifact_set_sha256"] != artifact_hash:
        raise ValueError(f"{name} runner artifact-set hash is invalid")
    packages = dict(validate_resolved_environment(provenance, name))
    for distribution, version in installed_versions.items():
        if packages.get(distribution) != version:
            raise ValueError(
                f"{name} installed {distribution} artifact is absent from resolved environment"
            )
    validate_loaded_plugins(provenance, name, packages)
    return provenance


def comparable_environment(
    provenance: dict[str, Any], excluded: set[str]
) -> dict[str, list[dict[str, str]]]:
    resolved = provenance["resolved_environment"]
    return {
        "packages": [
            package for package in resolved["packages"] if package["name"] not in excluded
        ],
        "pytest_plugins": [
            plugin
            for plugin in resolved["pytest_plugins"]
            if plugin["distribution"] not in excluded
        ],
        "loaded_pytest_plugins": [
            plugin
            for plugin in provenance["loaded_pytest_plugins"]
            if plugin["distribution"] not in excluded
        ],
    }


def validate_run_pair_provenance(
    classification: dict[str, Any], stock: dict[str, Any], candidate: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    stock_provenance = validate_provenance(stock, "stock")
    candidate_provenance = validate_provenance(candidate, "candidate")
    if (
        stock_provenance["project_identity_sha256"]
        == candidate_provenance["project_identity_sha256"]
    ):
        raise ValueError("stock and candidate runs must use independent project roots")
    expected_commit = classification.get("inventory", {}).get("upstream_commit")
    if stock_provenance["upstream_commit"] != expected_commit:
        raise ValueError("run provenance does not match the classified upstream commit")
    for field in (
        "upstream_commit",
        "inventory_sha256",
        "policy_sha256",
        "python",
        "platform",
        "plugin_autoload",
        "pytest_arguments",
        "explicit_deselections",
    ):
        if stock_provenance[field] != candidate_provenance[field]:
            raise ValueError(
                f"stock and candidate provenance differ for {field}: "
                f"{stock_provenance[field]!r} != {candidate_provenance[field]!r}"
            )
    if stock_provenance["harness_artifacts"] != candidate_provenance["harness_artifacts"]:
        raise ValueError("stock and candidate execution harness sources differ")
    excluded = {
        normalized_distribution(record["distribution"])
        for provenance in (stock_provenance, candidate_provenance)
        for record in provenance["installed_runner_artifacts"]
    }
    stock_environment = comparable_environment(stock_provenance, excluded)
    candidate_environment = comparable_environment(candidate_provenance, excluded)
    if stock_environment != candidate_environment:
        raise ValueError(
            "stock and candidate resolved dependency environments differ: "
            f"stock={stock_environment}, candidate={candidate_environment}"
        )
    return stock_provenance, candidate_provenance


def validate_report(report: object, name: str, nodeid: str, index: int) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ValueError(f"{name} report {index} for {nodeid} must be an object")
    missing = sorted(REQUIRED_REPORT_FIELDS - report.keys())
    unknown = sorted(report.keys() - REQUIRED_REPORT_FIELDS)
    if missing or unknown:
        raise ValueError(
            f"{name} report {index} for {nodeid} has missing={missing}, unknown={unknown}"
        )
    if report["phase"] not in {"setup", "call", "teardown"}:
        raise ValueError(f"{name} report {index} for {nodeid} has an invalid phase")
    if report["outcome"] not in OUTCOMES:
        raise ValueError(f"{name} report {index} for {nodeid} has an invalid outcome")
    for field in ("exception", "diagnostic"):
        if report[field] is not None and not isinstance(report[field], str):
            raise ValueError(f"{name} report {index} for {nodeid} has an invalid {field}")
    if not all(isinstance(report[field], str) for field in ("stdout", "stderr", "log")):
        raise ValueError(f"{name} report {index} for {nodeid} has invalid captured output")
    return report


def validate_warning(warning: object, name: str, nodeid: str, index: int) -> dict[str, Any]:
    if not isinstance(warning, dict) or set(warning) != REQUIRED_WARNING_FIELDS:
        raise ValueError(f"{name} warning {index} for {nodeid} is malformed")
    if (
        not isinstance(warning["message"], str)
        or not isinstance(warning["filename"], str)
        or not isinstance(warning["lineno"], int)
        or isinstance(warning["lineno"], bool)
        or warning["lineno"] < 1
    ):
        raise ValueError(f"{name} warning {index} for {nodeid} is invalid")
    return warning


def validate_session_warnings(run: dict[str, Any], name: str) -> list[dict[str, Any]]:
    raw = run.get("session_warnings")
    if not isinstance(raw, list):
        raise ValueError(f"{name} run has no ordered session warnings")
    warnings = []
    for index, warning in enumerate(raw):
        if not isinstance(warning, dict) or set(warning) != REQUIRED_WARNING_FIELDS | {"nodeid"}:
            raise ValueError(f"{name} session warning {index} is malformed")
        nodeid = warning["nodeid"]
        if not isinstance(nodeid, str):
            raise ValueError(f"{name} session warning {index} has an invalid node ID")
        validate_warning(
            {key: warning[key] for key in REQUIRED_WARNING_FIELDS}, name, nodeid, index
        )
        warnings.append(warning)
    return warnings


def validate_phase_protocol(reports: list[dict[str, Any]], name: str, nodeid: str) -> None:
    phases = [report["phase"] for report in reports]
    if (
        len(phases) < 2
        or phases[0] != "setup"
        or phases[-1] != "teardown"
        or any(phase != "call" for phase in phases[1:-1])
    ):
        raise ValueError(f"{name} case {nodeid} has invalid phase order: {phases}")
    setup = reports[0]
    teardown = reports[-1]
    has_call = len(reports) > 2
    if setup["outcome"] == "failed":
        raise ValueError(f"{name} case {nodeid} reports a setup-phase failure")
    if teardown["outcome"] == "failed":
        raise ValueError(f"{name} case {nodeid} reports a teardown-phase failure")
    if not has_call and setup["outcome"] == "passed":
        raise ValueError(f"{name} case {nodeid} passed setup without a call phase")
    if has_call and setup["outcome"] != "passed":
        raise ValueError(
            f"{name} case {nodeid} reports a call phase after {setup['outcome']} setup"
        )


def semantic_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project validated reports without interpreting their captured transcripts."""
    return [
        {
            "nodeid": case["nodeid"],
            "reports": [
                {field: report[field] for field in ("phase", "outcome", "exception")}
                for report in case["reports"]
            ],
            "warnings": case["warnings"],
        }
        for case in cases
    ]


def validate_cases(
    run: dict[str, Any], name: str, expected_nodeids: list[str]
) -> list[dict[str, Any]]:
    raw_cases = run.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError(f"{name} run has no cases array")
    actual_nodeids = [case.get("nodeid") if isinstance(case, dict) else None for case in raw_cases]
    if actual_nodeids != expected_nodeids:
        raise ValueError(
            f"{name} cases are missing, duplicated, extra, or out of order: "
            f"expected={expected_nodeids}, actual={actual_nodeids}"
        )
    cases: list[dict[str, Any]] = []
    for case in raw_cases:
        assert isinstance(case, dict)
        nodeid = case["nodeid"]
        reports = case.get("reports")
        if not isinstance(reports, list) or not reports:
            raise ValueError(f"{name} case {nodeid} has no ordered reports")
        validated = [
            validate_report(report, name, nodeid, index) for index, report in enumerate(reports)
        ]
        validate_phase_protocol(validated, name, nodeid)
        for report in validated:
            outcome = report["outcome"]
            if outcome == "error" and report["phase"] == "call":
                raise ValueError(f"{name} case {nodeid} reports a call-phase error")
            if outcome == "xpassed" and report["phase"] != "call":
                raise ValueError(f"{name} case {nodeid} reports xpassed outside the call phase")
            if outcome in {"passed", "xpassed"}:
                if report["exception"] is not None or report["diagnostic"] is not None:
                    raise ValueError(
                        f"{name} case {nodeid} has failure evidence for {outcome} phase"
                    )
            elif report["exception"] is None or not report["diagnostic"]:
                raise ValueError(f"{name} case {nodeid} has incomplete failure evidence")
        warnings = case.get("warnings")
        if not isinstance(warnings, list):
            raise ValueError(f"{name} case {nodeid} has no ordered warnings")
        validated_warnings = [
            validate_warning(warning, name, nodeid, index) for index, warning in enumerate(warnings)
        ]
        if set(case) != {"nodeid", "reports", "warnings"}:
            raise ValueError(f"{name} case {nodeid} has unexpected fields: {sorted(case)}")
        cases.append({"nodeid": nodeid, "reports": validated, "warnings": validated_warnings})
    return cases


def validate_rejections(
    rejections: dict[str, Any] | None,
    unsupported: list[dict[str, str]],
    *,
    stock_provenance: dict[str, Any],
    candidate_provenance: dict[str, Any],
) -> None:
    if not unsupported:
        if rejections is not None:
            raise ValueError("rejection evidence was supplied but no node is unsupported")
        return
    if not isinstance(rejections, dict):
        raise ValueError("unsupported nodes require a separate rejection record")
    raw = validate_rejection_envelope(rejections, stock_provenance, candidate_provenance)
    validate_rejection_rows(raw["rejections"], unsupported)


def validate_rejection_envelope(
    rejections: dict[str, Any],
    stock_provenance: dict[str, Any],
    candidate_provenance: dict[str, Any],
) -> dict[str, Any]:
    if set(rejections) != {
        "schema_version",
        "provenance",
        "rejections",
        "plugin_proofs",
        "hypothesis_lifecycle_proof",
    }:
        raise ValueError("rejection record has an invalid schema")
    if rejections["schema_version"] != 3 or isinstance(rejections["schema_version"], bool):
        raise ValueError("rejection record has an unsupported schema version")
    provenance = rejections["provenance"]
    provenance_fields = {
        "upstream_commit",
        "inventory_sha256",
        "policy_sha256",
        "python",
        "platform",
        "stock_runner_artifact_set_sha256",
        "stock_resolved_environment_sha256",
        "runner_artifact_set_sha256",
        "resolved_environment_sha256",
        "project_identity_sha256",
        "checker_artifacts",
    }
    if not isinstance(provenance, dict) or set(provenance) != provenance_fields:
        raise ValueError("rejection record provenance is invalid")
    expected_provenance = {
        "upstream_commit": candidate_provenance["upstream_commit"],
        "inventory_sha256": candidate_provenance["inventory_sha256"],
        "policy_sha256": candidate_provenance["policy_sha256"],
        "python": candidate_provenance["python"],
        "platform": candidate_provenance["platform"],
        "stock_runner_artifact_set_sha256": stock_provenance["runner_artifact_set_sha256"],
        "stock_resolved_environment_sha256": stock_provenance["resolved_environment_sha256"],
        "runner_artifact_set_sha256": candidate_provenance["runner_artifact_set_sha256"],
        "resolved_environment_sha256": candidate_provenance["resolved_environment_sha256"],
    }
    for field, expected in expected_provenance.items():
        if provenance.get(field) != expected:
            raise ValueError(f"rejection evidence provenance differs for {field}")
    identity = provenance["project_identity_sha256"]
    if not is_sha256(identity):
        raise ValueError("rejection evidence project identity hash is invalid")
    if identity in {
        stock_provenance["project_identity_sha256"],
        candidate_provenance["project_identity_sha256"],
    }:
        raise ValueError("rejection evidence must use an independent project root")
    validate_rejection_checker_artifacts(provenance["checker_artifacts"])
    raw_rejections = rejections.get("rejections")
    if not isinstance(raw_rejections, list):
        raise ValueError("rejection record has no rejections array")
    plugin_proofs = validate_plugin_proofs(
        rejections.get("plugin_proofs"),
        stock_provenance,
        candidate_provenance,
        required=False,
    )
    lifecycle_proof = rejections.get("hypothesis_lifecycle_proof")
    if lifecycle_proof is not None:
        lifecycle_proof = validate_hypothesis_lifecycle_proof(lifecycle_proof, provenance)
    return {
        "rejections": raw_rejections,
        "plugin_proofs": plugin_proofs,
        "hypothesis_lifecycle_proof": lifecycle_proof,
    }


def validate_hypothesis_lifecycle_proof(
    raw: object, provenance: dict[str, Any]
) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) != len(HYPOTHESIS_LATE_AUTOLOAD_NODEIDS):
        raise ValueError("Hypothesis late-autoload lifecycle proofs are missing or extra")
    return [
        validate_hypothesis_lifecycle_row(row, provenance, nodeid)
        for row, nodeid in zip(raw, HYPOTHESIS_LATE_AUTOLOAD_NODEIDS, strict=True)
    ]


def validate_hypothesis_lifecycle_row(
    raw: object, provenance: dict[str, Any], nodeid: str
) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != HYPOTHESIS_LIFECYCLE_PROOF_FIELDS:
        raise ValueError("Hypothesis late-autoload lifecycle proof has an invalid schema")
    passes_outer = nodeid.endswith("::test_no_header_trailer_info")
    if (
        raw["proof_case"] != "hypothesis-late-autoload-corpus-case"
        or raw["upstream_commit"] != provenance["upstream_commit"]
        or raw["pytest_arguments"]
        != ["-q", "-rP", "-p", "no:cacheprovider", HYPOTHESIS_LIFECYCLE_SETUP_NODEID, nodeid]
        or not isinstance(raw["outer_exit_status"], int)
        or isinstance(raw["outer_exit_status"], bool)
        or raw["outer_exit_status"] != (0 if passes_outer else 1)
        or raw["outer_test_outcomes"] != ["passed", "passed" if passes_outer else "failed"]
        or raw["outer_failure_signature"] != HYPOTHESIS_OUTER_FAILURE_SIGNATURES[nodeid]
        or raw["nested_warning_type"] != "HypothesisSideeffectWarning"
        or raw["nested_warning_text"]
        != "between importing hypothesis and loading the hypothesis plugin"
    ):
        raise ValueError("Hypothesis late-autoload lifecycle proof does not reproduce the failure")
    source_files = raw["source_files"]
    if not isinstance(source_files, list) or len(source_files) != len(
        HYPOTHESIS_LIFECYCLE_SOURCE_PATHS
    ):
        raise ValueError("Hypothesis late-autoload lifecycle proof has invalid source hashes")
    for index, (source, expected_path) in enumerate(
        zip(source_files, HYPOTHESIS_LIFECYCLE_SOURCE_PATHS, strict=True)
    ):
        if (
            not isinstance(source, dict)
            or set(source) != {"path", "sha256"}
            or source["path"] != expected_path
            or not is_sha256(source["sha256"])
        ):
            raise ValueError(f"Hypothesis late-autoload source record {index} is invalid")
    return raw


def validate_plugin_proofs(
    raw: object,
    stock_provenance: dict[str, Any],
    candidate_provenance: dict[str, Any],
    *,
    required: bool,
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("rejection record has no plug-in proofs array")
    if not required and not raw:
        return []
    actual_cases = [proof.get("proof_case") if isinstance(proof, dict) else None for proof in raw]
    expected_cases = list(PLUGIN_REJECTION_CASES)
    if actual_cases != expected_cases:
        raise ValueError(
            "plug-in proofs are missing, duplicated, extra, or out of order: "
            f"expected={expected_cases}, actual={actual_cases}"
        )
    packages = dict(validate_resolved_environment(candidate_provenance, "candidate"))
    stock_packages = dict(validate_resolved_environment(stock_provenance, "stock"))
    points = candidate_provenance["resolved_environment"]["pytest_plugins"]
    entrypoints = {point["name"]: point for point in points}
    return [
        validate_plugin_proof(value, index, entrypoints, packages, stock_packages)
        for index, value in enumerate(raw)
    ]


def validate_plugin_proof_identity(
    value: dict[str, Any],
    plugin_entrypoint: str,
    entrypoints: dict[str, dict[str, str]],
    packages: dict[str, str],
    stock_packages: dict[str, str],
) -> None:
    proof_case = value["proof_case"]
    identity = value["stock_loaded_identity"]
    if not isinstance(identity, dict) or set(identity) != {"name", "distribution", "version"}:
        raise ValueError(f"plug-in proof {proof_case!r} has no loaded stock identity")
    if not all(isinstance(item, str) and item for item in identity.values()):
        raise ValueError(f"plug-in proof {proof_case!r} has an invalid loaded stock identity")
    distribution = normalized_distribution(identity["distribution"])
    if identity["distribution"] != distribution:
        raise ValueError(f"plug-in proof {proof_case!r} distribution is not normalized")
    point = entrypoints.get(plugin_entrypoint)
    if point is None:
        raise ValueError(f"plug-in proof {proof_case!r} entrypoint is absent from the lock")
    expected_identity = {
        "name": point["value"],
        "distribution": normalized_distribution(point["distribution"]),
        "version": point["version"],
    }
    if identity != expected_identity:
        raise ValueError(
            f"plug-in proof {proof_case!r} stock identity differs from the locked entrypoint"
        )
    if packages.get(distribution) != identity["version"]:
        raise ValueError(f"plug-in proof {proof_case!r} contradicts candidate package versions")
    if stock_packages.get(distribution) != identity["version"]:
        raise ValueError(f"plug-in proof {proof_case!r} contradicts stock package versions")


def validate_plugin_proof(
    value: object,
    index: int,
    entrypoints: dict[str, dict[str, str]],
    packages: dict[str, str],
    stock_packages: dict[str, str],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != PLUGIN_PROOF_FIELDS:
        raise ValueError(f"plug-in proof {index} has an invalid schema")
    proof_case = value["proof_case"]
    plugin_entrypoint = value["plugin_entrypoint"]
    if (
        not isinstance(proof_case, str)
        or not isinstance(plugin_entrypoint, str)
        or proof_case not in PLUGIN_REJECTION_CASES
        or plugin_entrypoint != PLUGIN_REJECTION_CASES[proof_case]
    ):
        raise ValueError(f"plug-in proof {index} has an unexpected entrypoint identity")
    arguments = value["arguments"]
    if arguments != PLUGIN_REJECTION_ARGUMENTS[proof_case]:
        raise ValueError(f"plug-in proof {proof_case!r} used unexpected pytest arguments")
    stock_status = value["stock_exit_status"]
    if not isinstance(stock_status, int) or isinstance(stock_status, bool):
        raise ValueError(f"plug-in proof {proof_case!r} has an invalid stock exit status")
    outcome_fields = (
        stock_status == 0,
        value["stock_collection_started"] is True,
        value["stock_test_body_ran"] is True,
        value["candidate_exit_status"] == 4,
        value["test_body_ran"] is False,
        value["candidate_collection_started"] is False,
        value["candidate_cache_unchanged"] is True,
    )
    if not all(outcome_fields):
        raise ValueError(f"plug-in proof {proof_case!r} did not prove stock success and rejection")
    validate_plugin_proof_diagnostic(value, proof_case)
    validate_plugin_proof_fixtures(value, proof_case)
    validate_plugin_proof_identity(value, plugin_entrypoint, entrypoints, packages, stock_packages)
    return value


def validate_plugin_proof_diagnostic(value: dict[str, Any], proof_case: str) -> None:
    for field in ("diagnostic", "expected_diagnostic"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"plug-in proof {proof_case!r} has no {field}")
    if value["expected_diagnostic"] not in value["diagnostic"]:
        raise ValueError(f"plug-in proof {proof_case!r} has no named rejection diagnostic")


def validate_plugin_proof_fixtures(value: dict[str, Any], proof_case: str) -> None:
    if not is_sha256(value["fixture_sha256"]):
        raise ValueError(f"plug-in proof {proof_case!r} has an invalid fixture hash")
    before = value["candidate_cache_sha256_before"]
    after = value["candidate_cache_sha256_after"]
    if not is_sha256(before) or not is_sha256(after) or before != after:
        raise ValueError(f"plug-in proof {proof_case!r} changed its seeded cache")
    roots = (
        value["stock_project_identity_sha256"],
        value["candidate_project_identity_sha256"],
    )
    if not all(is_sha256(root) for root in roots) or roots[0] == roots[1]:
        raise ValueError(f"plug-in proof {proof_case!r} lacks separate project roots")


def validate_rejection_rows(raw: list[Any], unsupported: list[dict[str, str]]) -> None:
    actual_nodeids = [row.get("nodeid") if isinstance(row, dict) else None for row in raw]
    expected_nodeids = [record["nodeid"] for record in unsupported]
    if actual_nodeids != expected_nodeids:
        raise ValueError(
            "rejections are missing, duplicated, extra, or out of order: "
            f"expected={expected_nodeids}, actual={actual_nodeids}"
        )
    for classification, rejection in zip(unsupported, raw, strict=True):
        assert isinstance(rejection, dict)
        required = {
            "nodeid",
            "proof",
            "proof_case",
            "exit_status",
            "diagnostic",
            "expected_diagnostic",
            "fixture_sha256",
            "test_body_ran",
        }
        proof_case_value = rejection.get("proof_case")
        if isinstance(proof_case_value, str) and proof_case_value in PLUGIN_REJECTION_ARGUMENTS:
            required.add("arguments")
        if set(rejection) != required:
            raise ValueError(
                f"rejection for {classification['nodeid']} has unexpected fields: "
                f"{sorted(rejection)}"
            )
        if rejection["proof"] != classification.get("rejection_proof"):
            raise ValueError(f"rejection proof differs for {classification['nodeid']}")
        proof = rejection["proof"]
        proof_case = rejection["proof_case"]
        if (
            not isinstance(proof, str)
            or not proof.startswith(REJECTION_PROOF_PREFIX)
            or not isinstance(proof_case, str)
            or proof.removeprefix(REJECTION_PROOF_PREFIX) != proof_case
        ):
            raise ValueError(
                f"rejection proof is not an executable checker case for {classification['nodeid']}"
            )
        if (
            proof_case in PLUGIN_REJECTION_ARGUMENTS
            and rejection["arguments"] != (PLUGIN_REJECTION_ARGUMENTS[proof_case])
        ):
            raise ValueError(f"rejection arguments differ for {classification['nodeid']}")
        status = rejection["exit_status"]
        if not isinstance(status, int) or isinstance(status, bool) or status != 4:
            raise ValueError(f"rejection exit status is invalid for {classification['nodeid']}")
        diagnostic = rejection["diagnostic"]
        expected_diagnostic = rejection["expected_diagnostic"]
        if (
            not isinstance(diagnostic, str)
            or not diagnostic
            or not isinstance(expected_diagnostic, str)
            or not expected_diagnostic
            or expected_diagnostic not in diagnostic
        ):
            raise ValueError(f"rejection diagnostic is missing for {classification['nodeid']}")
        if not is_sha256(rejection["fixture_sha256"]):
            raise ValueError(f"rejection fixture hash is invalid for {classification['nodeid']}")
        if rejection["test_body_ran"] is not False:
            raise ValueError(f"unsupported test body ran for {classification['nodeid']}")


def validate_rejection_checker_artifacts(raw: object) -> None:
    if not isinstance(raw, list) or not raw:
        raise ValueError("rejection evidence has no checker source provenance")
    records: list[dict[str, str]] = []
    for index, record in enumerate(raw):
        if (
            not isinstance(record, dict)
            or set(record) != {"filename", "sha256"}
            or not isinstance(record["filename"], str)
            or not record["filename"]
            or not is_sha256(record["sha256"])
        ):
            raise ValueError(f"rejection checker artifact {index} is invalid")
        records.append(record)
    expected_names = sorted(REJECTION_CHECKER_FILES)
    if [record["filename"] for record in records] != expected_names:
        raise ValueError("rejection evidence has unexpected checker source files")
    for record in records:
        source = Path(__file__).resolve().parents[1] / record["filename"]
        if (
            not source.is_file()
            or hashlib.sha256(source.read_bytes()).hexdigest() != record["sha256"]
        ):
            raise ValueError(f"rejection checker source hash differs for {record['filename']}")


def validate_completed_run_status(
    run: dict[str, Any], name: str, cases: list[dict[str, Any]]
) -> int:
    status = run.get("exit_status")
    if (
        not isinstance(status, int)
        or isinstance(status, bool)
        or status not in COMPLETED_PYTEST_EXIT_STATUSES
    ):
        raise ValueError(f"{name} run did not complete as a pytest test session: {status!r}")
    has_test_failure = any(
        report["outcome"] in {"failed", "error"} for case in cases for report in case["reports"]
    )
    if (status == 1) != has_test_failure:
        raise ValueError(f"{name} exit status {status} does not match its recorded test outcomes")
    return status


def verify_accounting(
    classification: dict[str, Any],
    stock: dict[str, Any],
    candidate: dict[str, Any],
    rejections: dict[str, Any] | None = None,
    *,
    reference_scope: Literal["full-classification", "supported-cases"] = "full-classification",
) -> dict[str, object]:
    records = classified_nodes(classification)
    nodeids = [record["nodeid"] for record in records]
    unsupported = [
        record for record in records if record["disposition"] == "deliberately-unsupported"
    ]
    platform_skipped = [
        record for record in records if record["disposition"] == "reference/platform-skipped"
    ]
    parity_required = [record for record in records if record["disposition"] == "parity-required"]
    compared = [record for record in records if record["disposition"] != "deliberately-unsupported"]
    supported_nodeids = [record["nodeid"] for record in compared]
    if reference_scope not in {"full-classification", "supported-cases"}:
        raise ValueError(f"unknown stock reference scope: {reference_scope!r}")
    stock_nodeids = supported_nodeids if reference_scope == "supported-cases" else nodeids
    stock_provenance, candidate_provenance = validate_run_pair_provenance(
        classification, stock, candidate
    )
    validate_rejections(
        rejections,
        unsupported,
        stock_provenance=stock_provenance,
        candidate_provenance=candidate_provenance,
    )
    expected_inventory_hash = classification.get("inventory", {}).get("sha256")
    expected_policy_hash = classification.get("policy", {}).get("sha256")
    if (
        stock_provenance["inventory_sha256"] != expected_inventory_hash
        or stock_provenance["policy_sha256"] != expected_policy_hash
    ):
        raise ValueError("run provenance does not match the classified inventory and policy")
    stock_cases = validate_cases(stock, "stock", stock_nodeids)
    candidate_cases = validate_cases(candidate, "candidate", supported_nodeids)
    stock_cases_by_nodeid = {case["nodeid"]: case for case in stock_cases}
    for record in platform_skipped:
        case = stock_cases_by_nodeid[record["nodeid"]]
        if not any(report["outcome"] == "skipped" for report in case["reports"]):
            raise ValueError(
                f"reference/platform-skipped case did not skip in stock: {record['nodeid']}"
            )
    stock_collected = stock.get("collected")
    candidate_collected = candidate.get("collected")
    if type(stock_collected) is not int or stock_collected != len(stock_nodeids):
        raise ValueError(
            "stock collected count differs from classification: "
            f"{stock_collected!r} != {len(stock_nodeids)}"
        )
    if type(candidate_collected) is not int or candidate_collected != len(supported_nodeids):
        raise ValueError(
            "candidate collected count differs from supported classification: "
            f"{candidate_collected!r} != {len(supported_nodeids)}"
        )
    supported_nodeid_set = set(supported_nodeids)
    stock_supported_cases = [case for case in stock_cases if case["nodeid"] in supported_nodeid_set]
    stock_semantics = semantic_cases(stock_supported_cases)
    candidate_semantics = semantic_cases(candidate_cases)
    if stock_semantics != candidate_semantics:
        for index, (stock_case, candidate_case) in enumerate(
            zip(stock_semantics, candidate_semantics, strict=True)
        ):
            if stock_case != candidate_case:
                raise ValueError(
                    f"candidate observation differs at case {index} ({stock_case['nodeid']}): "
                    f"stock={stock_case}, candidate={candidate_case}"
                )
    stock_session_warnings = validate_session_warnings(stock, "stock")
    candidate_session_warnings = validate_session_warnings(candidate, "candidate")
    if stock_session_warnings != candidate_session_warnings:
        raise ValueError(
            "candidate session warnings differ from stock: "
            f"stock={stock_session_warnings}, candidate={candidate_session_warnings}"
        )
    stock_exit = validate_completed_run_status(stock, "stock", stock_cases)
    candidate_exit = validate_completed_run_status(candidate, "candidate", candidate_cases)
    if candidate_exit != stock_exit:
        raise ValueError(
            f"candidate exit status differs from stock: {candidate_exit!r} != {stock_exit}"
        )
    return {
        "accounted_cases": len(nodeids),
        "parity_cases": len(parity_required),
        "platform_skipped_cases": len(platform_skipped),
        "compared_cases": len(compared),
        "rejected_cases": len(unsupported),
        "ordered_semantics_equal": True,
        "comparison": "outer-session-semantics",
        "captured_transcripts_compared": False,
        "full_parity": not unsupported,
        "parity_scope": "full-classification" if not unsupported else "supported-cases-only",
        "exit_status": stock_exit,
        "upstream_commit": stock_provenance["upstream_commit"],
        "python": stock_provenance["python"],
        "platform": stock_provenance["platform"],
        "stock_runner_artifact_set_sha256": stock_provenance["runner_artifact_set_sha256"],
        "candidate_runner_artifact_set_sha256": candidate_provenance["runner_artifact_set_sha256"],
    }


def validate_three_run_classification(
    classification: dict[str, Any],
) -> tuple[list[dict[str, str]], list[str], list[dict[str, str]]]:
    records = classified_nodes(classification)
    nodeids = [record["nodeid"] for record in records]
    unsupported = [
        record for record in records if record["disposition"] == "deliberately-unsupported"
    ]
    actual_unsupported = {
        record["nodeid"]: record.get("rejection_proof", "").removeprefix(REJECTION_PROOF_PREFIX)
        for record in unsupported
    }
    if actual_unsupported != PLUGIN_DEPENDENT_UNSUPPORTED:
        raise ValueError(
            "the three-run corpus gate permits only the proven Hypothesis-dependent node: "
            f"expected={PLUGIN_DEPENDENT_UNSUPPORTED}, actual={actual_unsupported}"
        )
    if not any(
        record["nodeid"] == XDIST_OUTER_PLUGIN_NODEID
        and record["disposition"] == "reference/platform-skipped"
        for record in records
    ):
        raise ValueError("the no-autoload corpus must account for the skipped outer xdist case")
    return records, nodeids, unsupported


def validate_three_run_provenance(
    classification: dict[str, Any],
    full_stock: dict[str, Any],
    stock_reference: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    full_provenance = validate_provenance(full_stock, "full stock")
    stock_provenance = validate_provenance(stock_reference, "stock reference")
    candidate_provenance = validate_provenance(candidate, "candidate")
    run_provenance = (full_provenance, stock_provenance, candidate_provenance)
    project_identities = [item["project_identity_sha256"] for item in run_provenance]
    if len(set(project_identities)) != 3:
        raise ValueError("full stock, stock reference, and candidate need separate project roots")

    expected_commit = classification.get("inventory", {}).get("upstream_commit")
    for provenance in run_provenance:
        if (
            provenance["upstream_commit"] != expected_commit
            or provenance["inventory_sha256"] != classification["inventory"]["sha256"]
            or provenance["policy_sha256"] != classification["policy"]["sha256"]
        ):
            raise ValueError("three-run provenance does not match the classified corpus")
    common_fields = (
        "python",
        "platform",
        "runner_artifact_set_sha256",
        "resolved_environment_sha256",
    )
    for field in common_fields:
        if len({provenance[field] for provenance in run_provenance}) != 1:
            raise ValueError(f"three-run provenance differs for {field}")
    source_root = Path(__file__).resolve().parents[1] / "scripts"
    for name, provenance in zip(
        ("full stock", "stock reference", "candidate"), run_provenance, strict=True
    ):
        rows = provenance["harness_artifacts"]
        for filename in THREE_RUN_HARNESS_FILES:
            matches = [row for row in rows if row["filename"] == filename]
            source = source_root / filename
            expected_hash = (
                hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else None
            )
            if len(matches) != 1 or matches[0]["sha256"] != expected_hash:
                raise ValueError(f"{name} harness source hash differs for {filename}")
    comparable_fields = (
        "harness_artifacts",
        "runner_artifacts",
        "installed_runner_artifacts",
        "resolved_environment",
    )
    for field in comparable_fields:
        fingerprints = {
            json.dumps(provenance[field], separators=(",", ":"), sort_keys=True)
            for provenance in run_provenance
        }
        if len(fingerprints) != 1:
            raise ValueError(f"three-run {field.replace('_', ' ')} differ")
    resolved_environments = [provenance["resolved_environment"] for provenance in run_provenance]
    if not (resolved_environments[0] == resolved_environments[1] == resolved_environments[2]):
        raise ValueError("three-run resolved dependency environments differ")
    dependency_lock_hashes = [
        tuple(sorted(row["sha256"] for row in provenance["dependency_locks"]))
        for provenance in run_provenance
    ]
    if len(set(dependency_lock_hashes)) != 1:
        raise ValueError("three-run dependency lock contents differ")
    return full_provenance, stock_provenance, candidate_provenance


def validate_three_run_invocations(
    full_provenance: dict[str, Any],
    stock_provenance: dict[str, Any],
    candidate_provenance: dict[str, Any],
    expected_nodeids: list[str],
) -> None:
    expected_arguments = ["-q", "--runpytest=inprocess", "--confcutdir=<CORPUS_PARENT>"] + [
        f"--deselect={nodeid}" for nodeid in PLUGIN_DEPENDENT_UNSUPPORTED
    ]
    if (
        full_provenance["runner_distribution"] != "pytest"
        or full_provenance["plugin_autoload"] != "enabled"
        or full_provenance["pytest_arguments"] != ["-q"]
        or full_provenance["explicit_deselections"] != []
    ):
        raise ValueError("full stock baseline must use ordinary pytest plug-in autoload")
    if (
        stock_provenance["runner_distribution"] != "pytest"
        or stock_provenance["plugin_autoload"] != "disabled"
        or stock_provenance["pytest_arguments"] != expected_arguments
        or stock_provenance["explicit_deselections"] != list(PLUGIN_DEPENDENT_UNSUPPORTED)
    ):
        raise ValueError("stock reference must disable autoload and deselect only proved nodes")
    if (
        candidate_provenance["runner_distribution"] != "vfy"
        or candidate_provenance["plugin_autoload"] != "disabled"
        or candidate_provenance["pytest_arguments"] != expected_arguments
        or candidate_provenance["explicit_deselections"] != list(PLUGIN_DEPENDENT_UNSUPPORTED)
    ):
        raise ValueError("candidate must match the controlled stock reference invocation")
    full_control = full_provenance.get("pytester_control")
    stock_control = stock_provenance.get("pytester_control")
    candidate_control = candidate_provenance.get("pytester_control")
    expected_exception_ids = list(HYPOTHESIS_LATE_AUTOLOAD_NODEIDS)
    expected_inventory_nodeids_sha256 = hashlib.sha256(
        (json.dumps(expected_nodeids, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    if (
        not isinstance(full_control, dict)
        or full_control["mode"] != "default"
        or full_control["ancestor_config_applied"]
        or full_control["subprocess_nodeids"] != []
    ):
        raise ValueError("full stock baseline must retain pytester's default inprocess mode")
    if not isinstance(stock_control, dict):
        raise ValueError("stock reference pytester control provenance is invalid")
    if not isinstance(candidate_control, dict):
        raise ValueError("candidate pytester control provenance is invalid")
    for role, control in (("stock reference", stock_control), ("candidate", candidate_control)):
        if (
            not isinstance(control, dict)
            or control["mode"] != "inprocess-with-exceptions"
            or not control["ancestor_config_applied"]
            or control["subprocess_nodeids"] != expected_exception_ids
        ):
            raise ValueError(f"{role} must use the exact public pytester config fixture protocol")
    if (
        stock_control["config_template_sha256"] != candidate_control["config_template_sha256"]
        or stock_control["config_template_sha256"] != full_control["config_template_sha256"]
    ):
        raise ValueError("three-run pytester config template hashes differ")
    for role, control in (
        ("full stock", full_control),
        ("stock reference", stock_control),
        ("candidate", candidate_control),
    ):
        if (
            control["inventory_nodeid_count"] != len(expected_nodeids)
            or control["inventory_nodeids_sha256"] != expected_inventory_nodeids_sha256
        ):
            raise ValueError(f"{role} execution inventory differs from the corpus")
    if stock_provenance["loaded_pytest_plugins"] or candidate_provenance["loaded_pytest_plugins"]:
        raise ValueError(
            "the controlled no-autoload stock and candidate runs must load no plug-ins"
        )


def validate_full_stock_run(
    full_stock: dict[str, Any], nodeids: list[str]
) -> tuple[list[dict[str, Any]], int]:
    full_stock_cases = validate_cases(full_stock, "full stock", nodeids)
    if len(nodeids) != PINNED_CORE_NODE_COUNT:
        raise ValueError(
            f"the pinned full stock baseline must account for {PINNED_CORE_NODE_COUNT} nodes, "
            f"got {len(nodeids)}"
        )
    if full_stock.get("collected") != len(nodeids):
        raise ValueError(
            "full stock baseline did not collect the complete classification: "
            f"{full_stock.get('collected')!r} != {len(nodeids)}"
        )
    full_stock_exit = validate_completed_run_status(full_stock, "full stock", full_stock_cases)
    if full_stock_exit != 0:
        raise ValueError(f"full stock baseline must exit successfully, got {full_stock_exit}")
    return full_stock_cases, full_stock_exit


def validate_three_run_plugin_evidence(
    rejections: dict[str, Any],
    unsupported: list[dict[str, str]],
    stock_provenance: dict[str, Any],
    candidate_provenance: dict[str, Any],
    project_identities: list[str],
    full_provenance: dict[str, Any],
) -> None:
    rejection_envelope = validate_rejection_envelope(
        rejections, stock_provenance, candidate_provenance
    )
    validate_rejection_rows(rejection_envelope["rejections"], unsupported)
    plugin_proofs = validate_plugin_proofs(
        rejection_envelope["plugin_proofs"],
        stock_provenance,
        candidate_provenance,
        required=True,
    )
    if rejection_envelope["hypothesis_lifecycle_proof"] is None:
        raise ValueError(
            "the controlled pytester protocol requires its ordered inprocess lifecycle diagnostic"
        )
    proof_project_hashes = {
        identity
        for proof in plugin_proofs
        for identity in (
            proof["stock_project_identity_sha256"],
            proof["candidate_project_identity_sha256"],
        )
    }
    if len(proof_project_hashes) != 2 * len(plugin_proofs):
        raise ValueError("each plug-in rejection proof must use its own stock and candidate roots")
    if proof_project_hashes.intersection(project_identities):
        raise ValueError("plug-in rejection proofs must use roots separate from corpus runs")
    expected_plugins = [proof["stock_loaded_identity"] for proof in plugin_proofs]
    # Main xdist is supported. Its ordinary full-stock presence is bound to the
    # installed lock, rather than an unsupported-plug-in rejection fixture.
    points = full_provenance["resolved_environment"]["pytest_plugins"]
    xdist_points = [point for point in points if point["name"] == "xdist"]
    if xdist_points != [
        {
            "name": "xdist",
            "value": "xdist.plugin",
            "distribution": "pytest-xdist",
            "version": "3.8.0",
        }
    ]:
        raise ValueError("full stock requires the pinned pytest-xdist 3.8.0 entrypoint")
    expected_plugins.append(
        {"name": "xdist.plugin", "distribution": "pytest-xdist", "version": "3.8.0"}
    )
    actual_plugins = full_provenance["loaded_pytest_plugins"]

    def identity_key(plugin: dict[str, Any]) -> tuple[str, str, str]:
        name = plugin["name"]
        distribution = plugin["distribution"]
        version = plugin["version"]
        assert isinstance(name, str)
        assert isinstance(distribution, str)
        assert isinstance(version, str)
        return (name, distribution, version)

    if sorted(map(identity_key, actual_plugins)) != sorted(map(identity_key, expected_plugins)):
        raise ValueError(
            "full stock loaded plug-ins differ from the exact proof-bound allowlist: "
            f"actual={actual_plugins}, expected={expected_plugins}"
        )
    if len({identity_key(plugin) for plugin in actual_plugins}) != len(actual_plugins):
        raise ValueError("full stock loaded plug-in identities contain duplicates")


def verify_three_run_accounting(
    classification: dict[str, Any],
    full_stock: dict[str, Any],
    stock_reference: dict[str, Any],
    candidate: dict[str, Any],
    rejections: dict[str, Any],
) -> dict[str, object]:
    """Require a complete stock run and compare only the controlled core pair."""
    _, nodeids, unsupported = validate_three_run_classification(classification)
    full_provenance, stock_provenance, candidate_provenance = validate_three_run_provenance(
        classification, full_stock, stock_reference, candidate
    )
    validate_three_run_invocations(full_provenance, stock_provenance, candidate_provenance, nodeids)
    project_identities = [
        full_provenance["project_identity_sha256"],
        stock_provenance["project_identity_sha256"],
        candidate_provenance["project_identity_sha256"],
    ]
    full_stock_cases, full_stock_exit = validate_full_stock_run(full_stock, nodeids)
    validate_three_run_plugin_evidence(
        rejections,
        unsupported,
        stock_provenance,
        candidate_provenance,
        project_identities,
        full_provenance,
    )
    compared = verify_accounting(
        classification,
        stock_reference,
        candidate,
        rejections,
        reference_scope="supported-cases",
    )
    if compared["exit_status"] != 0:
        raise ValueError("the supported no-autoload stock and candidate runs must both pass")
    return {
        **compared,
        "supported_cases": compared["compared_cases"],
        "unsupported_cases": compared["rejected_cases"],
        "candidate_selection_route": "exact-node-deselection",
        "native_compact_collection_parity": False,
        "full_stock_cases": len(full_stock_cases),
        "full_stock_exit_status": full_stock_exit,
        "full_stock_complete": True,
        "full_stock_green": True,
        "plugin_autoload_comparison": "disabled stock reference and candidate only",
        "full_stock_loaded_plugins_proof_bound": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--classification", required=True, type=Path)
    parser.add_argument("--stock", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--rejections", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify_accounting(
        load_object(args.classification),
        load_object(args.stock),
        load_object(args.candidate),
        load_object(args.rejections) if args.rejections else None,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        write_atomic(args.output, rendered)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
