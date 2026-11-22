"""Check that unsupported native pytest inputs fail before test execution."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmarks.benchmark_harness import benchmark_environment
from scripts import pytest_execution_accounting, pytest_execution_record
from scripts.process_tree import run_captured

TEST = """import os
from pathlib import Path


def test_runs():
    Path(os.environ["PYTEST_NATIVE_REJECTION_EVENT"]).write_text("ran\\n", encoding="utf-8")
"""

PLUGIN_TEST = """import os
from pathlib import Path
import json


Path(os.environ["PYTEST_NATIVE_REJECTION_COLLECTION_EVENT"]).write_text(
    "collected\\n", encoding="utf-8"
)


def test_plugin_is_registered(request):
    plugin_name = os.environ["PYTEST_NATIVE_REJECTION_PLUGIN"]
    manager = request.config.pluginmanager
    plugin = manager.get_plugin(plugin_name)
    assert plugin is not None
    for loaded_plugin, distribution in manager.list_plugin_distinfo():
        if loaded_plugin is plugin:
            identity = {
                "name": manager.get_canonical_name(plugin),
                "distribution": distribution.metadata["Name"],
                "version": distribution.version,
            }
            Path(os.environ["PYTEST_NATIVE_REJECTION_PLUGIN_IDENTITY_EVENT"]).write_text(
                json.dumps(identity, sort_keys=True), encoding="utf-8"
            )
            break
    else:
        raise AssertionError(f"plug-in {plugin_name!r} has no distribution identity")
    Path(os.environ["PYTEST_NATIVE_REJECTION_EVENT"]).write_text(
        "ran\\n", encoding="utf-8"
    )
"""

HYPOTHESIS_LIFECYCLE_TEST = """import json
import os
import warnings
from pathlib import Path

from hypothesis.configuration import storage_directory
from hypothesis.errors import HypothesisSideeffectWarning


def _disable_unrelated_plugins():
    names = json.loads(os.environ["PYTEST_NATIVE_HYPOTHESIS_BLOCKED_PLUGINS"])
    return [argument for name in names for argument in ("-p", f"no:{name}")]


def test_00_hypothesis_storage_side_effect(pytestconfig):
    expected_plugin_loaded = os.environ["PYTEST_NATIVE_HYPOTHESIS_EXPECT_OUTER_PLUGIN"] == "1"
    assert (
        pytestconfig.pluginmanager.get_plugin("hypothesispytest") is not None
    ) is expected_plugin_loaded
    # Leave a real Hypothesis initialization side effect for a later nested load.
    storage_directory("examples").create_if_missing()


def test_10_nested_autoload(pytester, monkeypatch):
    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)
    warnings.simplefilter("error", HypothesisSideeffectWarning)
    result = pytester.runpytest(
        "--version", "--version", *_disable_unrelated_plugins()
    )
    record = {
        "exit_status": result.ret,
        "stderr": result.stderr.str(),
        "stdout": result.stdout.str(),
    }
    Path(os.environ["PYTEST_NATIVE_HYPOTHESIS_PROBE_RESULT"]).write_text(
        json.dumps(record, sort_keys=True), encoding="utf-8"
    )
    expected_exit = int(os.environ["PYTEST_NATIVE_HYPOTHESIS_EXPECTED_EXIT"])
    assert result.ret == expected_exit
    if expected_exit == 3:
        assert "HypothesisSideeffectWarning" in result.stderr.str()
        assert "between importing hypothesis and loading the hypothesis plugin" in (
            result.stderr.str()
        )
"""

FAILURE_TEST = """import os
from pathlib import Path


def test_fails():
    Path(os.environ["PYTEST_NATIVE_REJECTION_EVENT"]).write_text("ran\\n", encoding="utf-8")
    assert False, "native failure detail"
"""

RUNTIME_PLUGIN = """def pytest_runtest_setup(item):
    del item
"""

CONFIGURE_PLUGIN = """import os
from pathlib import Path


def pytest_configure(config):
    del config
    Path(os.environ["PYTEST_NATIVE_REJECTION_CONFIG_EVENT"]).write_text(
        "configured\\n", encoding="utf-8"
    )
"""

COLLECTION_PLUGIN = """import os
from pathlib import Path


def pytest_collection_modifyitems(session, config, items):
    del session, config, items
    Path(os.environ["PYTEST_NATIVE_REJECTION_CONFIG_EVENT"]).write_text(
        "collected\\n", encoding="utf-8"
    )
"""

RUNLOOP_REPLACEMENT_PLUGIN = """import os
from pathlib import Path


def pytest_runtestloop(session):
    del session
    Path(os.environ["PYTEST_NATIVE_REJECTION_CONFIG_EVENT"]).write_text(
        "replaced\\n", encoding="utf-8"
    )
    return True
"""

CONFIGURE_HOOK = """import os
from pathlib import Path


def pytest_configure(config):
    del config
    Path(os.environ["PYTEST_NATIVE_REJECTION_CONFIG_EVENT"]).write_text(
        "configured\\n", encoding="utf-8"
    )
"""


@dataclass(frozen=True)
class Rejection:
    name: str
    arguments: tuple[str, ...]
    diagnostic: str
    test_source: str = TEST
    use_cacheprovider: bool = False
    conftest_source: str | None = None
    plugin_name: str | None = None


REJECTIONS = (
    Rejection("runtime-plugin", ("-p", "runtime_plugin"), "runtime_plugin"),
    Rejection("configure-plugin", ("-p", "configure_plugin"), "pytest_configure"),
    Rejection("collection-plugin", ("-p", "collection_plugin"), "pytest_collection_modifyitems"),
    Rejection(
        "runloop-replacement-plugin", ("-p", "runloop_replacement_plugin"), "pytest_runtestloop"
    ),
)

PLUGIN_REJECTIONS = (
    Rejection(
        "xdist-looponfail-plugin",
        ("-p", "xdist.looponfail"),
        "plugin 'xdist.looponfail'",
        test_source=PLUGIN_TEST,
        plugin_name="xdist.looponfail",
    ),
    Rejection(
        "hypothesis-plugin",
        ("-p", "hypothesispytest"),
        "plugin 'hypothesispytest'",
        test_source=PLUGIN_TEST,
        plugin_name="hypothesispytest",
    ),
)

HYPOTHESIS_LIFECYCLE_SOURCE_PATHS = pytest_execution_accounting.HYPOTHESIS_LIFECYCLE_SOURCE_PATHS
HYPOTHESIS_LIFECYCLE_SETUP_NODEID = pytest_execution_accounting.HYPOTHESIS_LIFECYCLE_SETUP_NODEID
HYPOTHESIS_LIFECYCLE_JUNIT_NODEIDS = (
    "testing/test_junitxml.py::test_random_report_log_xdist",
    "testing/test_junitxml.py::test_runs_twice_xdist",
)
HYPOTHESIS_LIFECYCLE_TERMINAL_PASS_NODEIDS = (
    "testing/test_terminal.py::TestTerminalFunctional::test_no_header_trailer_info",
)
HYPOTHESIS_LIFECYCLE_TARGET_NODEIDS = pytest_execution_record.CORPUS_SUBPROCESS_RUNPYTEST_NODEIDS
HYPOTHESIS_WARNING_TEXT = "between importing hypothesis and loading the hypothesis plugin"
HYPOTHESIS_TERMINAL_FAILURE_SIGNATURES = {
    "testing/test_terminal.py::TestTerminalFunctional::test_header_trailer_info": (
        "Failed: remains unmatched: '*===== test session starts ====*'"
    ),
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

REJECTION_PROOF_PREFIX = "validation/pytest_runner/check_native_rejections.py#"


def hypothesis_lifecycle_arguments(target_nodeid: str) -> list[str]:
    if target_nodeid not in HYPOTHESIS_LIFECYCLE_TARGET_NODEIDS:
        raise ValueError(f"unrecognized Hypothesis lifecycle node: {target_nodeid!r}")
    return [
        "-q",
        "-rP",
        "-p",
        "no:cacheprovider",
        HYPOTHESIS_LIFECYCLE_SETUP_NODEID,
        target_nodeid,
    ]


def hypothesis_lifecycle_expected_result(
    target_nodeid: str,
) -> tuple[int, list[str], str | None]:
    if target_nodeid in HYPOTHESIS_LIFECYCLE_JUNIT_NODEIDS:
        return 1, ["passed", "failed"], "FileNotFoundError: junit.xml"
    if target_nodeid in HYPOTHESIS_TERMINAL_FAILURE_SIGNATURES:
        return 1, ["passed", "failed"], HYPOTHESIS_TERMINAL_FAILURE_SIGNATURES[target_nodeid]
    if target_nodeid in HYPOTHESIS_LIFECYCLE_TERMINAL_PASS_NODEIDS:
        return 0, ["passed", "passed"], None
    raise ValueError(f"unrecognized Hypothesis lifecycle node: {target_nodeid!r}")


def hypothesis_lifecycle_summary_matches(stdout: str, expected_exit_status: int) -> bool:
    if expected_exit_status not in {0, 1}:
        raise ValueError(f"unexpected lifecycle proof exit status: {expected_exit_status}")
    summary_line = next(
        (line.strip() for line in reversed(stdout.splitlines()) if line.strip()),
        "",
    )
    summary_pattern = (
        r"1 failed, 1 passed in \d+(?:\.\d+)?s"
        if expected_exit_status == 1
        else r"2 passed in \d+(?:\.\d+)?s"
    )
    return re.fullmatch(summary_pattern, summary_line) is not None


def hypothesis_lifecycle_outer_failure_signature(stdout: str, target_nodeid: str) -> str | None:
    _exit_status, _outcomes, expected_signature = hypothesis_lifecycle_expected_result(
        target_nodeid
    )
    error_lines = [
        match.group(1).strip()
        for line in stdout.splitlines()
        if (match := re.fullmatch(r"\s*E\s+(.*)", line)) is not None
    ]
    if expected_signature is None:
        if any(line.startswith(("Failed:", "FileNotFoundError:")) for line in error_lines):
            raise RuntimeError("passing no-header case emitted an outer failure signature")
        return None

    matching_signatures: list[str] = []
    if expected_signature == "FileNotFoundError: junit.xml":
        error_prefix = "FileNotFoundError: [Errno 2] No such file or directory: "
        for line in error_lines:
            if not line.startswith(error_prefix):
                continue
            try:
                missing_path = ast.literal_eval(line.removeprefix(error_prefix))
            except (SyntaxError, ValueError):
                continue
            if isinstance(missing_path, str) and Path(missing_path).name == "junit.xml":
                matching_signatures.append("FileNotFoundError: junit.xml")
    else:
        matching_signatures = [line for line in error_lines if line == expected_signature]
    if len(matching_signatures) != 1:
        raise RuntimeError(
            f"expected one exact outer failure signature for {target_nodeid!r}; "
            f"found {matching_signatures!r} in E-lines {error_lines!r}"
        )
    return matching_signatures[0]


def fixture_sha256(project: Path, rejection: Rejection) -> str:
    source_names = (
        "test_contract.py",
        "conftest.py",
        "runtime_plugin.py",
        "configure_plugin.py",
        "collection_plugin.py",
        "runloop_replacement_plugin.py",
    )
    sources = [
        {"filename": name, "sha256": hashlib.sha256((project / name).read_bytes()).hexdigest()}
        for name in source_names
        if (project / name).is_file()
    ]
    fixture = {
        "sources": sources,
        "arguments": list(rejection.arguments),
        "use_cacheprovider": rejection.use_cacheprovider,
    }
    canonical = json.dumps(fixture, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(canonical).hexdigest()


def rejection_evidence(
    *,
    classification_path: Path,
    stock_python: Path,
    candidate_python: Path,
    runner_artifacts: list[Path],
    project: Path,
    checks: dict[str, dict[str, object]],
    plugin_observations: dict[str, dict[str, object]],
    hypothesis_lifecycle_proof: list[dict[str, object]] | None,
) -> dict[str, object]:
    classification = pytest_execution_accounting.load_object(classification_path)
    unsupported = [
        record
        for record in pytest_execution_accounting.classified_nodes(classification)
        if record["disposition"] == "deliberately-unsupported"
    ]
    if hypothesis_lifecycle_proof is None or len(hypothesis_lifecycle_proof) != len(
        HYPOTHESIS_LIFECYCLE_TARGET_NODEIDS
    ):
        raise ValueError(
            "the controlled pytester protocol requires all ordered Hypothesis lifecycle diagnostics"
        )
    stock_resolved = pytest_execution_record.resolved_environment(stock_python)
    resolved = pytest_execution_record.resolved_environment(candidate_python)
    pytest_execution_record.verify_installed_artifacts(
        stock_python,
        runner_artifacts,
        "pytest",
    )
    pytest_execution_record.verify_installed_artifacts(
        candidate_python,
        runner_artifacts,
        "vfy",
    )
    artifact_rows = pytest_execution_record.artifact_records(runner_artifacts, "runner artifact")
    checker_root = Path(__file__).resolve().parents[2]
    checker_artifacts = [
        {
            "filename": relative,
            "sha256": hashlib.sha256((checker_root / relative).read_bytes()).hexdigest(),
        }
        for relative in pytest_execution_accounting.REJECTION_CHECKER_FILES
    ]
    rejections = []
    for record in unsupported:
        proof = record.get("rejection_proof", "")
        if not proof.startswith(REJECTION_PROOF_PREFIX):
            raise ValueError(
                f"unsupported node {record['nodeid']} must name an executable rejection case"
            )
        proof_case = proof.removeprefix(REJECTION_PROOF_PREFIX)
        check = checks.get(proof_case)
        if check is None:
            raise ValueError(f"no executable rejection check exists for proof {proof!r}")
        rejections.append(
            {
                "nodeid": record["nodeid"],
                "proof": proof,
                "proof_case": proof_case,
                **check,
            }
        )
    resolved_hash = hashlib.sha256(
        json.dumps(resolved, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    stock_resolved_hash = hashlib.sha256(
        json.dumps(stock_resolved, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    plugin_proofs = [
        {
            "proof_case": proof_case,
            "plugin_entrypoint": pytest_execution_accounting.PLUGIN_REJECTION_CASES[proof_case],
            "arguments": observation["arguments"],
            "stock_exit_status": observation["stock_exit_status"],
            "stock_collection_started": observation["stock_collection_started"],
            "stock_test_body_ran": observation["stock_test_body_ran"],
            "stock_loaded_identity": observation["stock_loaded_identity"],
            "candidate_exit_status": observation["exit_status"],
            "diagnostic": observation["diagnostic"],
            "expected_diagnostic": observation["expected_diagnostic"],
            "fixture_sha256": observation["fixture_sha256"],
            "test_body_ran": observation["test_body_ran"],
            "candidate_collection_started": observation["candidate_collection_started"],
            "candidate_cache_unchanged": observation["candidate_cache_unchanged"],
            "candidate_cache_sha256_before": observation["candidate_cache_sha256_before"],
            "candidate_cache_sha256_after": observation["candidate_cache_sha256_after"],
            "stock_project_identity_sha256": observation["stock_project_identity_sha256"],
            "candidate_project_identity_sha256": observation["candidate_project_identity_sha256"],
        }
        for proof_case, observation in plugin_observations.items()
    ]
    return {
        "schema_version": 3,
        "provenance": {
            "upstream_commit": classification["inventory"]["upstream_commit"],
            "inventory_sha256": classification["inventory"]["sha256"],
            "policy_sha256": classification["policy"]["sha256"],
            "python": resolved["python"],
            "platform": resolved["platform"],
            "stock_runner_artifact_set_sha256": pytest_execution_record.artifact_set_sha256(
                artifact_rows
            ),
            "stock_resolved_environment_sha256": stock_resolved_hash,
            "runner_artifact_set_sha256": pytest_execution_record.artifact_set_sha256(
                artifact_rows
            ),
            "resolved_environment_sha256": resolved_hash,
            "project_identity_sha256": hashlib.sha256(str(project.resolve()).encode()).hexdigest(),
            "checker_artifacts": sorted(checker_artifacts, key=lambda row: row["filename"]),
        },
        "rejections": rejections,
        "plugin_proofs": plugin_proofs,
        "hypothesis_lifecycle_proof": hypothesis_lifecycle_proof,
    }


def run_case(
    python: Path,
    project: Path,
    event: Path,
    config_event: Path,
    *arguments: str,
    use_cacheprovider: bool = False,
    native: bool = True,
    plugin_name: str | None = None,
) -> tuple[int, str, str]:
    collection_event = event.with_name(f"{event.stem}-collection{event.suffix}")
    environment = benchmark_environment(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_NATIVE_REJECTION_EVENT": str(event),
            "PYTEST_NATIVE_REJECTION_CONFIG_EVENT": str(config_event),
            "PYTEST_NATIVE_REJECTION_COLLECTION_EVENT": str(collection_event),
            "PYTEST_NATIVE_REJECTION_PLUGIN_IDENTITY_EVENT": str(
                event.with_name(f"{event.stem}-plugin-identity{event.suffix}")
            ),
        }
    )
    if plugin_name is not None:
        environment["PYTEST_NATIVE_REJECTION_PLUGIN"] = plugin_name
    command = [
        str(python.absolute()),
        "-m",
        "pytest",
        "-q",
        "--color=no",
        "-c",
        os.devnull,
        "--rootdir",
        str(project),
    ]
    if native or not use_cacheprovider:
        command.extend(("-p", "no:cacheprovider"))
    if native:
        command.extend(("-p", "vfy._pytest.native_pytest"))
    command.extend((*arguments, "test_contract.py"))
    completed = run_captured(command, cwd=project, env=environment, timeout=120)
    return completed.returncode, completed.stdout, completed.stderr


def cache_files(project: Path) -> dict[str, bytes]:
    cache = project / ".pytest_cache"
    return {
        path.relative_to(cache).as_posix(): path.read_bytes()
        for path in cache.rglob("*")
        if path.is_file()
    }


def cache_snapshot_sha256(snapshot: dict[str, bytes]) -> str:
    payload = json.dumps(
        {name: hashlib.sha256(contents).hexdigest() for name, contents in sorted(snapshot.items())},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def check_plugin_rejection(
    stock_python: Path,
    candidate_python: Path,
    destination: Path,
    rejection: Rejection,
) -> tuple[dict[str, object], dict[str, object]]:
    """Prove a named external plugin runs on stock and rejects before native collection."""
    assert rejection.plugin_name is not None
    stock_project = destination / "stock"
    candidate_project = destination / "candidate"
    stock_project.mkdir(parents=True, exist_ok=True)
    candidate_project.mkdir(parents=True, exist_ok=True)
    for project in (stock_project, candidate_project):
        (project / "test_contract.py").write_text(rejection.test_source, encoding="utf-8")

    stock_event = destination / "stock-test-ran.txt"
    stock_collection_event = stock_event.with_name("stock-test-ran-collection.txt")
    stock_identity_event = stock_event.with_name("stock-test-ran-plugin-identity.txt")
    stock_config_event = destination / "stock-configured.txt"
    for marker in (stock_event, stock_collection_event, stock_config_event, stock_identity_event):
        marker.unlink(missing_ok=True)
    status, stdout, stderr = run_case(
        stock_python,
        stock_project,
        stock_event,
        stock_config_event,
        *rejection.arguments,
        native=False,
        plugin_name=rejection.plugin_name,
    )
    if (
        status != 0
        or not stock_event.is_file()
        or stock_event.read_text(encoding="utf-8") != "ran\n"
        or not stock_collection_event.is_file()
        or stock_collection_event.read_text(encoding="utf-8") != "collected\n"
    ):
        raise RuntimeError(
            f"stock {rejection.name} case did not collect and pass: {status}\n{stdout}\n{stderr}"
        )
    stock_identity = json.loads(stock_identity_event.read_text(encoding="utf-8"))
    if (
        not isinstance(stock_identity, dict)
        or set(stock_identity) != {"name", "distribution", "version"}
        or not all(isinstance(value, str) and value for value in stock_identity.values())
    ):
        raise RuntimeError(f"stock {rejection.name} case has invalid plug-in identity")

    candidate_event = destination / "candidate-test-ran.txt"
    candidate_collection_event = candidate_event.with_name("candidate-test-ran-collection.txt")
    candidate_identity_event = candidate_event.with_name("candidate-test-ran-plugin-identity.txt")
    candidate_config_event = destination / "candidate-configured.txt"
    for marker in (
        candidate_event,
        candidate_collection_event,
        candidate_config_event,
        candidate_identity_event,
    ):
        marker.unlink(missing_ok=True)
    sentinel = candidate_project / ".pytest_cache" / "v" / "cache" / "rejection-sentinel"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(b'{"before": "native plugin rejection"}\n')
    before = cache_files(candidate_project)
    status, stdout, stderr = run_case(
        candidate_python,
        candidate_project,
        candidate_event,
        candidate_config_event,
        *rejection.arguments,
        native=True,
        plugin_name=rejection.plugin_name,
    )
    diagnostic = stdout + stderr
    after = cache_files(candidate_project)
    if (
        status != 4
        or "native pytest does not execute hooks from plugin" not in diagnostic
        or rejection.diagnostic not in diagnostic
        or candidate_event.exists()
        or candidate_collection_event.exists()
        or candidate_config_event.exists()
        or candidate_identity_event.exists()
        or before != after
    ):
        raise RuntimeError(
            f"{rejection.name}: expected named usage rejection before collection without cache "
            f"mutation, got {status}\n{stdout}\n{stderr}\n"
            f"collection={candidate_collection_event.exists()} body={candidate_event.exists()} "
            f"cache_before={cache_snapshot_sha256(before)} "
            f"cache_after={cache_snapshot_sha256(after)}"
        )

    proof = {
        "exit_status": status,
        "diagnostic": diagnostic,
        "expected_diagnostic": rejection.diagnostic,
        "arguments": list(rejection.arguments),
        "fixture_sha256": fixture_sha256(candidate_project, rejection),
        "test_body_ran": candidate_event.exists(),
    }
    observation = {
        **proof,
        "stock_exit_status": 0,
        "stock_plugin_loaded": rejection.plugin_name,
        "stock_collection_started": True,
        "stock_test_body_ran": True,
        "stock_loaded_identity": stock_identity,
        "candidate_collection_started": candidate_collection_event.exists(),
        "candidate_cache_unchanged": before == after,
        "candidate_cache_sha256_before": cache_snapshot_sha256(before),
        "candidate_cache_sha256_after": cache_snapshot_sha256(after),
        "stock_project_identity_sha256": hashlib.sha256(
            str(stock_project.resolve()).encode()
        ).hexdigest(),
        "candidate_project_identity_sha256": hashlib.sha256(
            str(candidate_project.resolve()).encode()
        ).hexdigest(),
    }
    return proof, observation


def pytest11_entrypoint_names(python: Path, cwd: Path) -> list[str]:
    command = (
        "import json; from importlib.metadata import entry_points; "
        "print(json.dumps(sorted({entry.name for entry in entry_points(group='pytest11')})))"
    )
    result = run_captured(
        [str(python.absolute()), "-c", command],
        cwd=cwd,
        env=benchmark_environment({"PYTHONDONTWRITEBYTECODE": "1"}),
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"could not inspect pytest plugin entry points: {result.stderr}")
    value = json.loads(result.stdout)
    if (
        not isinstance(value, list)
        or any(not isinstance(name, str) or not name for name in value)
        or "hypothesispytest" not in value
    ):
        raise RuntimeError(f"isolated Hypothesis probe requires its pytest entry point: {value!r}")
    return sorted(set(value))


def disabled_plugin_arguments(plugin_names: Iterable[str], *, allowed: frozenset[str]) -> list[str]:
    """Disable every entry-point plugin except the one under lifecycle test."""
    return [
        argument
        for name in sorted(set(plugin_names) - allowed)
        for argument in ("-p", f"no:{name}")
    ]


def check_hypothesis_autoload_lifecycle(stock_python: Path, destination: Path) -> dict[str, object]:
    """Isolate Hypothesis's nested late-load warning from unrelated plug-ins."""
    destination.mkdir(parents=True, exist_ok=True)
    names = pytest11_entrypoint_names(stock_python, destination)
    blocked_names = sorted(set(names) - {"hypothesispytest"})
    blocked_arguments = disabled_plugin_arguments(names, allowed=frozenset({"hypothesispytest"}))
    observations: dict[str, object] = {"pytest11_entrypoints": names}
    for mode, enable_outer_autoload, expected_nested_exit in (
        ("late", False, 3),
        ("early", True, 0),
    ):
        project = destination / f"hypothesis-autoload-{mode}"
        project.mkdir(parents=True, exist_ok=True)
        test = project / "test_contract.py"
        test.write_text(HYPOTHESIS_LIFECYCLE_TEST, encoding="utf-8")
        result_path = project / "nested-result.json"
        environment = benchmark_environment(
            {
                "HYPOTHESIS_STORAGE_DIRECTORY": str(project / "hypothesis-storage"),
                "PYTEST_NATIVE_HYPOTHESIS_PROBE_RESULT": str(result_path),
                "PYTEST_NATIVE_HYPOTHESIS_EXPECTED_EXIT": str(expected_nested_exit),
                "PYTEST_NATIVE_HYPOTHESIS_BLOCKED_PLUGINS": json.dumps(blocked_names),
                "PYTEST_NATIVE_HYPOTHESIS_EXPECT_OUTER_PLUGIN": "1"
                if enable_outer_autoload
                else "0",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        if enable_outer_autoload:
            environment.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
        command = [
            str(stock_python.absolute()),
            "-m",
            "pytest",
            "-q",
            "--color=no",
            "-c",
            os.devnull,
            "--rootdir",
            str(project),
            "-p",
            "no:cacheprovider",
            "-p",
            "pytester",
            *blocked_arguments,
            str(test),
        ]
        completed = run_captured(command, cwd=project, env=environment, timeout=90)
        if completed.returncode != 0 or not result_path.is_file():
            raise RuntimeError(
                f"Hypothesis {mode}-autoload control failed: {completed.returncode}\n"
                f"{completed.stdout}\n{completed.stderr}"
            )
        nested = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(nested, dict) or nested.get("exit_status") != expected_nested_exit:
            raise RuntimeError(f"Hypothesis {mode}-autoload result is malformed: {nested!r}")
        nested_stderr = nested.get("stderr")
        warning_text = "between importing hypothesis and loading the hypothesis plugin"
        if mode == "late":
            if (
                not isinstance(nested_stderr, str)
                or "HypothesisSideeffectWarning" not in nested_stderr
                or warning_text not in nested_stderr
            ):
                raise RuntimeError(
                    "late nested autoload did not fail specifically on the Hypothesis warning: "
                    f"{nested_stderr!r}"
                )
        elif nested_stderr or "HypothesisSideeffectWarning" in str(nested_stderr):
            raise RuntimeError(
                f"eager Hypothesis plugin load did not clear the nested warning: {nested!r}"
            )
        observations[mode] = {
            "outer_autoload_enabled": enable_outer_autoload,
            "nested_exit_status": expected_nested_exit,
            "nested_stderr": nested_stderr,
            "hypothesis_storage_isolated": str(project / "hypothesis-storage"),
            "other_pytest11_entrypoints_blocked": blocked_names,
            "test_fixture_sha256": hashlib.sha256(test.read_bytes()).hexdigest(),
        }
    return observations


def check_hypothesis_late_autoload_corpus_case(
    stock_python: Path,
    upstream_root: Path,
    classification: dict[str, Any],
    destination: Path,
    target_nodeid: str,
) -> dict[str, object]:
    """Prove one pinned corpus case's ordered Hypothesis plugin-autoload effect."""
    destination.mkdir(parents=True, exist_ok=True)
    pytest_arguments = hypothesis_lifecycle_arguments(target_nodeid)
    expected_exit_status, expected_outcomes, expected_outer_failure_signature = (
        hypothesis_lifecycle_expected_result(target_nodeid)
    )
    upstream_commit = pytest_execution_record.validate_upstream(upstream_root, classification)
    source_files = [
        {
            "path": relative,
            "sha256": hashlib.sha256((upstream_root / relative).read_bytes()).hexdigest(),
        }
        for relative in HYPOTHESIS_LIFECYCLE_SOURCE_PATHS
    ]
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="hypothesis-late-autoload-", dir=destination
    ) as temporary:
        project = Path(temporary) / "upstream"
        shutil.copytree(
            upstream_root,
            project,
            ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__"),
        )
        environment = benchmark_environment(
            {
                "PYTHONHASHSEED": "0",
                "PYTHONNOUSERSITE": "1",
                "PYTHONSAFEPATH": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            }
        )
        command = [
            str(stock_python.absolute()),
            "-P",
            "-m",
            "pytest",
            *pytest_arguments,
        ]
        completed = run_captured(command, cwd=project, env=environment, timeout=120)
        pytest_execution_accounting.write_atomic(
            destination / "proof-run.json",
            json.dumps(
                {
                    "argv": command,
                    "exit_status": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
    output = completed.stdout + completed.stderr
    warning_text = HYPOTHESIS_WARNING_TEXT
    warning_visible = "HypothesisSideeffectWarning" in output and warning_text in output
    outer_failure_signature = hypothesis_lifecycle_outer_failure_signature(
        completed.stdout, target_nodeid
    )
    if (
        completed.returncode != expected_exit_status
        or not hypothesis_lifecycle_summary_matches(completed.stdout, expected_exit_status)
        or not warning_visible
        or outer_failure_signature != expected_outer_failure_signature
    ):
        raise RuntimeError(
            "the ordered pinned Hypothesis late-autoload case did not reproduce its upstream "
            f"failure: exit={completed.returncode}\n{completed.stdout}\n{completed.stderr}"
        )
    return {
        "proof_case": "hypothesis-late-autoload-corpus-case",
        "upstream_commit": upstream_commit,
        "pytest_arguments": pytest_arguments,
        "source_files": source_files,
        "outer_exit_status": completed.returncode,
        "outer_test_outcomes": expected_outcomes,
        "outer_failure_signature": outer_failure_signature,
        "nested_warning_type": "HypothesisSideeffectWarning",
        "nested_warning_text": warning_text,
    }


def check_hypothesis_late_autoload_corpus_cases(
    stock_python: Path,
    upstream_root: Path,
    classification: dict[str, Any],
    destination: Path,
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    proofs = [
        check_hypothesis_late_autoload_corpus_case(
            stock_python,
            upstream_root,
            classification,
            destination / f"{index:02d}-{target_nodeid.replace('/', '-').replace('::', '-')}",
            target_nodeid,
        )
        for index, target_nodeid in enumerate(HYPOTHESIS_LIFECYCLE_TARGET_NODEIDS, start=1)
    ]
    observations = {
        f"hypothesis-late-autoload:{target_nodeid}": proof
        for target_nodeid, proof in zip(HYPOTHESIS_LIFECYCLE_TARGET_NODEIDS, proofs, strict=True)
    }
    return proofs, observations


def check_native_cacheprovider(
    stock_python: Path, candidate_python: Path, destination: Path
) -> dict[str, object]:
    """Check the native cache default and explicit rejection of pytest's provider."""
    version = run_captured(
        [str(stock_python.absolute()), "-c", "import pytest; print(pytest.__version__)"],
        cwd=destination,
        env=benchmark_environment({"PYTHONDONTWRITEBYTECODE": "1"}),
        timeout=30,
    )
    if version.returncode != 0 or version.stdout.strip() != "9.1.1":
        raise RuntimeError(
            f"cache differential requires stock pytest 9.1.1, got "
            f"{version.stdout.strip()!r}: {version.stderr}"
        )

    stock_project = destination / "cache-stock-project"
    candidate_project = destination / "cache-candidate-project"
    stock_project.mkdir(exist_ok=True)
    candidate_project.mkdir(exist_ok=True)
    for project in (stock_project, candidate_project):
        (project / "test_contract.py").write_text(TEST, encoding="utf-8")

    stock_event = destination / "cache-stock-event.txt"
    stock_event.unlink(missing_ok=True)
    status, stdout, stderr = run_case(
        stock_python,
        stock_project,
        stock_event,
        destination / "cache-stock-config-event.txt",
        use_cacheprovider=True,
        native=False,
    )
    if status != 0 or not stock_event.is_file():
        raise RuntimeError(f"stock cacheprovider case failed: {status}\n{stdout}\n{stderr}")

    candidate_event = destination / "cache-candidate-event.txt"
    candidate_event.unlink(missing_ok=True)
    status, stdout, stderr = run_case(
        candidate_python,
        candidate_project,
        candidate_event,
        destination / "cache-candidate-config-event.txt",
        use_cacheprovider=True,
    )
    if status != 0 or not candidate_event.is_file():
        raise RuntimeError(f"candidate native cache default failed: {status}\n{stdout}\n{stderr}")
    cache_root = candidate_project / ".pytest_cache"
    expected_support_files = {"README.md", ".gitignore", "CACHEDIR.TAG"}
    if not expected_support_files.issubset({path.name for path in cache_root.iterdir()}):
        raise RuntimeError("candidate native cache did not initialize pytest's cache directory")

    sentinels = {"sentinel": '{"prior": true}\n'}
    cache = candidate_project / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    for name, value in sentinels.items():
        (cache / name).write_text(value, encoding="utf-8")
    before = cache_files(candidate_project)
    candidate_event.unlink()
    status, stdout, stderr = run_case(
        candidate_python,
        candidate_project,
        candidate_event,
        destination / "cache-candidate-config-event.txt",
        "-p",
        "cacheprovider",
        use_cacheprovider=True,
    )
    diagnostic = stdout + stderr
    after = cache_files(candidate_project)
    if (
        status != 4
        or "Rust cache service" not in diagnostic
        or candidate_event.exists()
        or before != after
    ):
        raise RuntimeError(
            "candidate did not reject an explicitly loaded stock cacheprovider before execution "
            f"without changing cache state: {status}\n{stdout}\n{stderr}\n"
            f"before={sorted(before)} after={sorted(after)}"
        )
    return {
        "stock_pytest": "9.1.1",
        "stock_exit_status": 0,
        "candidate_native_cache_exit_status": 0,
        "candidate_explicit_stock_provider_exit_status": 4,
        "candidate_cache_unchanged_after_stock_provider_rejection": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-python", required=True, type=Path)
    parser.add_argument(
        "--stock-python",
        type=Path,
        help="pinned stock pytest 9.1.1 executable (defaults to the sibling stock venv)",
    )
    parser.add_argument("--classification", type=Path)
    parser.add_argument("--upstream-root", type=Path)
    parser.add_argument("--pytest-artifact", action="append", type=Path, default=[])
    parser.add_argument("--runner-artifact", action="append", type=Path, default=[])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    project = destination / "project"
    project.mkdir(exist_ok=True)
    test = project / "test_contract.py"
    conftest = project / "conftest.py"
    conftest.unlink(missing_ok=True)
    (project / "runtime_plugin.py").write_text(RUNTIME_PLUGIN, encoding="utf-8")
    (project / "configure_plugin.py").write_text(CONFIGURE_PLUGIN, encoding="utf-8")
    (project / "collection_plugin.py").write_text(COLLECTION_PLUGIN, encoding="utf-8")
    (project / "runloop_replacement_plugin.py").write_text(
        RUNLOOP_REPLACEMENT_PLUGIN, encoding="utf-8"
    )
    event = destination / "test-ran.txt"
    config_event = destination / "configured.txt"
    observations: dict[str, dict[str, object]] = {}
    executable_checks: dict[str, dict[str, object]] = {}
    plugin_observations: dict[str, dict[str, object]] = {}
    hypothesis_lifecycle_proofs: list[dict[str, object]] | None = None

    stock_python = args.stock_python
    if stock_python is None:
        stock_python = args.candidate_python.resolve().parents[2] / "stock" / "bin" / "python"
    if not stock_python.is_file():
        raise RuntimeError(
            "the pinned stock pytest executable is required; pass --stock-python or "
            "place it beside the candidate venv"
        )

    test.write_text(TEST, encoding="utf-8")
    event.unlink(missing_ok=True)
    status, stdout, stderr = run_case(args.candidate_python, project, event, config_event)
    if status != 0 or not event.is_file() or event.read_text(encoding="utf-8") != "ran\n":
        raise RuntimeError(f"ordinary native execution failed: {status}\n{stdout}\n{stderr}")
    observations["ordinary"] = {"exit_status": status}

    conftest.write_text(CONFIGURE_HOOK, encoding="utf-8")
    event.unlink(missing_ok=True)
    config_event.unlink(missing_ok=True)
    status, stdout, stderr = run_case(args.candidate_python, project, event, config_event)
    if (
        status != 0
        or event.read_text(encoding="utf-8") != "ran\n"
        or config_event.read_text(encoding="utf-8") != "configured\n"
    ):
        raise RuntimeError(
            f"supported configure hook did not run once: {status}\n{stdout}\n{stderr}"
        )
    observations["configure_hook"] = {"exit_status": status, "executed_once": True}
    conftest.unlink()

    test.write_text(FAILURE_TEST, encoding="utf-8")
    event.unlink(missing_ok=True)
    status, stdout, stderr = run_case(args.candidate_python, project, event, config_event)
    if (
        status != 1
        or not event.is_file()
        or event.read_text(encoding="utf-8") != "ran\n"
        or "native failure detail" not in stdout + stderr
    ):
        raise RuntimeError(f"native failure detail was not rendered: {status}\n{stdout}\n{stderr}")
    observations["failure_detail"] = {"exit_status": status}

    for rejection in REJECTIONS:
        test.write_text(rejection.test_source, encoding="utf-8")
        if rejection.conftest_source is None:
            conftest.unlink(missing_ok=True)
        else:
            conftest.write_text(rejection.conftest_source, encoding="utf-8")
        event.unlink(missing_ok=True)
        config_event.unlink(missing_ok=True)
        status, stdout, stderr = run_case(
            args.candidate_python,
            project,
            event,
            config_event,
            *rejection.arguments,
            use_cacheprovider=rejection.use_cacheprovider,
        )
        diagnostic = stdout + stderr
        if (
            status != 4
            or "native pytest" not in diagnostic
            or rejection.diagnostic not in diagnostic
            or event.exists()
            or config_event.exists()
        ):
            raise RuntimeError(
                f"{rejection.name}: expected usage exit 4 and no test execution, "
                f"got {status}\n{stdout}\n{stderr}"
            )
        check = {
            "exit_status": status,
            "diagnostic": diagnostic,
            "expected_diagnostic": rejection.diagnostic,
            "fixture_sha256": fixture_sha256(project, rejection),
            "test_body_ran": event.exists(),
        }
        executable_checks[rejection.name] = check
        observations[rejection.name] = {
            "exit_status": status,
            "expected_diagnostic": rejection.diagnostic,
            "test_body_ran": event.exists(),
        }

    for rejection in PLUGIN_REJECTIONS:
        proof, observation = check_plugin_rejection(
            stock_python,
            args.candidate_python,
            destination / rejection.name,
            rejection,
        )
        executable_checks[rejection.name] = proof
        observations[rejection.name] = observation
        plugin_observations[rejection.name] = observation

    observations["hypothesis-autoload-lifecycle"] = check_hypothesis_autoload_lifecycle(
        stock_python,
        destination / "hypothesis-autoload-lifecycle",
    )

    classification: dict[str, Any] | None = None
    if args.classification is not None:
        classification = pytest_execution_accounting.load_object(args.classification.resolve())
    if args.upstream_root is not None:
        if classification is None:
            raise ValueError("the ordered Hypothesis proof requires the pinned classification")
        hypothesis_lifecycle_proofs, lifecycle_observations = (
            check_hypothesis_late_autoload_corpus_cases(
                stock_python,
                args.upstream_root.resolve(),
                classification,
                destination / "hypothesis-late-autoload-corpus-case",
            )
        )
        observations.update(lifecycle_observations)

    observations["native_cacheprovider"] = check_native_cacheprovider(
        stock_python, args.candidate_python, destination
    )

    report = destination / "rejections.json"
    if args.classification is not None:
        runner_artifacts = [*args.pytest_artifact, *args.runner_artifact]
        if not args.pytest_artifact or not args.runner_artifact:
            raise ValueError(
                "provenance-bound rejection evidence requires pytest and vfy artifacts"
            )
        evidence = rejection_evidence(
            classification_path=args.classification.resolve(),
            stock_python=pytest_execution_record.resolve_executable(stock_python),
            candidate_python=pytest_execution_record.resolve_executable(args.candidate_python),
            runner_artifacts=[artifact.resolve() for artifact in runner_artifacts],
            project=project,
            checks=executable_checks,
            plugin_observations=plugin_observations,
            hypothesis_lifecycle_proof=hypothesis_lifecycle_proofs,
        )
        pytest_execution_accounting.write_atomic(
            report,
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        )
        pytest_execution_accounting.write_atomic(
            destination / "rejection-checks.json",
            json.dumps(observations, indent=2, sort_keys=True) + "\n",
        )
    else:
        pytest_execution_accounting.write_atomic(
            report,
            json.dumps(observations, indent=2, sort_keys=True) + "\n",
        )
    sys.stdout.write(f"{report}\n")


if __name__ == "__main__":
    main()
