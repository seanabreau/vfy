"""Measure the user-facing baseline -> edit -> affected-test workflow.

The benchmark creates a small Git project, records a passing vfy baseline,
applies a behavior-preserving source edit, and compares the selected vfy
run with pytest-testmon and an independent full pytest-cov run. Timing starts
after the fixture and baselines are prepared. Exact executed-test parity and
Coverage.py data parity are checked before any samples are published.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from scripts.process_tree import run_captured

from benchmarks.benchmark_harness import (
    BenchmarkMode,
    SamplingConfig,
    add_sampling_arguments,
    benchmark_environment,
    run_checked,
    summarize,
    timed_process,
)
from benchmarks.benchmark_projects import normalized_data
from benchmarks.benchmark_provenance import build_provenance
from benchmarks.benchmark_resources import tree_resource_methodology, tree_resource_process
from benchmarks.benchmark_workflows import clear_measured_caches
from vfy.native import build_profile

EDIT_LOOP_CACHE_STATE = (
    "affected/unchanged comparisons share project bytecode and pytest caches after "
    "per-repetition full baselines, with rotating/alternating tool order; cold controls "
    "use separate pristine copies without history or project caches; default vfy uses "
    "its own full baseline then edit, with the oracle afterward; only the vfy-only "
    "seven-phase sequence clears project bytecode/pytest caches before the oracle and "
    "again before each timed vfy phase, retaining history within each sequence; "
    "installed dependency and OS caches remain warm"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules", type=int, default=40)
    parser.add_argument(
        "--workload",
        choices=("independent", "shared", "fixtures", "parametrized"),
        default="independent",
    )
    add_sampling_arguments(parser)
    parser.add_argument("--diagnostic", dest="mode", action="store_const", const="diagnostic")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--profile-dir",
        type=Path,
        help="write diagnostic controller and pytest-child phase records; disables timing",
    )
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--tree-resources",
        action="store_true",
        help="sample Linux live process-tree memory and CPU for measured commands",
    )
    parser.add_argument(
        "--profile-workflow", choices=("affected", "cold", "unchanged"), default="affected"
    )
    return parser.parse_args()


def run(
    command: list[str], root: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return run_checked(command, cwd=root, env=environment, timeout=300)


def git(root: Path, *arguments: str) -> None:
    run(["git", *arguments], root, benchmark_environment())


def build_evidence(repository: Path) -> dict[str, object]:
    return build_provenance(repository, workload="controlled-edit-loop")


def test_count(modules: int, workload: str) -> int:
    return modules * 2 if workload == "parametrized" else modules


def generated_nodeids(modules: int, workload: str) -> list[str]:
    nodeids = []
    for index in range(modules):
        module = f"module_{index:03d}"
        base = f"tests/test_{module}.py::test_{module}"
        if workload == "parametrized":
            nodeids.extend((f"{base}[one]", f"{base}[zero]"))
        else:
            nodeids.append(base)
    return sorted(nodeids)


def create_project(root: Path, modules: int, workload: str = "independent") -> None:
    source = root / "src"
    tests = root / "tests"
    source.mkdir()
    tests.mkdir()
    (root / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    (root / ".gitignore").write_text(
        ".vfy/\n.testmondata*\n.coverage*\n*.coverage\n*-tests.txt\n*.json\n"
        "__pycache__/\n.pytest_cache/\n",
        encoding="utf-8",
    )
    (root / "conftest.py").write_text(
        "import json\nimport os\n"
        "from pathlib import Path\n\n"
        "if os.environ.get('VFY_NATIVE_COVERAGE_BRIDGE') != '1':\n"
        "    def pytest_runtest_logreport(report):\n"
        "        if hasattr(report, 'worker_id'):\n"
        "            return\n"
        "        output = os.environ.get('VFY_BENCHMARK_EXECUTED')\n"
        "        if output:\n"
        "            with Path(output).open('a', encoding='utf-8') as stream:\n"
        "                value = [report.nodeid, report.when, report.outcome]\n"
        "                stream.write(json.dumps(value) + '\\n')\n",
        encoding="utf-8",
    )
    (source / "__init__.py").write_text("", encoding="utf-8")
    if workload == "shared":
        for index in range(0, modules, 2):
            (source / f"shared_{index // 2:03d}.py").write_text(
                "def normalize(value):\n    return value\n", encoding="utf-8"
            )
    for index in range(modules):
        module = f"module_{index:03d}"
        shared_import = ""
        value_expression = "1"
        if workload == "shared":
            shared = f"shared_{index // 2:03d}"
            shared_import = f"from .{shared} import normalize\n\n"
            if index % 2 == 0:
                value_expression = "normalize(1)"
        if workload == "parametrized":
            function_source = (
                f"def value_one():\n    return 1 + {index} - {index}\n\n"
                "def value_zero():\n    return 0\n"
            )
        else:
            function_source = (
                f"{shared_import}def value(flag=True):\n"
                f"    if flag:\n        return {value_expression}\n"
                "    return 0\n"
            )
        (source / f"{module}.py").write_text(
            function_source,
            encoding="utf-8",
        )
        if workload == "fixtures":
            test_source = (
                "import pytest\n\n"
                f"from src.{module} import value\n\n"
                "@pytest.fixture\n"
                f"def {module}_raw():\n    return value()\n\n"
                "@pytest.fixture\n"
                f"def {module}_prepared({module}_raw):\n    return ({module}_raw, 1)\n\n"
                f"def test_{module}({module}_prepared):\n"
                f"    actual, expected = {module}_prepared\n"
                "    assert actual == expected\n"
            )
        elif workload == "parametrized":
            test_source = (
                "import pytest\n\n"
                f"from src.{module} import value_one, value_zero\n\n"
                "@pytest.mark.parametrize(\n"
                "    'function, expected',\n"
                "    [(value_one, 1), (value_zero, 0)],\n"
                "    ids=['one', 'zero'],\n"
                ")\n"
                f"def test_{module}(function, expected):\n"
                "    assert function() == expected\n"
            )
        else:
            test_source = (
                f"from src.{module} import value\n\ndef test_{module}():\n    assert value() == 1\n"
            )
        (tests / f"test_{module}.py").write_text(test_source, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.email", "vfy-benchmark@example.invalid")
    git(root, "config", "user.name", "vfy Benchmark")
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")


def command_vfy(
    root: Path,
    report: str,
    *,
    all_tests: bool = False,
    data_file: str = ".coverage",
    workers: str | int | None = 1,
) -> list[str]:
    selection = ["test", "--engine=pytest-native"]
    if all_tests:
        selection.append("--all")
    if workers is not None:
        selection.extend(("-j", str(workers)))
    return [
        sys.executable,
        "-m",
        "vfy",
        *selection,
        "--source",
        "src",
        "--history",
        ".vfy/test-history.json",
        "--data-file",
        data_file,
        "-q",
        "--cov=src",
        "--cov-branch",
        "--cov-context=test",
        f"--cov-report=json:{report}",
    ]


def command_full(root: Path, report: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "pytest_cov.plugin",
        "-p",
        "xdist.plugin",
        "-n",
        "0",
        "--cov=src",
        "--cov-branch",
        "--cov-context=test",
        f"--cov-report=json:{report}",
    ]


def command_testmon() -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "testmon.pytest_testmon",
        "--testmon",
    ]


def edit_project(root: Path, workload: str = "independent", *, breaking: bool = False) -> None:
    replacement = "2" if breaking else "int('1')"
    if workload == "parametrized":
        target = root / "src" / "module_000.py"
        target.write_text(
            f"def value_one():\n    return {replacement}\n\ndef value_zero():\n    return 0\n",
            encoding="utf-8",
        )
        return
    if workload == "shared":
        target = root / "src" / "module_000.py"
        target.write_text(
            "from .shared_000 import normalize\n\n"
            f"def value(flag=True):\n    if flag:\n        return normalize({replacement})\n"
            "    return 0\n",
            encoding="utf-8",
        )
        return
    target = root / "src" / "module_000.py"
    target.write_text(
        f"def value(flag=True):\n    if flag:\n        return {replacement}\n    return 0\n",
        encoding="utf-8",
    )


def prepare_sample(prepared: Path, sample_root: Path, base_environment: dict[str, str]) -> None:
    shutil.copytree(prepared, sample_root)
    run(
        command_vfy(sample_root, "selected.json", all_tests=True),
        sample_root,
        base_environment,
    )
    run(command_testmon(), sample_root, base_environment)


def run_default_command(
    prepared: Path,
    sample_root: Path,
    base_environment: dict[str, str],
    *,
    tests: int,
    workload: str,
    timed_run: bool,
    resources: ResourceSamples | None = None,
) -> tuple[float, list[str]]:
    shutil.copytree(prepared, sample_root)
    run(
        command_vfy(sample_root, "default.json", all_tests=True, workers=None),
        sample_root,
        base_environment,
    )
    edit_project(sample_root, workload)
    output = sample_root / "default-tests.txt"
    environment = execution_environment(base_environment, output)
    command = command_vfy(sample_root, "default.json", workers=None)
    if timed_run:
        elapsed, stderr = timed(command, sample_root, environment, resources, "default-vfy")
    else:
        completed = run(command, sample_root, environment)
        elapsed, stderr = 0.0, completed.stderr
    assert_useful_selection(stderr, tests)
    nodeids = executed_tests(output)
    if not nodeids:
        raise RuntimeError("default vfy command did not execute an affected test")
    oracle_environment = execution_environment(base_environment, sample_root / "full-tests.txt")
    oracle_environment["COVERAGE_FILE"] = str(sample_root / "full.coverage")
    run(command_full(sample_root, "full.json"), sample_root, oracle_environment)
    assert_parity(
        sample_root / "default.json",
        sample_root / "full.json",
        sample_root / ".coverage",
        sample_root / "full.coverage",
        sample_root,
    )
    return elapsed, nodeids


def run_profile(
    prepared: Path,
    sample_root: Path,
    base_environment: dict[str, str],
    profile_dir: Path,
    *,
    tests: int,
    workload: str,
    workflow: str,
) -> list[str]:
    shutil.copytree(prepared, sample_root)
    if workflow != "cold":
        run(
            command_vfy(sample_root, "profile.json", all_tests=True),
            sample_root,
            base_environment,
        )
    if workflow == "affected":
        edit_project(sample_root, workload)
    output = sample_root / "profile-tests.txt"
    environment = execution_environment(base_environment, output)
    environment["VFY_PROFILE_DIR"] = str(profile_dir)
    environment["VFY_PROFILE_CONTROLLER_SPAWN_NS"] = str(time.perf_counter_ns())
    completed = run(
        command_vfy(sample_root, "profile.json", all_tests=workflow == "cold"),
        sample_root,
        environment,
    )
    if workflow == "affected":
        assert_useful_selection(completed.stderr, tests)
    if workflow == "unchanged":
        return []
    return executed_tests(output)


def run_unchanged_pair(
    prepared: Path,
    sample_root: Path,
    base_environment: dict[str, str],
    *,
    order: int,
    timed_run: bool,
    resources: ResourceSamples | None = None,
) -> tuple[float, float]:
    prepare_sample(prepared, sample_root, base_environment)
    commands = {
        "vfy": command_vfy(sample_root, "selected.json"),
        "testmon": command_testmon(),
    }
    elapsed = {"vfy": 0.0, "testmon": 0.0}
    for name in (("vfy", "testmon"), ("testmon", "vfy"))[order % 2]:
        output = sample_root / f"unchanged-{name}-tests.txt"
        environment = execution_environment(base_environment, output)
        if timed_run:
            elapsed[name], stderr = timed(
                commands[name], sample_root, environment, resources, f"unchanged-{name}"
            )
        else:
            completed = run(commands[name], sample_root, environment)
            stderr = completed.stderr
        if output.exists():
            raise RuntimeError(f"unchanged {name} run unexpectedly executed tests")
        if name == "vfy" and "No code changed" not in stderr:
            raise RuntimeError("unchanged vfy run did not restore its passing baseline")
    if not (sample_root / "selected.json").is_file():
        raise RuntimeError("unchanged vfy run did not write its requested report")
    return elapsed["vfy"], elapsed["testmon"]


def run_cold_baselines(
    pristine: Path,
    sample_root: Path,
    base_environment: dict[str, str],
    *,
    order: int,
    timed_run: bool,
    resources: ResourceSamples | None = None,
) -> tuple[float, float, float, list[str]]:
    vfy_root = sample_root / "vfy"
    testmon_root = sample_root / "testmon"
    plain_root = sample_root / "plain"
    shutil.copytree(pristine, vfy_root)
    shutil.copytree(pristine, testmon_root)
    shutil.copytree(pristine, plain_root)
    calls = {
        "vfy": (
            command_vfy(vfy_root, "cold.json", all_tests=True),
            vfy_root,
            execution_environment(base_environment, vfy_root / "cold-tests.txt"),
        ),
        "testmon": (
            command_testmon(),
            testmon_root,
            execution_environment(base_environment, testmon_root / "cold-tests.txt"),
        ),
        "plain": (
            [sys.executable, "-m", "pytest", "-q"],
            plain_root,
            execution_environment(base_environment, plain_root / "cold-tests.txt"),
        ),
    }
    elapsed = dict.fromkeys(calls, 0.0)
    names = list(calls)
    offset = order % len(names)
    for name in names[offset:] + names[:offset]:
        command, root, environment = calls[name]
        if timed_run:
            elapsed[name], _stderr = timed(command, root, environment, resources, f"cold-{name}")
        else:
            run(command, root, environment)
    vfy_tests = executed_tests(vfy_root / "cold-tests.txt")
    testmon_tests = executed_tests(testmon_root / "cold-tests.txt")
    plain_tests = executed_tests(plain_root / "cold-tests.txt")
    if not vfy_tests == testmon_tests == plain_tests:
        raise RuntimeError("cold baselines executed different tests")
    return elapsed["vfy"], elapsed["testmon"], elapsed["plain"], vfy_tests


def normalized_report(path: Path, root: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    files = payload.get("files")
    if not isinstance(files, dict):
        raise RuntimeError(f"report has no files object: {path}")
    normalized: dict[str, object] = {}
    for filename, value in files.items():
        if not isinstance(filename, str) or not isinstance(value, dict):
            raise RuntimeError(f"invalid report entry: {filename!r}")
        candidate = Path(filename)
        if candidate.is_absolute():
            try:
                relative = candidate.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                relative = filename.replace("\\", "/")
        else:
            relative = candidate.as_posix()
        relative = relative.removeprefix("src/")
        normalized[relative] = {
            field: value.get(field)
            for field in (
                "summary",
                "missing_lines",
                "executed_lines",
                "excluded_lines",
                "executed_branches",
                "missing_branches",
            )
        }
    return normalized


def assert_parity(
    selected: Path, full: Path, selected_data: Path, full_data: Path, root: Path
) -> None:
    actual = normalized_report(selected, root)
    expected = normalized_report(full, root)
    if not actual or not expected:
        raise RuntimeError("coverage reports must contain measured files")
    if actual != expected:
        differing = [
            filename
            for filename in sorted(set(actual) | set(expected))
            if actual.get(filename) != expected.get(filename)
        ]
        raise RuntimeError(
            "selected vfy coverage differs from the independent full run: "
            f"{differing[:5]} actual={actual.get(differing[0])!r} "
            f"expected={expected.get(differing[0])!r}"
        )
    if normalized_data(selected_data, root) != normalized_data(full_data, root):
        raise RuntimeError("selected vfy data differs from the independent full run")


@dataclass
class ResourceSamples:
    """Retain resource evidence for each measured workflow command."""

    directory: Path
    records: list[dict[str, object]] = field(default_factory=list)

    def measure(
        self, command: list[str], root: Path, env: dict[str, str], workflow: str
    ) -> tuple[float, subprocess.CompletedProcess[str]]:
        record_path = self.directory / f"sample-{len(self.records):04d}.json"
        elapsed, completed, record = tree_resource_process(
            command, cwd=root, env=env, record_path=record_path, timeout=300
        )
        self.records.append({"workflow": workflow, "record": record, "file": record_path.name})
        return elapsed, completed


def measured_command(
    command: list[str],
    root: Path,
    env: dict[str, str],
    resources: ResourceSamples | None,
    workflow: str,
) -> tuple[float, subprocess.CompletedProcess[str]]:
    if resources is not None:
        return resources.measure(command, root, env, workflow)
    return timed_process(command, cwd=root, env=env, timeout=300)


def timed(
    command: list[str],
    root: Path,
    env: dict[str, str],
    resources: ResourceSamples | None = None,
    workflow: str = "affected",
) -> tuple[float, str]:
    elapsed, completed = measured_command(command, root, env, resources, workflow)
    if completed.returncode:
        raise RuntimeError(f"benchmark command failed ({completed.returncode}): {completed.stderr}")
    return elapsed, completed.stderr


def run_unchecked(
    command: list[str], root: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return run_captured(command, cwd=root, env=environment, timeout=300)


def selection_diagnostics(stderr: str) -> list[str]:
    return [
        line
        for line in stderr.splitlines()
        if line.startswith(
            ("Changed code:", "Affected code:", "Selected ", "Running the full suite", "Note:")
        )
    ]


def _outcome_records(contents: str, path: Path) -> list[object]:
    """Project the native record or stock hook log into the common parity input."""
    records: list[object]
    if contents.lstrip().startswith("{"):
        record = json.loads(contents)
        if (
            not isinstance(record, dict)
            or record.get("engine") != "pytest-runner-native-v1"
            or not isinstance(record.get("cases"), list)
        ):
            raise RuntimeError(f"invalid native outcome report: {path}")
        records = []
        native_nodeids: set[str] = set()
        for case in record["cases"]:
            if (
                not isinstance(case, dict)
                or not isinstance(case.get("nodeid"), str)
                or not case["nodeid"]
                or not isinstance(case.get("phases"), list)
                or not case["phases"]
            ):
                raise RuntimeError(f"invalid native test case in {path}: {case!r}")
            if case["nodeid"] in native_nodeids:
                raise RuntimeError(f"duplicate native test case in {path}: {case['nodeid']}")
            native_nodeids.add(case["nodeid"])
            for phase in case["phases"]:
                if not isinstance(phase, dict):
                    raise RuntimeError(f"invalid native test phase in {path}: {phase!r}")
                records.append([case.get("nodeid"), phase.get("phase"), phase.get("outcome")])
    else:
        records = [json.loads(line) for line in contents.splitlines()]
    return records


def executed_outcomes(path: Path) -> dict[str, str]:
    """Require setup, call, and teardown outcomes for every executed test."""
    if not path.is_file():
        raise RuntimeError(f"pytest outcome report is missing: {path}")
    records = _outcome_records(path.read_text(encoding="utf-8"), path)
    tests: dict[str, dict[str, str]] = {}
    for value in records:
        if (
            not isinstance(value, list)
            or len(value) != 3
            or not all(isinstance(item, str) for item in value)
        ):
            raise RuntimeError(f"invalid pytest outcome in {path}: {value!r}")
        nodeid, phase, outcome = value
        if (
            not nodeid
            or phase not in {"setup", "call", "teardown"}
            or outcome not in {"passed", "failed", "skipped"}
        ):
            raise RuntimeError(f"invalid pytest outcome in {path}: {value!r}")
        phases = tests.setdefault(nodeid, {})
        if phase in phases:
            raise RuntimeError(f"duplicate pytest phase in {path}: {value!r}")
        phases[phase] = outcome
    if not tests:
        raise RuntimeError(f"pytest outcome report has no tests: {path}")
    outcomes = {}
    for nodeid, phases in sorted(tests.items()):
        if not {"setup", "teardown"} <= phases.keys() or (
            (phases["setup"] == "passed") != ("call" in phases)
        ):
            raise RuntimeError(f"incomplete pytest protocol for {nodeid}: {phases!r}")
        if phases["setup"] == "failed" or phases["teardown"] != "passed":
            raise RuntimeError(f"pytest fixture failure cannot validate {nodeid}: {phases!r}")
        outcomes[nodeid] = (
            "failed"
            if "failed" in phases.values()
            else "skipped"
            if "skipped" in phases.values()
            else "passed"
        )
    return outcomes


def executed_tests(path: Path) -> list[str]:
    outcomes = executed_outcomes(path)
    if set(outcomes.values()) != {"passed"}:
        raise RuntimeError(f"benchmark tests did not all pass: {outcomes!r}")
    return sorted(outcomes)


def assert_test_failure(completed: subprocess.CompletedProcess[str], report: Path) -> None:
    outcomes = executed_outcomes(report)
    if completed.returncode != 1 or "failed" not in outcomes.values():
        raise RuntimeError(
            f"breaking edit did not produce a pytest test failure: "
            f"exit={completed.returncode}, outcomes={outcomes!r}"
        )


def execution_environment(base_environment: dict[str, str], output: Path) -> dict[str, str]:
    values = base_environment.copy()
    values["VFY_BENCHMARK_EXECUTED"] = str(output)
    values["PYTEST_NATIVE_RESULT"] = str(output)
    return values


def assert_useful_selection(stderr: str, tests: int) -> None:
    match = re.search(r"Selected (\d+) / (\d+) tests", stderr)
    if match is None:
        raise RuntimeError(f"selection did not report a selected subset: {stderr}")
    selected, total = (int(value) for value in match.groups())
    if total != tests or not 0 < selected < total:
        raise RuntimeError(f"selection did not reduce the generated suite: {selected} / {total}")


def run_pair(
    sample_root: Path,
    base_environment: dict[str, str],
    *,
    tests: int,
    order: int,
    timed_run: bool,
    exact_selection: bool = True,
    resources: ResourceSamples | None = None,
) -> tuple[float, float, float, str, list[str]]:
    selected_command = command_vfy(sample_root, "selected.json")
    testmon_command = command_testmon()
    full_command = command_full(sample_root, "full.json")
    vfy_tests = sample_root / "vfy-tests.txt"
    testmon_tests = sample_root / "testmon-tests.txt"
    full_tests = sample_root / "full-tests.txt"
    selected_environment = execution_environment(base_environment, vfy_tests)
    testmon_environment = execution_environment(base_environment, testmon_tests)
    full_environment = execution_environment(base_environment, full_tests)
    full_environment["COVERAGE_FILE"] = str(sample_root / "full.coverage")
    if timed_run:
        calls = {
            "selected": (selected_command, selected_environment),
            "testmon": (testmon_command, testmon_environment),
            "full": (full_command, full_environment),
        }
        elapsed: dict[str, float] = {}
        diagnostics = ""
        orders = (
            ("selected", "testmon", "full"),
            ("testmon", "full", "selected"),
            ("full", "selected", "testmon"),
        )
        names = orders[order % len(orders)]
        for name in names:
            command, environment = calls[name]
            elapsed[name], stderr = timed(
                command, sample_root, environment, resources, f"affected-{name}"
            )
            if name == "selected":
                diagnostics = stderr
    else:
        selected_result = run(selected_command, sample_root, selected_environment)
        run(testmon_command, sample_root, testmon_environment)
        run(full_command, sample_root, full_environment)
        elapsed = {"selected": 0.0, "testmon": 0.0, "full": 0.0}
        diagnostics = selected_result.stderr
    assert_useful_selection(diagnostics, tests)
    vfy_nodeids = executed_tests(vfy_tests)
    testmon_nodeids = executed_tests(testmon_tests)
    selections_match = vfy_nodeids == testmon_nodeids
    conservative_selection = set(testmon_nodeids) < set(vfy_nodeids)
    if (
        not vfy_nodeids
        or (exact_selection and not selections_match)
        or (not exact_selection and not (selections_match or conservative_selection))
    ):
        raise RuntimeError(
            "vfy and pytest-testmon selected different tests: "
            f"vfy={vfy_nodeids!r} testmon={testmon_nodeids!r}"
        )
    if not set(vfy_nodeids) < set(executed_tests(full_tests)):
        raise RuntimeError("affected-test runs did not execute a strict subset of the full suite")
    assert_parity(
        sample_root / "selected.json",
        sample_root / "full.json",
        sample_root / ".coverage",
        sample_root / "full.coverage",
        sample_root,
    )
    return (
        elapsed["selected"],
        elapsed["testmon"],
        elapsed["full"],
        diagnostics,
        vfy_nodeids,
    )


class SequenceRecord(TypedDict):
    phases: list[dict[str, object]]
    total_ms: float
    validation: str


def run_developer_sequence(
    pristine: Path,
    root: Path,
    base_environment: dict[str, str],
    *,
    modules: int,
    measure: bool,
    resources: ResourceSamples | None = None,
) -> SequenceRecord:
    """Keep one checkout and its history through edits, failure, and repair."""
    shutil.copytree(pristine, root)
    phases = ("baseline", "unchanged", "local-edit", "shared-edit", "breaking", "repair", "full")
    records: list[dict[str, object]] = []
    total = 0.0
    for phase in phases:
        if phase in {"local-edit", "repair", "breaking"}:
            edit_project(root, "shared", breaking=phase == "breaking")
        elif phase == "shared-edit":
            (root / "src/shared_000.py").write_text(
                "def normalize(value):\n    return value + 0\n", encoding="utf-8"
            )
        # Reset caches before measuring; reference validation is untimed.
        clear_measured_caches(root)
        oracle_outcomes = root / f"{phase}-oracle-tests.txt"
        oracle_environment = execution_environment(base_environment, oracle_outcomes)
        oracle_environment["COVERAGE_FILE"] = str(root / "oracle.coverage")
        oracle_command = command_full(root, "oracle.json")
        if phase == "breaking":
            # A failed selected run reports only that execution; successful runs
            # reconstruct coverage using the passing baseline.
            oracle_command.extend(generated_nodeids(modules, "shared")[:1])
        oracle = run_unchecked(oracle_command, root, oracle_environment)
        expected = executed_outcomes(oracle_outcomes)
        if len(expected) != (1 if phase == "breaking" else modules) or oracle.returncode != (
            1 if phase == "breaking" else 0
        ):
            raise RuntimeError(f"{phase}: independent full-suite oracle failed")
        # Reference validation warms bytecode and pytest caches. Clear them
        # before timing to preserve the declared cache state.
        clear_measured_caches(root)
        outcomes_path = root / f"{phase}-vfy-tests.txt"
        env = execution_environment(base_environment, outcomes_path)
        command = command_vfy(root, "sequence.json", all_tests=phase in {"baseline", "full"})
        if phase != "unchanged":
            (root / "sequence.json").unlink(missing_ok=True)
        if measure:
            elapsed, completed = measured_command(
                command, root, env, resources, f"sequence-{phase}"
            )
        else:
            elapsed, completed = 0.0, run_unchecked(command, root, env)
        (root.parent / f"{root.name}-{phase}.log").write_text(
            completed.stdout + completed.stderr, encoding="utf-8"
        )
        if completed.returncode != oracle.returncode:
            raise RuntimeError(f"{phase}: vfy and oracle exit statuses differ")
        if phase == "unchanged":
            if outcomes_path.exists() and outcomes_path.read_text(encoding="utf-8").strip():
                raise RuntimeError("unchanged command unexpectedly executed tests")
            actual: dict[str, str] = {}
        else:
            actual = executed_outcomes(outcomes_path)
            if actual != {nodeid: expected[nodeid] for nodeid in actual if nodeid in expected}:
                raise RuntimeError(f"{phase}: selected test outcomes differ from the oracle")
            selected = set(actual)
            expected_nodes = generated_nodeids(modules, "shared")
            # Only module_000 calls the edited helper; module_001 imports it unused.
            required = set(expected_nodes) if phase in {"baseline", "full"} else {expected_nodes[0]}
            if selected != required:
                raise RuntimeError(f"{phase}: selection differs: {selected!r} != {required!r}")
            if phase == "breaking":
                assert_test_failure(completed, outcomes_path)
        assert_parity(
            root / "sequence.json",
            root / "oracle.json",
            root / ".coverage",
            root / "oracle.coverage",
            root,
        )
        total += elapsed
        records.append({"phase": phase, "elapsed_ms": elapsed, "outcomes": actual})
    return {"phases": records, "total_ms": total, "validation": "passed"}


@contextmanager
def benchmark_workspace(artifacts: Path | None) -> Iterator[str]:
    if artifacts is not None and artifacts.exists():
        raise RuntimeError(f"benchmark artifact directory already exists: {artifacts}")
    # Keep generated repositories outside the controller's ignored build tree.
    with tempfile.TemporaryDirectory(prefix="vfy-edit-loop-") as temporary:
        try:
            yield temporary
        finally:
            if artifacts is not None:
                artifacts.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(temporary, artifacts)


def main() -> int:  # noqa: C901 - one linear benchmark protocol preserves sample ordering
    args = parse_args()
    if args.modules < 2:
        raise SystemExit("--modules must be at least 2")
    profiling = args.profile_dir is not None
    if args.tree_resources and sys.platform != "linux":
        raise SystemExit("--tree-resources requires Linux")
    if args.tree_resources and (profiling or args.validate_only):
        raise SystemExit("--tree-resources requires measured samples without profiling")
    if profiling and (args.output is not None or args.validate_only):
        raise SystemExit("--profile-dir cannot be combined with --output or --validate-only")
    if not profiling and args.profile_workflow != "affected":
        raise SystemExit("--profile-workflow requires --profile-dir")
    sampling = SamplingConfig(
        args.warmups,
        args.runs,
        BenchmarkMode.DIAGNOSTIC if args.validate_only or profiling else BenchmarkMode(args.mode),
    )
    if not args.validate_only and not profiling and args.output is None:
        raise SystemExit("published edit-loop benchmarks require --output")
    repository = Path(__file__).resolve().parents[1]
    if build_profile() != "release":
        raise SystemExit("edit-loop benchmarks require a release-mode vfy build")
    base_environment = benchmark_environment()
    initial_build = build_provenance(
        repository,
        workload={"name": "controlled-edit-loop", "modules": args.modules, "kind": args.workload},
        workers=1,
        cache=EDIT_LOOP_CACHE_STATE,
        packages=("pytest", "pytest-cov", "pytest-testmon", "coverage"),
        publication=sampling.publishable,
    )
    expected_test_count = test_count(args.modules, args.workload)
    exact_selection = args.workload != "parametrized"

    artifact_root = args.output.with_suffix(".artifacts") if args.output is not None else None
    with benchmark_workspace(artifact_root) as temporary:
        resources = ResourceSamples(Path(temporary) / "resources") if args.tree_resources else None
        pristine = Path(temporary) / "pristine"
        pristine.mkdir()
        create_project(pristine, args.modules, args.workload)
        prepared = Path(temporary) / "prepared"
        shutil.copytree(pristine, prepared)
        run(
            command_vfy(prepared, "selected.json", all_tests=True),
            prepared,
            base_environment,
        )

        sample_root = Path(temporary) / "sample"
        prepare_sample(prepared, sample_root, base_environment)
        edit_project(sample_root, args.workload)
        _selected_elapsed, _testmon_elapsed, _full_elapsed, diagnostics, selected_tests = run_pair(
            sample_root,
            base_environment,
            tests=expected_test_count,
            order=0,
            timed_run=False,
            exact_selection=exact_selection,
        )
        default_root = Path(temporary) / "default"
        _default_elapsed, default_tests = run_default_command(
            prepared,
            default_root,
            base_environment,
            tests=expected_test_count,
            workload=args.workload,
            timed_run=False,
        )
        if default_tests != selected_tests:
            raise RuntimeError(
                "default and serial vfy commands selected different tests: "
                f"default={default_tests!r} serial={selected_tests!r}"
            )

        failure_root = Path(temporary) / "failure"
        prepare_sample(prepared, failure_root, base_environment)
        edit_project(failure_root, args.workload, breaking=True)
        failure = run_unchecked(
            command_vfy(failure_root, "selected.json"),
            failure_root,
            execution_environment(base_environment, failure_root / "vfy-tests.txt"),
        )
        assert_test_failure(failure, failure_root / "vfy-tests.txt")
        testmon_failure = run_unchecked(
            command_testmon(),
            failure_root,
            execution_environment(base_environment, failure_root / "testmon-tests.txt"),
        )
        assert_test_failure(testmon_failure, failure_root / "testmon-tests.txt")
        failure_outcomes = {
            tool: executed_outcomes(failure_root / f"{tool}-tests.txt")
            for tool in ("vfy", "testmon")
        }
        failure_selection_matches = failure_outcomes["vfy"] == failure_outcomes["testmon"]
        conservative_failure = set(failure_outcomes["testmon"]) < set(failure_outcomes["vfy"])
        if (
            sorted(failure_outcomes["vfy"]) != selected_tests
            or (exact_selection and not failure_selection_matches)
            or (not exact_selection and not (failure_selection_matches or conservative_failure))
        ):
            raise RuntimeError(
                "breaking edit changed the expected selected test identities: "
                f"expected={selected_tests!r}, actual={failure_outcomes!r}"
            )
        sequence_pristine = Path(temporary) / "sequence-pristine"
        sequence_pristine.mkdir()
        create_project(sequence_pristine, args.modules, "shared")
        sequence_validation = run_developer_sequence(
            sequence_pristine,
            Path(temporary) / "sequence-validation",
            base_environment,
            modules=args.modules,
            measure=False,
        )
        if args.validate_only:
            print(
                json.dumps(
                    {
                        "workflow": "edit-loop",
                        "parity": "exact",
                        "selection": "subset",
                        "selection_vs_testmon": (
                            "exact" if exact_selection else "vfy_conservative"
                        ),
                        "failure_detection": "passed",
                        "developer_sequence": sequence_validation,
                    }
                )
            )
            return 0
        if args.profile_dir is not None:
            profile_dir = args.profile_dir.resolve()
            profile_dir.mkdir(parents=True, exist_ok=True)
            if any(profile_dir.iterdir()):
                raise RuntimeError(f"profile directory is not empty: {profile_dir}")
            profile_root = Path(temporary) / "profile"
            profiled_tests = run_profile(
                pristine,
                profile_root,
                base_environment,
                profile_dir,
                tests=expected_test_count,
                workload=args.workload,
                workflow=args.profile_workflow,
            )
            expected_profile_tests = selected_tests
            if args.profile_workflow == "cold":
                expected_profile_tests = generated_nodeids(args.modules, args.workload)
            elif args.profile_workflow == "unchanged":
                expected_profile_tests = []
            if profiled_tests != expected_profile_tests:
                raise RuntimeError("profile run changed the validated test identities")
            manifest = {
                "schema": 1,
                "publishable": False,
                "reason": "profiling changes process execution and cannot be timed",
                "execution_model": "controller and pytest share one command process",
                "modules": args.modules,
                "tests": expected_test_count,
                "workload": args.workload,
                "profile_workflow": args.profile_workflow,
                "environment": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    **build_evidence(repository),
                },
                "command": command_vfy(
                    profile_root, "profile.json", all_tests=args.profile_workflow == "cold"
                ),
                "selected_tests": profiled_tests,
            }
            (profile_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps({"profile_dir": str(profile_dir), "publishable": False}))
            return 0

        sequence_records = []
        selected_samples = []
        default_samples = []
        testmon_samples = []
        full_samples = []
        unchanged_vfy_samples = []
        unchanged_testmon_samples = []
        cold_vfy_samples = []
        cold_testmon_samples = []
        cold_plain_samples = []
        selected_counts = [selection_diagnostics(diagnostics)]
        expected_tests = selected_tests
        for _ in range(args.warmups - 1):
            shutil.rmtree(sample_root)
            prepare_sample(prepared, sample_root, base_environment)
            edit_project(sample_root, args.workload)
            run_pair(
                sample_root,
                base_environment,
                tests=expected_test_count,
                order=0,
                timed_run=False,
                exact_selection=exact_selection,
            )
            shutil.rmtree(default_root)
            run_default_command(
                prepared,
                default_root,
                base_environment,
                tests=expected_test_count,
                workload=args.workload,
                timed_run=False,
            )
        unchanged_root = Path(temporary) / "unchanged"
        cold_root = Path(temporary) / "cold"
        cold_tests: list[str] = []
        for warmup in range(args.warmups):
            if unchanged_root.exists():
                shutil.rmtree(unchanged_root)
            run_unchanged_pair(
                prepared,
                unchanged_root,
                base_environment,
                order=warmup,
                timed_run=False,
            )
            if cold_root.exists():
                shutil.rmtree(cold_root)
            _cold_vfy, _cold_testmon, _cold_plain, cold_tests = run_cold_baselines(
                pristine,
                cold_root,
                base_environment,
                order=warmup,
                timed_run=False,
            )
            if len(cold_tests) != expected_test_count:
                raise RuntimeError("cold baseline did not execute the complete generated suite")
        for warmup in range(args.warmups):
            run_developer_sequence(
                sequence_pristine,
                Path(temporary) / f"sequence-warmup-{warmup}",
                base_environment,
                modules=args.modules,
                measure=False,
            )
        for _index in range(args.runs):
            sequence_records.append(
                run_developer_sequence(
                    sequence_pristine,
                    Path(temporary) / f"sequence-sample-{_index}",
                    base_environment,
                    modules=args.modules,
                    measure=True,
                    resources=resources,
                )
            )
            shutil.rmtree(sample_root)
            prepare_sample(prepared, sample_root, base_environment)
            edit_project(sample_root, args.workload)
            selected_time, testmon_time, full_time, diagnostics, selected_tests = run_pair(
                sample_root,
                base_environment,
                tests=expected_test_count,
                order=_index,
                timed_run=True,
                resources=resources,
                exact_selection=exact_selection,
            )
            if selected_tests != expected_tests:
                raise RuntimeError("sample changed the validated selected test identities")
            selected_samples.append(selected_time)
            testmon_samples.append(testmon_time)
            full_samples.append(full_time)
            selected_counts.append(selection_diagnostics(diagnostics))
            shutil.rmtree(default_root)
            default_time, default_tests = run_default_command(
                prepared,
                default_root,
                base_environment,
                tests=expected_test_count,
                workload=args.workload,
                timed_run=True,
                resources=resources,
            )
            if default_tests != selected_tests:
                raise RuntimeError("default and serial vfy selection diverged")
            default_samples.append(default_time)
            if unchanged_root.exists():
                shutil.rmtree(unchanged_root)
            unchanged_vfy, unchanged_testmon = run_unchanged_pair(
                prepared,
                unchanged_root,
                base_environment,
                order=_index,
                timed_run=True,
                resources=resources,
            )
            unchanged_vfy_samples.append(unchanged_vfy)
            unchanged_testmon_samples.append(unchanged_testmon)
            if cold_root.exists():
                shutil.rmtree(cold_root)
            cold_vfy, cold_testmon, cold_plain, cold_tests = run_cold_baselines(
                pristine,
                cold_root,
                base_environment,
                order=_index,
                timed_run=True,
                resources=resources,
            )
            if len(cold_tests) != expected_test_count:
                raise RuntimeError("cold baseline sample did not execute every generated test")
            cold_vfy_samples.append(cold_vfy)
            cold_testmon_samples.append(cold_testmon)
            cold_plain_samples.append(cold_plain)

        final_build = build_provenance(
            repository,
            workload={
                "name": "controlled-edit-loop",
                "modules": args.modules,
                "kind": args.workload,
            },
            workers=1,
            cache=EDIT_LOOP_CACHE_STATE,
            packages=("pytest", "pytest-cov", "pytest-testmon", "coverage"),
            publication=sampling.publishable,
        )
        if initial_build != final_build:
            raise RuntimeError("benchmark source or installed artifact changed during sampling")
        publishable = sampling.publishable
        result = {
            "schema": 7,
            "benchmark": "edit-loop",
            "mode": sampling.mode.value,
            "methodology": {
                **sampling.as_dict(),
                "validation": (
                    (
                        "exact selected pytest node IDs versus pytest-testmon; "
                        if exact_selection
                        else "vfy may select a conservative superset of pytest-testmon's "
                        "pytest node IDs; shared selected outcomes match; "
                    )
                    + "exact normalized "
                    "Coverage.py JSON, SQLite lines/arcs, and contexts versus a full oracle "
                    "before timing samples; both affected-test runners also had to fail "
                    "after a behavior-breaking change"
                ),
                "cache_state": EDIT_LOOP_CACHE_STATE,
                "oracle": "independent full pytest-cov runs are outside timed vfy phases",
                "timed": (
                    "actual child command wall time; gates and evidence parsing are outside "
                    "timers; "
                    "resource runs include sampler overhead when --tree-resources is enabled"
                ),
                "sequence": (
                    "baseline -> unchanged cache -> local edit -> shared edit -> breaking "
                    "edit -> repair -> full verification"
                ),
            },
            "provenance": final_build,
            "raw_artifacts": str(artifact_root),
            "resources": (
                {**tree_resource_methodology(), "samples": resources.records}
                if resources is not None
                else None
            ),
            "publishable": publishable,
            "build_before": initial_build,
            "workflow": "baseline -> behavior-preserving edit -> affected tests",
            "modules": args.modules,
            "tests": expected_test_count,
            "workload": args.workload,
            "warmups": args.warmups,
            "runs": args.runs,
            "environment": {
                "python": platform.python_version(),
                "implementation": platform.python_implementation(),
                "platform": platform.platform(),
                "machine": platform.machine(),
                "vfy": importlib.metadata.version("vfy"),
                **final_build,
                "pytest": importlib.metadata.version("pytest"),
                "pytest_cov": importlib.metadata.version("pytest-cov"),
                "pytest_testmon": importlib.metadata.version("pytest-testmon"),
                "coverage": importlib.metadata.version("coverage"),
            },
            "commands": {
                "selected": command_vfy(sample_root, "selected.json"),
                "vfy_default": command_vfy(sample_root, "default.json", workers=None),
                "testmon": command_testmon(),
                "full_oracle": command_full(sample_root, "full.json"),
                "unchanged_vfy": command_vfy(unchanged_root, "selected.json"),
                "unchanged_testmon": command_testmon(),
                "cold_vfy": command_vfy(cold_root / "vfy", "cold.json", all_tests=True),
                "cold_testmon": command_testmon(),
                "cold_plain": [sys.executable, "-m", "pytest", "-q"],
            },
            "selected_tests": selected_tests,
            "selection_vs_testmon": "exact" if exact_selection else "vfy_conservative",
            "outcomes": {
                "selected": executed_outcomes(sample_root / "vfy-tests.txt"),
                "testmon": executed_outcomes(sample_root / "testmon-tests.txt"),
                "full_oracle": executed_outcomes(sample_root / "full-tests.txt"),
                "vfy_default": executed_outcomes(default_root / "default-tests.txt"),
                "breaking": failure_outcomes,
            },
            "selection_diagnostics": selected_counts,
            "developer_sequence": {
                "samples": sequence_records,
                **dict(summarize([float(record["total_ms"]) for record in sequence_records])),
            },
            "selected": summarize(selected_samples),
            "vfy_default": summarize(default_samples),
            "testmon": summarize(testmon_samples),
            "full_oracle": summarize(full_samples),
            "controls": {
                "unchanged": {
                    "executed_tests": [],
                    "vfy": summarize(unchanged_vfy_samples),
                    "testmon": summarize(unchanged_testmon_samples),
                },
                "cold_baseline": {
                    "executed_tests": cold_tests,
                    "vfy": summarize(cold_vfy_samples),
                    "testmon": summarize(cold_testmon_samples),
                    "plain_pytest": summarize(cold_plain_samples),
                },
            },
            "speedup_vs_testmon": statistics.median(testmon_samples)
            / statistics.median(selected_samples),
            "speedup_vs_full_oracle": statistics.median(full_samples)
            / statistics.median(selected_samples),
        }
        assert args.output is not None
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "workflow": result["workflow"],
                    "mode": sampling.mode.value,
                    "publishable": publishable,
                    "speedup_vs_testmon": result["speedup_vs_testmon"],
                    "speedup_vs_full_oracle": result["speedup_vs_full_oracle"],
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
