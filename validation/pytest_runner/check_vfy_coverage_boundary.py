"""Differentially check the installed vfy/pytest coverage lifecycle."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coverage import CoverageData
from tests.support import VfyProject

SOURCE = """def setup_marker():
    return "setup"


def teardown_marker():
    return "teardown"


def measured(enabled):
    value = 40 + 2
    if enabled:
        return value
    return value + 1
"""

SUCCESS_TESTS = """import os
from pathlib import Path

import pytest

from src.sample import measured


@pytest.fixture
def source_lifecycle():
    from src.sample import setup_marker, teardown_marker

    assert setup_marker() == "setup"
    yield
    assert teardown_marker() == "teardown"


def test_measured_source(source_lifecycle):
    with Path(os.environ["VFY_NATIVE_BOUNDARY_EVENT"]).open("a", encoding="utf-8") as event:
        event.write("measured\\n")
    assert measured(True) == 42


@pytest.mark.no_cover
def test_no_cover_marker():
    with Path(os.environ["VFY_NATIVE_BOUNDARY_EVENT"]).open("a", encoding="utf-8") as event:
        event.write("marker\\n")
    assert measured(False) == 43


def test_no_cover_fixture(no_cover):
    with Path(os.environ["VFY_NATIVE_BOUNDARY_EVENT"]).open("a", encoding="utf-8") as event:
        event.write("fixture\\n")
    assert measured(False) == 43
"""

NO_COVER_EXCEPTION_TESTS = """from src.sample import measured


def test_no_cover_raises(no_cover):
    assert measured(False) == 43
    raise ValueError("expected no_cover failure")


def test_coverage_resumes_after_no_cover_failure():
    assert measured(True) == 42
"""

RESULT_SENTINEL = b"preserve-native-result\n"
HISTORY_SENTINEL = b'{"tests":{"sentinel":{"duration_ms":1.0}}}\n'
PHASE_CONFTST = '''"""Record pytest's public phase reports for the vfy differential."""

import json
import os
from pathlib import Path


def pytest_runtest_logreport(report):
    outcome = report.outcome
    if hasattr(report, "wasxfail"):
        outcome = "xfailed" if report.skipped else "xpassed"
    elif report.failed and report.when != "call":
        outcome = "error"
    record = {
        "nodeid": report.nodeid,
        "phase": report.when,
        "outcome": outcome,
        "stdout": "".join(
            text for name, text in report.sections
            if name == f"Captured stdout {report.when}"
        ),
        "stderr": "".join(
            text for name, text in report.sections
            if name == f"Captured stderr {report.when}"
        ),
    }
    with Path(os.environ["VFY_PHASE_LOG"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\\n")
'''


def command(
    project: VfyProject,
    python: Path,
    arguments: tuple[str, ...],
    *,
    event: Path,
    native_result: Path,
    native_bridge: bool = False,
    phase_log: Path | None = None,
) -> tuple[int, str, str]:
    # Differential sides must select their route independently of product defaults.
    if "test" in arguments and not any(arg.startswith("--engine=") for arg in arguments):
        position = arguments.index("test") + 1
        engine = "pytest-native" if native_bridge else "pytest"
        arguments = (*arguments[:position], f"--engine={engine}", *arguments[position:])
    result = project.run(
        str(python.absolute()),
        "-m",
        "vfy",
        *arguments,
        env={
            # VfyProject adds a source-tree path only when its own interpreter
            # has no installed vfy package. The child must import its wheel.
            "PYTHONPATH": None,
            "VFY_NATIVE_BOUNDARY_EVENT": str(event),
            "PYTEST_NATIVE_RESULT": str(native_result),
            "VFY_PHASE_LOG": str(phase_log) if phase_log is not None else None,
        },
    )
    return result.returncode, result.stdout, result.stderr


def run_test(
    project: VfyProject,
    python: Path,
    event: Path,
    native_result: Path,
    *,
    tests: str = SUCCESS_TESTS,
    workers: str = "1",
    native_bridge: bool = False,
    engine: str | None = None,
    data_file: str = ".coverage",
    extra_arguments: tuple[str, ...] = (),
    phase_log: Path | None = None,
) -> tuple[int, str, str]:
    project.write("src/__init__.py", "")
    project.write("src/sample.py", SOURCE)
    project.write("test_contract.py", tests)
    if phase_log is not None:
        project.write("conftest.py", PHASE_CONFTST)
    return command(
        project,
        python,
        (
            "--isolated",
            "test",
            *((f"--engine={engine}",) if engine is not None else ()),
            f"--workers={workers}",
            "--no-replay",
            "--history",
            ".vfy/test-history.json",
            "--data-file",
            data_file,
            "--source",
            "src",
            "--cov-branch",
            "--cov-context=test",
            *extra_arguments,
            "test_contract.py",
        ),
        event=event,
        native_result=native_result,
        native_bridge=native_bridge,
        phase_log=phase_log,
    )


def normalized_coverage(path: Path, root: Path) -> dict[str, object]:
    data = CoverageData(basename=str(path))
    try:
        data.read()
        files: dict[str, object] = {}
        for filename in sorted(data.measured_files()):
            absolute = Path(filename)
            if not absolute.is_absolute():
                absolute = root / absolute
            try:
                relative = absolute.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                relative = absolute.as_posix()
            by_line = data.contexts_by_lineno(filename)
            files[relative] = {
                "lines": sorted(data.lines(filename) or []),
                "arcs": sorted(data.arcs(filename) or []),
                "contexts_by_line": {
                    str(line): sorted(normalize_context(context, root) for context in contexts)
                    for line, contexts in sorted(by_line.items())
                },
            }
        return {
            "has_arcs": data.has_arcs(),
            "measured_contexts": sorted(
                normalize_context(context, root) for context in data.measured_contexts()
            ),
            "files": files,
        }
    finally:
        data.close()


def result_tree(root: Path) -> dict[str, bytes | None]:
    """Snapshot files and directory topology without Python bytecode caches."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
        if "__pycache__" not in path.parts
    }


def expected_phase_records(
    expected_outcomes: dict[str, str],
    phase_outcomes: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for nodeid, call_outcome in expected_outcomes.items():
        outcomes = {
            "setup": "passed",
            "call": call_outcome,
            "teardown": "passed",
        }
        outcomes.update((phase_outcomes or {}).get(nodeid, {}))
        records.extend(
            {"nodeid": nodeid, "phase": phase, "outcome": outcomes[phase]}
            for phase in ("setup", "call", "teardown")
        )
    return records


def normalize_nodeid(nodeid: str, root: Path) -> str:
    filename, separator, suffix = nodeid.partition("::")
    test_path = Path(filename)
    if not test_path.is_absolute():
        test_path = Path.cwd() / test_path
    try:
        relative = test_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return nodeid
    return f"{relative}{separator}{suffix}"


def normalize_context(context: str, root: Path) -> str:
    nodeid, separator, phase = context.rpartition("|")
    if separator and phase in {"setup", "run", "teardown"} and "::" in nodeid:
        return f"{normalize_nodeid(nodeid, root)}|{phase}"
    return context


def read_phase_log(path: Path, root: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise RuntimeError(f"pytest did not write phase observations at {path}")
    records: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        records.append(
            {
                "nodeid": normalize_nodeid(record["nodeid"], root),
                "phase": record["phase"],
                "outcome": record["outcome"],
                "stdout": record["stdout"],
                "stderr": record["stderr"],
            }
        )
    return records


def _check_phase_records(
    records: list[dict[str, str]],
    expected_outcomes: dict[str, str],
    *,
    source: str,
    phase_outcomes: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    expected = expected_phase_records(expected_outcomes, phase_outcomes)
    actual_identities = [(row["nodeid"], row["phase"]) for row in records]
    expected_identities = [(row["nodeid"], row["phase"]) for row in expected]
    if actual_identities != expected_identities:
        raise RuntimeError(
            f"{source} phase records are missing, duplicated, or out of order: "
            f"expected={expected_identities}, actual={actual_identities}"
        )
    for actual, wanted in zip(records, expected, strict=True):
        if actual["outcome"] != wanted["outcome"]:
            raise RuntimeError(
                f"{source} has the wrong {wanted['phase']} outcome for "
                f"{wanted['nodeid']}: {actual['outcome']} != {wanted['outcome']}"
            )
        if not isinstance(actual["stdout"], str) or not isinstance(actual["stderr"], str):
            raise RuntimeError(f"{source} phase streams must be strings: {actual}")
    return records


def normalized_history_tests(project: VfyProject) -> dict[str, tuple[str, object]]:
    history_path = project.root / ".vfy/test-history.json"
    if not history_path.is_file():
        raise RuntimeError(f"vfy did not publish test history at {history_path}")
    history = json.loads(history_path.read_text(encoding="utf-8"))
    tests = history.get("tests", {})
    if not isinstance(tests, dict):
        raise RuntimeError(f"vfy test history has an invalid tests object: {tests}")
    normalized_tests: dict[str, tuple[str, object]] = {}
    for nodeid, entry in tests.items():
        normalized = normalize_nodeid(nodeid, project.root)
        if normalized in normalized_tests:
            raise RuntimeError(
                f"vfy history contains duplicate node identities after normalization: "
                f"{normalized_tests[normalized][0]} and {nodeid}"
            )
        normalized_tests[normalized] = (nodeid, entry)
    return normalized_tests


def _assert_history_evidence(
    project: VfyProject,
    expected_outcomes: dict[str, str],
) -> None:
    normalized_tests = normalized_history_tests(project)
    expected_nodeids = list(expected_outcomes)
    latest_observations: dict[str, dict[str, object]] = {}
    for nodeid in expected_nodeids:
        indexed = normalized_tests.get(nodeid)
        entry = indexed[1] if indexed is not None else None
        observations = entry.get("observations") if isinstance(entry, dict) else None
        if not isinstance(observations, list) or not observations:
            raise RuntimeError(f"vfy history omitted executed test {nodeid}: {entry}")
        latest = observations[-1]
        if not isinstance(latest, dict):
            raise RuntimeError(f"vfy history has an invalid observation for {nodeid}: {latest}")
        latest_observations[nodeid] = latest
    run_ids = {observation.get("run_id") for observation in latest_observations.values()}
    if len(run_ids) != 1 or None in run_ids:
        raise RuntimeError(
            f"vfy history does not identify one latest run for {expected_nodeids}: {run_ids}"
        )
    run_id = next(iter(run_ids))
    current_rows: dict[str, list[dict[str, object]]] = {}
    for normalized_nodeid, (nodeid, entry) in normalized_tests.items():
        observations = entry.get("observations", []) if isinstance(entry, dict) else []
        if not isinstance(observations, list):
            raise RuntimeError(f"vfy history has invalid observations for {nodeid}: {entry}")
        matching = [
            observation
            for observation in observations
            if isinstance(observation, dict) and observation.get("run_id") == run_id
        ]
        if matching:
            current_rows[normalized_nodeid] = matching
    if set(current_rows) != set(expected_nodeids):
        raise RuntimeError(
            "vfy history contains extra or missing tests for the latest run: "
            f"expected={expected_nodeids}, actual={sorted(current_rows)}"
        )
    for nodeid, expected_outcome in expected_outcomes.items():
        observations = current_rows[nodeid]
        if len(observations) != 1:
            raise RuntimeError(
                f"vfy history has duplicate observations for {nodeid} in run {run_id}: "
                f"{observations}"
            )
        observation = observations[0]
        if (
            observation.get("attempt") != "primary"
            or observation.get("outcome") != expected_outcome
        ):
            raise RuntimeError(
                f"vfy history has the wrong primary outcome for {nodeid}: {observation}"
            )


def _candidate_phase_records(
    native_result: Path,
    root: Path,
    expected_outcomes: dict[str, str],
    phase_outcomes: dict[str, dict[str, str]] | None,
) -> list[dict[str, str]]:
    expected_nodeids = list(expected_outcomes)
    if not native_result.is_file():
        raise RuntimeError("candidate native result artifact was not written")
    record = json.loads(native_result.read_text(encoding="utf-8"))
    if record.get("engine") != "pytest-runner-native-v1":
        raise RuntimeError("candidate did not publish its native run result")
    cases = record.get("cases")
    if not isinstance(cases, list):
        raise RuntimeError(f"candidate native result cases are invalid: {cases}")
    actual_nodeids = [
        normalize_nodeid(case["nodeid"], root)
        for case in cases
        if isinstance(case, dict) and isinstance(case.get("nodeid"), str)
    ]
    if actual_nodeids != expected_nodeids:
        raise RuntimeError(
            "candidate native result identities are missing, duplicated, or out of order: "
            f"expected={expected_nodeids}, actual={actual_nodeids}"
        )
    phase_records: list[dict[str, str]] = []
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("phases"), list):
            raise RuntimeError(f"candidate native result has an invalid case record: {case}")
        for phase in case["phases"]:
            if not isinstance(phase, dict):
                raise RuntimeError(f"candidate native result has an invalid phase: {phase}")
            phase_records.append(
                {
                    "nodeid": normalize_nodeid(case["nodeid"], root),
                    "phase": phase["phase"],
                    "outcome": phase["outcome"],
                    "stdout": phase["stdout"],
                    "stderr": phase["stderr"],
                }
            )
    return _check_phase_records(
        phase_records,
        expected_outcomes,
        source="candidate native result",
        phase_outcomes=phase_outcomes,
    )


def assert_run_evidence(
    project: VfyProject,
    native_result: Path,
    expected_outcomes: dict[str, str],
    *,
    candidate: bool,
    phase_log: Path | None = None,
    phase_outcomes: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    _assert_history_evidence(project, expected_outcomes)
    if not candidate:
        if native_result.exists():
            raise RuntimeError("stock pytest unexpectedly wrote a native result artifact")
        if phase_log is None:
            raise RuntimeError("pytest phase hook log is required for differential run evidence")
        return _check_phase_records(
            read_phase_log(phase_log, project.root),
            expected_outcomes,
            source="stock pytest",
            phase_outcomes=phase_outcomes,
        )
    checked_records = _candidate_phase_records(
        native_result, project.root, expected_outcomes, phase_outcomes
    )
    if phase_log is not None:
        pytest_records = _check_phase_records(
            read_phase_log(phase_log, project.root),
            expected_outcomes,
            source="candidate pytest phase hook",
            phase_outcomes=phase_outcomes,
        )
        if pytest_records != checked_records:
            raise RuntimeError(
                "candidate native result differs from pytest phase reports: "
                f"native={checked_records}, reports={pytest_records}"
            )
    return checked_records


CUSTOM_ITEM_CONFTEST = """import os
from pathlib import Path

import pytest


class CustomItem(pytest.Item):
    def runtest(self):
        Path(os.environ[\"VFY_NATIVE_BOUNDARY_EVENT\"]).write_text(\"ran\\n\")


class CustomCollector(pytest.File):
    def collect(self):
        return [CustomItem.from_parent(name=\"generated\", parent=self)]


def pytest_collect_file(file_path, parent):
    if file_path.name == \"test_contract.py\":
        return CustomCollector.from_parent(path=file_path, parent=parent)
"""


def run_unsupported_case(
    destination: Path,
    candidate_python: Path,
    coverage_seed: bytes | None,
    name: str,
    tests: str,
    *,
    workers: str = "1",
    native_bridge: bool = True,
    data_file: str = ".coverage",
    extra_arguments: tuple[str, ...] = (),
    expected_status: int = 4,
    expected_diagnostic: str | None = None,
    expect_cache_absent: bool = False,
    cache_seed: bytes | None = None,
    conftest: str | None = None,
) -> dict[str, object]:
    project = VfyProject(destination / name)
    project.write("src/__init__.py", "")
    project.write("src/sample.py", SOURCE)
    if conftest is not None:
        project.write("conftest.py", conftest)
    project.write("test_contract.py", tests)
    coverage_path = project.root / data_file
    if coverage_seed is not None:
        coverage_path.parent.mkdir(parents=True, exist_ok=True)
        coverage_path.write_bytes(coverage_seed)
    history_path = project.write(".vfy/test-history.json", HISTORY_SENTINEL.decode("utf-8"))
    event = destination / f"{name}-test-body-ran.txt"
    native_result = destination / f"{name}-native-result.json"
    event.unlink(missing_ok=True)
    native_result.write_bytes(RESULT_SENTINEL)
    cache_path = project.root / ".pytest_cache"
    cache_seed_path = cache_path / "v/vfy/rejected-preserved"
    if cache_seed is not None:
        cache_seed_path.parent.mkdir(parents=True, exist_ok=True)
        cache_seed_path.write_bytes(cache_seed)
    before = result_tree(project.root)
    status, stdout, stderr = run_test(
        project,
        candidate_python,
        event,
        native_result,
        tests=tests,
        workers=workers,
        native_bridge=native_bridge,
        engine="pytest-native" if native_bridge else None,
        data_file=data_file,
        extra_arguments=extra_arguments,
    )
    after = result_tree(project.root)
    changed = sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )
    diagnostic = stdout + stderr
    if (
        status != expected_status
        or "native pytest" not in diagnostic.lower()
        or (expected_diagnostic is not None and expected_diagnostic not in diagnostic)
        or event.exists()
        or (
            coverage_path.read_bytes() != coverage_seed
            if coverage_seed is not None
            else coverage_path.exists()
        )
        or history_path.read_bytes() != HISTORY_SENTINEL
        or native_result.read_bytes() != RESULT_SENTINEL
        or changed
        or (expect_cache_absent and cache_path.exists())
        or (cache_seed is not None and cache_seed_path.read_bytes() != cache_seed)
    ):
        raise RuntimeError(
            f"candidate did not reject {name} before result mutation: {status}\n"
            f"{stdout}\n{stderr}\ntest_body_ran={event.exists()} changed_files={changed}"
        )
    result = {
        "exit_status": status,
        "test_body_ran": False,
        "coverage_history_and_result_unchanged": True,
        "cache_directory_absent": not cache_path.exists(),
        "rejection": next(
            (line.strip() for line in diagnostic.splitlines() if "native pytest" in line.lower()),
            "native pytest usage error",
        ),
    }
    if cache_seed is not None:
        result["seeded_cache_value_preserved"] = True
    return result


def check_default_native_route(destination: Path, candidate_python: Path) -> dict[str, object]:
    """Prove that an unqualified product run is executed by the native engine."""
    project = VfyProject(destination / "default-native-route")
    event = destination / "default-native-route-events.txt"
    native_result = destination / "default-native-route-result.json"
    for artifact in (event, native_result):
        artifact.unlink(missing_ok=True)
    project.write("src/__init__.py", "")
    project.write("src/sample.py", SOURCE)
    project.write("test_contract.py", SUCCESS_TESTS)
    completed = project.run(
        str(candidate_python.absolute()),
        "-m",
        "vfy",
        "--isolated",
        "test",
        "--no-replay",
        "--history",
        ".vfy/test-history.json",
        "--source",
        "src",
        "--cov-branch",
        "--cov-context=test",
        "test_contract.py",
        env={
            "PYTHONPATH": None,
            "VFY_NATIVE_BOUNDARY_EVENT": str(event),
            "PYTEST_NATIVE_RESULT": str(native_result),
        },
    )
    status, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    expected_outcomes = {
        "test_contract.py::test_measured_source": "passed",
        "test_contract.py::test_no_cover_marker": "passed",
        "test_contract.py::test_no_cover_fixture": "passed",
    }
    phases = assert_run_evidence(
        project,
        native_result,
        expected_outcomes,
        candidate=True,
    )
    events = event.read_text(encoding="utf-8") if event.is_file() else ""
    coverage_path = project.root / ".coverage"
    if status != 0 or events != "measured\nmarker\nfixture\n" or not coverage_path.is_file():
        raise RuntimeError(
            "candidate default route did not execute through the native engine: "
            f"status={status} events={events!r}\n{stdout}\n{stderr}"
        )
    return {
        "exit_status": status,
        "engine": "pytest-native",
        "events": events.splitlines(),
        "coverage": normalized_coverage(coverage_path, project.root),
        "phases": phases,
    }


def check_explicit_pytest_route(destination: Path, candidate_python: Path) -> dict[str, object]:
    """Keep the stock pytest route available for internal differential checks."""
    project = VfyProject(destination / "explicit-pytest-route")
    event = destination / "explicit-pytest-route-events.txt"
    native_result = destination / "explicit-pytest-route-native-result.json"
    phase_log = destination / "explicit-pytest-route-phases.jsonl"
    for artifact in (event, native_result, phase_log):
        artifact.unlink(missing_ok=True)
    status, stdout, stderr = run_test(
        project,
        candidate_python,
        event,
        native_result,
        engine="pytest",
        phase_log=phase_log,
    )
    expected_outcomes = {
        "test_contract.py::test_measured_source": "passed",
        "test_contract.py::test_no_cover_marker": "passed",
        "test_contract.py::test_no_cover_fixture": "passed",
    }
    phases = assert_run_evidence(
        project,
        native_result,
        expected_outcomes,
        candidate=False,
        phase_log=phase_log,
    )
    events = event.read_text(encoding="utf-8") if event.is_file() else ""
    coverage_path = project.root / ".coverage"
    if status != 0 or events != "measured\nmarker\nfixture\n" or not coverage_path.is_file():
        raise RuntimeError(
            "candidate explicit pytest route did not preserve stock behavior: "
            f"status={status} events={events!r}\n{stdout}\n{stderr}"
        )
    return {
        "exit_status": status,
        "engine": "pytest",
        "events": events.splitlines(),
        "coverage": normalized_coverage(coverage_path, project.root),
        "phases": phases,
    }


def check_no_cov(
    destination: Path, stock_python: Path, candidate_python: Path
) -> dict[str, object]:
    """Prove --no-cov runs without requiring or modifying coverage storage."""
    tests = """from pathlib import Path


def _record(value):
    with Path("no-cov-events.txt").open("a", encoding="utf-8") as stream:
        stream.write(value + "\\n")


def test_passes_without_coverage():
    _record("passed")
    assert True


def test_failure_is_still_reported():
    _record("failed")
    assert False, "controlled no-cov failure"
"""
    expected_outcomes = {
        "test_contract.py::test_passes_without_coverage": "passed",
        "test_contract.py::test_failure_is_still_reported": "failed",
    }
    expected_phases = [
        ("test_contract.py::test_passes_without_coverage", "setup", "passed"),
        ("test_contract.py::test_passes_without_coverage", "call", "passed"),
        ("test_contract.py::test_passes_without_coverage", "teardown", "passed"),
        ("test_contract.py::test_failure_is_still_reported", "setup", "passed"),
        ("test_contract.py::test_failure_is_still_reported", "call", "failed"),
        ("test_contract.py::test_failure_is_still_reported", "teardown", "passed"),
    ]
    observed: dict[str, dict[str, object]] = {}
    data_file = "nested/missing/.coverage"
    seed = b"coverage data owned by the caller\n"

    for state in ("absent", "seeded"):
        for name, python in (("stock", stock_python), ("candidate", candidate_python)):
            project_name = f"no-cov-{state}-{name}"
            project = VfyProject(destination / project_name)
            project.write("src/__init__.py", "")
            project.write("src/sample.py", SOURCE)
            project.write("test_contract.py", tests)
            coverage_path = project.root / data_file
            if state == "seeded":
                coverage_path.parent.mkdir(parents=True)
                coverage_path.write_bytes(seed)
            event = project.root / "no-cov-events.txt"
            native_result = destination / f"{project_name}-native-result.json"
            native_result.unlink(missing_ok=True)
            status, stdout, stderr = command(
                project,
                python,
                (
                    "--isolated",
                    "test",
                    "--workers=1",
                    "--no-replay",
                    "--history",
                    ".vfy/test-history.json",
                    "--data-file",
                    data_file,
                    "--source",
                    "src",
                    "--no-cov",
                    "-q",
                    "test_contract.py",
                ),
                event=event,
                native_result=native_result,
                native_bridge=name == "candidate",
            )
            if status != 1:
                raise RuntimeError(
                    f"{project_name}: expected controlled test failure exit 1, got {status}\n"
                    f"{stdout}\n{stderr}"
                )
            if event.read_text(encoding="utf-8").splitlines() != ["passed", "failed"]:
                raise RuntimeError(f"{project_name}: tests did not execute exactly once")
            actual_seed = coverage_path.read_bytes() if coverage_path.is_file() else None
            expected_seed = seed if state == "seeded" else None
            missing_parent_created = state == "absent" and coverage_path.parent.exists()
            if actual_seed != expected_seed or missing_parent_created:
                raise RuntimeError(
                    f"{project_name}: --no-cov changed or required coverage storage at "
                    f"{coverage_path}"
                )

            history_path = project.root / ".vfy/test-history.json"
            if not history_path.is_file():
                raise RuntimeError(f"{project_name}: native test history was not published")
            history = json.loads(history_path.read_text(encoding="utf-8"))
            history_tests = history.get("tests", {})
            history_outcomes = {
                normalize_nodeid(nodeid, project.root): [
                    item["outcome"] for item in record.get("observations", [])
                ]
                for nodeid, record in sorted(history_tests.items())
            }
            expected_history = {nodeid: [outcome] for nodeid, outcome in expected_outcomes.items()}
            if history_outcomes != expected_history:
                raise RuntimeError(
                    f"{project_name}: unexpected vfy history observations: {history_outcomes}"
                )

            phases: list[tuple[str, str, str]] | None = None
            if name == "candidate":
                if not native_result.is_file():
                    raise RuntimeError(f"{project_name}: native result was not published")
                result = json.loads(native_result.read_text(encoding="utf-8"))
                if result.get("engine") != "pytest-runner-native-v1":
                    raise RuntimeError(f"{project_name}: native result provenance is missing")
                phases = [
                    (
                        normalize_nodeid(case["nodeid"], project.root),
                        phase["phase"],
                        phase["outcome"],
                    )
                    for case in result.get("cases", [])
                    for phase in case.get("phases", [])
                ]
                if phases != expected_phases:
                    raise RuntimeError(f"{project_name}: native phase results differ: {phases}")
            elif native_result.exists():
                raise RuntimeError(f"{project_name}: stock pytest wrote a native result")

            observed[f"{state}-{name}"] = {
                "exit_status": status,
                "history_outcomes": history_outcomes,
                "native_phases": phases,
            }

        stock = observed[f"{state}-stock"]
        candidate = observed[f"{state}-candidate"]
        if (
            stock["exit_status"] != candidate["exit_status"]
            or stock["history_outcomes"] != candidate["history_outcomes"]
        ):
            raise RuntimeError(f"--no-cov vfy result/history differs from stock: {observed}")

    return {
        "runs": observed,
        "matches_stock_for_exit_history_and_failed_execution": True,
        "absent_coverage_parent_remains_absent": True,
        "existing_coverage_bytes_preserved": True,
        "candidate_executes_once_and_publishes_native_phases": True,
    }


def check_missing_coverage_adapter(destination: Path, candidate_python: Path) -> dict[str, object]:
    """Reject an absent coverage adapter when pytest has not disabled coverage."""
    project = VfyProject(destination / "missing-coverage-adapter")
    project.write("probe.py", "")
    check = """from types import SimpleNamespace

import vfy._pytest.native_pytest as native_pytest


def unexpected_native_run(*args, **kwargs):
    raise AssertionError("native execution started without an enabled coverage adapter")


native_pytest.run_native_suite = unexpected_native_run
session = SimpleNamespace(
    items=[],
    config=SimpleNamespace(
        pluginmanager=SimpleNamespace(getplugin=lambda name: None),
        getoption=lambda name, default=False: default,
    )
)
try:
    native_pytest._run_native_session(session, True, 0, 0, True)
except native_pytest.UnsupportedExtensionError as error:
    assert "enabled coverage adapter" in str(error)
else:
    raise AssertionError("native execution accepted a missing coverage adapter")

session.config.getoption = lambda name, default=False: name == "no_cov"
try:
    native_pytest._run_native_session(session, True, 0, 0, True)
except native_pytest.UnsupportedExtensionError as error:
    assert "result adapter" in str(error)
else:
    raise AssertionError("native no-cov execution accepted a missing result adapter")
"""
    result = project.run(str(candidate_python.absolute()), "-c", check)
    if result.returncode != 0:
        raise RuntimeError(
            "native pytest did not reject the missing enabled coverage adapter before execution:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return {
        "missing_coverage_adapter_rejected_before_native_execution": True,
        "missing_no_cov_result_adapter_rejected_before_native_execution": True,
    }


def event_test_source(name: str, parameters: str = "", *, before_event: str = "") -> str:
    prelude = "".join(f"    {line}\n" for line in before_event.splitlines())
    return (
        "import os\nfrom pathlib import Path\n\n\n"
        f"def {name}({parameters}):\n"
        f"{prelude}"
        '    event = Path(os.environ["VFY_NATIVE_BOUNDARY_EVENT"])\n'
        '    event.write_text("ran\\n")\n'
    )


def check_subprocess_coverage(
    destination: Path, stock_python: Path, candidate_python: Path
) -> dict[str, object]:
    observed: dict[str, dict[str, object]] = {}
    for name, python in (("stock", stock_python), ("candidate", candidate_python)):
        project = VfyProject(destination / f"subprocess-{name}")
        project.write("src/__init__.py", "")
        project.write(
            "src/child.py",
            "def child():\n"
            "    answer = 40 + 2\n"
            "    assert answer == 42\n\n"
            'if __name__ == "__main__":\n'
            "    child()\n",
        )
        tests = (
            "import subprocess\n"
            "import sys\n\n"
            "def test_child():\n"
            '    subprocess.run([sys.executable, "-m", "src.child"], check=True)\n'
        )
        event = destination / f"subprocess-{name}-event.txt"
        native_result = destination / f"subprocess-{name}-native-result.json"
        stock_phase_log = destination / f"subprocess-{name}-stock-phases.jsonl"
        stock_phase_log.unlink(missing_ok=True)
        status, stdout, stderr = run_test(
            project,
            python,
            event,
            native_result,
            tests=tests,
            native_bridge=name == "candidate",
            phase_log=stock_phase_log if name == "stock" else None,
        )
        if status != 0:
            raise RuntimeError(f"{name} vfy subprocess run failed: {status}\n{stdout}\n{stderr}")
        phases = assert_run_evidence(
            project,
            native_result,
            {"test_contract.py::test_child": "passed"},
            candidate=name == "candidate",
            phase_log=stock_phase_log if name == "stock" else None,
        )
        coverage = normalized_coverage(project.root / ".coverage", project.root)
        child = coverage["files"].get("src/child.py")
        if child is None or not set(child["lines"]) >= {1, 2, 3, 6}:
            raise RuntimeError(f"{name} vfy omitted child-process lines: {coverage}")
        observed[name] = {"coverage": coverage, "phases": phases}
    if observed["candidate"] != observed["stock"]:
        raise RuntimeError(f"native vfy subprocess coverage differs from stock: {observed}")
    return {"coverage_matches_stock": True, "child_lines_recorded": True}


def check_selected_run(
    destination: Path, stock_python: Path, candidate_python: Path
) -> dict[str, object]:
    selection_arguments = (
        "--isolated",
        "test",
        "--workers=1",
        "--history",
        ".vfy/test-history.json",
        "--data-file",
        ".coverage",
        "--source",
        "src",
        "--cov-branch",
        "--cov-context=test",
        "-p",
        "vfy.pytest_plugin",
        "tests",
    )
    expected = {"tests/test_first.py::test_first": "passed"}
    runs: dict[str, dict[str, object]] = {}
    with tempfile.TemporaryDirectory(prefix="vfy-selection-boundary-") as temporary:
        for name, python, native_bridge in (
            ("stock", stock_python, False),
            ("candidate", candidate_python, True),
        ):
            # Selection deliberately ignores generated build trees such as `target/`.
            # Keep these Git fixtures outside the retained artifact directory so a
            # changed source file is observable to the production selector.
            project = VfyProject(Path(temporary) / name)
            project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
            project.write("pytest.ini", "[pytest]\n")
            project.write("src/__init__.py", "")
            project.write("src/first.py", "def value():\n    return 1\n")
            project.write("src/second.py", "def value():\n    return 2\n")
            project.write(
                "tests/test_first.py",
                "import os\nfrom pathlib import Path\nfrom src.first import value\n\n"
                "def test_first():\n"
                '    with Path(os.environ["VFY_NATIVE_BOUNDARY_EVENT"]).open("a") as event:\n'
                '        event.write("first\\n")\n'
                "    assert value() == 1\n",
            )
            project.write(
                "tests/test_second.py",
                "import os\nfrom pathlib import Path\nfrom src.second import value\n\n"
                "def test_second():\n"
                '    with Path(os.environ["VFY_NATIVE_BOUNDARY_EVENT"]).open("a") as event:\n'
                '        event.write("second\\n")\n'
                "    assert value() == 2\n",
            )
            if not native_bridge:
                project.write("conftest.py", PHASE_CONFTST)
            project.git_init()
            event = destination / f"selected-{name}-event.txt"
            native_result = destination / f"selected-{name}-native-result.json"
            stock_phase_log = destination / f"selected-{name}-stock-phases.jsonl"
            event.unlink(missing_ok=True)
            native_result.unlink(missing_ok=True)
            stock_phase_log.unlink(missing_ok=True)
            baseline_status, baseline_out, baseline_err = command(
                project,
                python,
                selection_arguments,
                event=event,
                native_result=native_result,
                native_bridge=native_bridge,
                phase_log=stock_phase_log if not native_bridge else None,
            )
            if baseline_status != 0 or not (project.root / ".vfy/import-graph.json").is_file():
                raise RuntimeError(
                    f"{name} vfy could not establish its independent selection baseline: "
                    f"{baseline_status}\n{baseline_out}\n{baseline_err}"
                )
            event.unlink(missing_ok=True)
            native_result.unlink(missing_ok=True)
            stock_phase_log.unlink(missing_ok=True)
            project.write(
                "src/first.py",
                "def value():\n    if True:\n        return 1\n    return 0\n",
            )
            selected_status, selected_out, selected_err = command(
                project,
                python,
                selection_arguments,
                event=event,
                native_result=native_result,
                native_bridge=native_bridge,
                phase_log=stock_phase_log if not native_bridge else None,
            )
            if (
                selected_status != 0
                or not event.is_file()
                or event.read_text(encoding="utf-8") != "first\n"
            ):
                raise RuntimeError(
                    f"{name} selected vfy run did not execute only the affected case: "
                    f"{selected_status}\n{selected_out}\n{selected_err}"
                )
            phases = assert_run_evidence(
                project,
                native_result,
                expected,
                candidate=native_bridge,
                phase_log=stock_phase_log if not native_bridge else None,
            )
            runs[name] = {
                "baseline_exit_status": baseline_status,
                "selected_exit_status": selected_status,
                "events": event.read_text(encoding="utf-8").splitlines(),
                "coverage": normalized_coverage(project.root / ".coverage", project.root),
                "phases": phases,
            }
    if runs["stock"] != runs["candidate"]:
        raise RuntimeError(f"stock and candidate selected runs differ: {runs}")
    return {
        "runs": runs,
        "selected_nodeids": sorted(expected),
        "selection_callback_supported": True,
        "explicit_vfy_plugin_supported": True,
    }


def check_accepted_execution_failure(
    destination: Path, stock_python: Path, candidate_python: Path
) -> dict[str, object]:
    tests = """import os
from pathlib import Path

import pytest


def test_completed_before_interrupt():
    with Path(os.environ["VFY_NATIVE_BOUNDARY_EVENT"]).open("a") as event:
        event.write("completed\\n")


def test_exit_after_acceptance():
    with Path(os.environ["VFY_NATIVE_BOUNDARY_EVENT"]).open("a") as event:
        event.write("accepted\\n")
    pytest.exit("expected accepted runtime interruption", returncode=2)
"""
    observations: dict[str, object] = {}
    history_outcomes: dict[str, dict[str, str]] = {}
    coverage_data: dict[str, object] = {}
    for name, python, candidate in (
        ("stock", stock_python, False),
        ("candidate", candidate_python, True),
    ):
        project = VfyProject(destination / f"accepted-runtime-{name}")
        project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
        event = destination / f"accepted-runtime-{name}.txt"
        native_result = destination / f"accepted-runtime-{name}-native.json"
        event.unlink(missing_ok=True)
        native_result.unlink(missing_ok=True)
        baseline_status, baseline_stdout, baseline_stderr = run_test(
            project,
            python,
            event,
            native_result,
            native_bridge=candidate,
        )
        if baseline_status != 0:
            raise RuntimeError(
                f"{name} vfy could not seed accepted-runtime history: "
                f"{baseline_status}\n{baseline_stdout}\n{baseline_stderr}"
            )
        event.unlink(missing_ok=True)
        native_result.write_bytes(RESULT_SENTINEL)
        project.write("test_contract.py", tests)
        (project.root / ".vfy/test-run.json").write_bytes(RESULT_SENTINEL)
        result = command(
            project,
            python,
            (
                "--isolated",
                "test",
                "--workers=1",
                "--no-replay",
                "--history",
                ".vfy/test-history.json",
                "--data-file",
                ".coverage",
                "--source",
                "src",
                "--cov-branch",
                "--cov-context=test",
                "test_contract.py",
            ),
            event=event,
            native_result=native_result,
            native_bridge=candidate,
        )
        status, stdout, stderr = result
        result_path = project.root / ".vfy/test-run.json"
        if (
            status != 2
            or not event.is_file()
            or event.read_text(encoding="utf-8") != "completed\naccepted\n"
            or result_path.read_bytes() != RESULT_SENTINEL
            or native_result.read_bytes() != RESULT_SENTINEL
        ):
            raise RuntimeError(
                f"{name} vfy did not preserve accepted interruption evidence: "
                f"{status}\n{stdout}\n{stderr}"
            )
        expected_nodeid = "test_contract.py::test_completed_before_interrupt"
        normalized_history = normalized_history_tests(project)
        expected_history = normalized_history.get(expected_nodeid)
        history_entries = (
            expected_history[1].get("observations", [])
            if expected_history is not None and isinstance(expected_history[1], dict)
            else []
        )
        if not history_entries or history_entries[-1].get("outcome") != "passed":
            raise RuntimeError(f"{name} history omitted the completed test: {history_entries}")
        history_outcomes[name] = {expected_nodeid: history_entries[-1]["outcome"]}
        observations[name] = {
            "exit_status": status,
            "test_body_ran": True,
            "previous_green_record_preserved": True,
            "completed_case_in_history": expected_nodeid,
        }
        coverage_data[name] = normalized_coverage(project.root / ".coverage", project.root)
    if history_outcomes["stock"] != history_outcomes["candidate"]:
        raise RuntimeError(f"accepted interruption history differs from stock: {history_outcomes}")
    if coverage_data["stock"] != coverage_data["candidate"]:
        raise RuntimeError(
            "accepted interruption coverage differs from stock: "
            f"{json.dumps(coverage_data, sort_keys=True)}"
        )
    for observation in observations.values():
        observation["history_matches_stock"] = True
        observation["coverage_matches_stock"] = True
    return observations


def check_maxfail(
    destination: Path, stock_python: Path, candidate_python: Path
) -> dict[str, object]:
    tests = """import os
from pathlib import Path


def test_first_fails():
    with Path(os.environ["VFY_NATIVE_BOUNDARY_EVENT"]).open("a") as event:
        event.write("first\\n")
    assert False


def test_second_does_not_run():
    with Path(os.environ["VFY_NATIVE_BOUNDARY_EVENT"]).open("a") as event:
        event.write("second\\n")
"""
    expected = {"test_contract.py::test_first_fails": "failed"}
    observations: dict[str, object] = {}
    coverage_data: dict[str, object] = {}
    phase_data: dict[str, list[dict[str, str]]] = {}
    for name, python, candidate in (
        ("stock", stock_python, False),
        ("candidate", candidate_python, True),
    ):
        project = VfyProject(destination / f"maxfail-{name}")
        event = destination / f"maxfail-{name}-event.txt"
        native_result = destination / f"maxfail-{name}-native-result.json"
        stock_phase_log = destination / f"maxfail-{name}-stock-phases.jsonl"
        event.unlink(missing_ok=True)
        native_result.unlink(missing_ok=True)
        stock_phase_log.unlink(missing_ok=True)
        status, stdout, stderr = run_test(
            project,
            python,
            event,
            native_result,
            tests=tests,
            native_bridge=candidate,
            extra_arguments=("--maxfail=1",),
            phase_log=stock_phase_log if not candidate else None,
        )
        if status != 1 or not event.is_file() or event.read_text(encoding="utf-8") != "first\n":
            raise RuntimeError(
                f"{name} vfy maxfail did not stop after the first failure: "
                f"{status}\n{stdout}\n{stderr}"
            )
        phase_data[name] = assert_run_evidence(
            project,
            native_result,
            expected,
            candidate=candidate,
            phase_log=stock_phase_log if not candidate else None,
        )
        coverage_data[name] = normalized_coverage(project.root / ".coverage", project.root)
        observations[name] = {
            "exit_status": status,
            "executed_events": event.read_text(encoding="utf-8").splitlines(),
            "result_nodeids": sorted(expected),
        }
    if coverage_data["stock"] != coverage_data["candidate"]:
        raise RuntimeError(
            "native vfy maxfail coverage differs from stock: "
            f"{json.dumps(coverage_data, sort_keys=True)}"
        )
    if phase_data["stock"] != phase_data["candidate"]:
        raise RuntimeError(f"native vfy maxfail phase records differ from stock: {phase_data}")
    return {
        "runs": observations,
        "coverage_matches_stock": True,
    }


def check_coverage_save_failure(
    destination: Path, stock_python: Path, candidate_python: Path
) -> dict[str, object]:
    tests = """import os
from pathlib import Path


def test_coverage_save_fails(request):
    coverage = request.config.pluginmanager.getplugin("_cov").coverage

    def fail_save():
        raise OSError("expected coverage save failure")

    coverage.save = fail_save
    Path(os.environ["VFY_NATIVE_BOUNDARY_EVENT"]).write_text("ran\\n")
"""
    runs: dict[str, dict[str, object]] = {}
    for name, python, native_bridge in (
        ("stock", stock_python, False),
        ("candidate", candidate_python, True),
    ):
        project = VfyProject(destination / f"coverage-save-failure-{name}")
        event = destination / f"coverage-save-failure-{name}-event.txt"
        native_result = destination / f"coverage-save-failure-{name}-native-result.json"
        event.unlink(missing_ok=True)
        native_result.unlink(missing_ok=True)
        baseline_status, baseline_stdout, baseline_stderr = run_test(
            project,
            python,
            event,
            native_result,
            native_bridge=native_bridge,
        )
        if baseline_status != 0:
            raise RuntimeError(
                f"{name} vfy could not seed coverage-save failure state: "
                f"{baseline_status}\n{baseline_stdout}\n{baseline_stderr}"
            )

        coverage_path = project.root / ".coverage"
        history_path = project.root / ".vfy/test-history.json"
        green_run_path = project.root / ".vfy/test-run.json"
        coverage_before = coverage_path.read_bytes()
        history_before = history_path.read_bytes()
        green_run_path.write_bytes(RESULT_SENTINEL)
        event.unlink(missing_ok=True)
        status, stdout, stderr = run_test(
            project,
            python,
            event,
            native_result,
            tests=tests,
            native_bridge=native_bridge,
        )
        if status == 0 or not event.is_file() or event.read_text(encoding="utf-8") != "ran\n":
            raise RuntimeError(
                f"{name} vfy did not surface coverage save failure: {status}\n{stdout}\n{stderr}"
            )
        if native_bridge:
            if not coverage_path.is_file() or coverage_path.read_bytes() != coverage_before:
                raise RuntimeError("candidate coverage save failure replaced prior coverage data")
            coverage_state = "previous_database_preserved"
        else:
            if coverage_path.exists():
                raise RuntimeError(
                    "stock coverage save failure no longer matches its replace-mode behavior"
                )
            coverage_state = "database_erased_before_save_failure"
        if history_path.read_bytes() != history_before:
            raise RuntimeError(f"{name} coverage save failure published test history")
        if green_run_path.read_bytes() != RESULT_SENTINEL:
            raise RuntimeError(f"{name} coverage save failure published a green-run record")
        runs[name] = {
            "exit_status": status,
            "test_body_ran": True,
            "coverage_state": coverage_state,
            "history_preserved": True,
            "previous_green_record_preserved": True,
        }
    comparable = (
        "exit_status",
        "test_body_ran",
        "history_preserved",
        "previous_green_record_preserved",
    )
    if any(runs["stock"][key] != runs["candidate"][key] for key in comparable):
        raise RuntimeError(
            "native vfy coverage-save failure behavior differs from stock: "
            f"{json.dumps(runs, sort_keys=True)}"
        )
    return {
        "runs": runs,
        "matches_stock_for_status_and_result_preservation": True,
        "candidate_preserves_previous_coverage_atomically": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-python", required=True, type=Path)
    parser.add_argument("--candidate-python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    success_outcomes = {
        "test_contract.py::test_measured_source": "passed",
        "test_contract.py::test_no_cover_marker": "passed",
        "test_contract.py::test_no_cover_fixture": "passed",
    }
    expected_events = "measured\nmarker\nfixture\n"
    success_runs: dict[str, dict[str, object]] = {}
    stock_data = b""
    for name, python, candidate in (
        ("stock", args.stock_python, False),
        ("candidate", args.candidate_python, True),
    ):
        project = VfyProject(destination / f"project-{name}")
        event = destination / f"test-body-ran-{name}.txt"
        native_result = destination / f"{name}-native-result.json"
        stock_phase_log = destination / f"{name}-stock-phases.jsonl"
        event.unlink(missing_ok=True)
        native_result.unlink(missing_ok=True)
        stock_phase_log.unlink(missing_ok=True)
        status, stdout, stderr = run_test(
            project,
            python,
            event,
            native_result,
            native_bridge=candidate,
            phase_log=stock_phase_log if not candidate else None,
        )
        if (
            status != 0
            or not event.is_file()
            or event.read_text(encoding="utf-8") != expected_events
            or not (project.root / ".coverage").is_file()
        ):
            raise RuntimeError(
                f"{name} vfy test did not execute and collect coverage: {status}\n"
                f"{stdout}\n{stderr}"
            )
        coverage = normalized_coverage(project.root / ".coverage", project.root)
        if not coverage["has_arcs"]:
            raise RuntimeError(f"{name} vfy run did not record branch arcs")
        phases = assert_run_evidence(
            project,
            native_result,
            success_outcomes,
            candidate=candidate,
            phase_log=stock_phase_log if not candidate else None,
        )
        success_runs[name] = {
            "exit_status": status,
            "events": event.read_text(encoding="utf-8"),
            "coverage": coverage,
            "phases": phases,
        }
        if not candidate:
            stock_data = (project.root / ".coverage").read_bytes()
    stock_status = success_runs["stock"]["exit_status"]
    candidate_status = success_runs["candidate"]["exit_status"]
    stock_coverage = success_runs["stock"]["coverage"]
    candidate_coverage = success_runs["candidate"]["coverage"]
    if candidate_coverage != stock_coverage:
        raise RuntimeError(
            "installed candidate coverage differs from stock vfy: "
            f"stock={json.dumps(stock_coverage, sort_keys=True)}\n"
            f"candidate={json.dumps(candidate_coverage, sort_keys=True)}"
        )
    if success_runs["candidate"] != success_runs["stock"]:
        raise RuntimeError(f"candidate run evidence differs from stock vfy: {success_runs}")
    expected_contexts = {
        "test_contract.py::test_measured_source|setup",
        "test_contract.py::test_measured_source|run",
        "test_contract.py::test_measured_source|teardown",
    }
    contexts = set(candidate_coverage["measured_contexts"])
    if not expected_contexts <= contexts:
        raise RuntimeError(f"candidate omitted setup/run/teardown coverage contexts: {contexts}")
    excluded_contexts = {
        "test_contract.py::test_no_cover_marker|run",
        "test_contract.py::test_no_cover_fixture|run",
    }
    if excluded_contexts & contexts:
        raise RuntimeError(
            f"candidate collected no_cover call contexts: {contexts & excluded_contexts}"
        )

    failure_outcomes = {
        "test_contract.py::test_no_cover_raises": "failed",
        "test_contract.py::test_coverage_resumes_after_no_cover_failure": "passed",
    }
    failure_runs: dict[str, dict[str, object]] = {}
    for name, python, candidate in (
        ("stock", args.stock_python, False),
        ("candidate", args.candidate_python, True),
    ):
        failure_project = VfyProject(destination / f"no-cover-failure-{name}")
        failure_event = destination / f"no-cover-failure-test-body-ran-{name}.txt"
        failure_native_result = destination / f"no-cover-failure-{name}-result.json"
        failure_phase_log = destination / f"no-cover-failure-{name}-phases.jsonl"
        failure_event.unlink(missing_ok=True)
        failure_native_result.unlink(missing_ok=True)
        failure_phase_log.unlink(missing_ok=True)
        status, stdout, stderr = run_test(
            failure_project,
            python,
            failure_event,
            failure_native_result,
            tests=NO_COVER_EXCEPTION_TESTS,
            native_bridge=candidate,
            phase_log=failure_phase_log if not candidate else None,
        )
        if status != 1:
            raise RuntimeError(f"{name} no_cover failure run returned {status}: {stdout}\n{stderr}")
        phases = assert_run_evidence(
            failure_project,
            failure_native_result,
            failure_outcomes,
            candidate=candidate,
            phase_log=failure_phase_log if not candidate else None,
        )
        failure_runs[name] = {
            "exit_status": status,
            "events": failure_event.read_text(encoding="utf-8") if failure_event.is_file() else "",
            "coverage": normalized_coverage(
                failure_project.root / ".coverage", failure_project.root
            ),
            "phases": phases,
        }
    failure_candidate_status = failure_runs["candidate"]["exit_status"]
    failure_stock_coverage = failure_runs["stock"]["coverage"]
    failure_candidate_coverage = failure_runs["candidate"]["coverage"]
    if failure_candidate_coverage != failure_stock_coverage:
        raise RuntimeError(
            "no_cover exception did not restore stock coverage lifecycle: "
            f"stock={json.dumps(failure_stock_coverage, sort_keys=True)}\n"
            f"candidate={json.dumps(failure_candidate_coverage, sort_keys=True)}"
        )
    if failure_runs["candidate"] != failure_runs["stock"]:
        raise RuntimeError(f"candidate no_cover run evidence differs from stock: {failure_runs}")

    selected_run = check_selected_run(destination, args.stock_python, args.candidate_python)
    unsupported = {
        "xdist_stepwise": run_unsupported_case(
            destination,
            args.candidate_python,
            stock_data,
            "xdist-stepwise",
            event_test_source("test_xdist_stepwise_rejected"),
            extra_arguments=("--numprocesses", "2", "--stepwise"),
            expected_diagnostic="native pytest does not implement stepwise execution with xdist",
        ),
        "custom_collection_item": run_unsupported_case(
            destination,
            args.candidate_python,
            stock_data,
            "custom-collection-item",
            'def test_native_item_body_must_not_run():\n    raise AssertionError("must not run")\n',
            expect_cache_absent=True,
            conftest=CUSTOM_ITEM_CONFTEST,
        ),
        "cov_append": run_unsupported_case(
            destination,
            args.candidate_python,
            stock_data,
            "cov-append",
            event_test_source("test_append_rejected"),
            extra_arguments=("--cov-append",),
            expected_status=1,
        ),
        "missing_data_parent": run_unsupported_case(
            destination,
            args.candidate_python,
            None,
            "missing-data-parent",
            event_test_source("test_missing_parent_rejected"),
            data_file="nested/data/.coverage",
        ),
    }

    default_native_route = check_default_native_route(destination, args.candidate_python)
    explicit_pytest_route = check_explicit_pytest_route(destination, args.candidate_python)

    maxfail = check_maxfail(destination, args.stock_python, args.candidate_python)
    accepted_failure = check_accepted_execution_failure(
        destination, args.stock_python, args.candidate_python
    )
    coverage_save_failure = check_coverage_save_failure(
        destination, args.stock_python, args.candidate_python
    )
    subprocess_coverage = check_subprocess_coverage(
        destination, args.stock_python, args.candidate_python
    )
    no_cov = check_no_cov(destination, args.stock_python, args.candidate_python)
    missing_coverage_adapter = check_missing_coverage_adapter(destination, args.candidate_python)
    unsupported["collection_subprocess_before_rejection"] = run_unsupported_case(
        destination,
        args.candidate_python,
        stock_data,
        "collection-subprocess-before-rejection",
        "import subprocess\n"
        "import sys\n\n"
        "subprocess.run(\n"
        '    [sys.executable, "-c",\n'
        '     "from src.sample import measured; assert measured(True) == 42"],\n'
        "    check=True,\n"
        ")\n\n"
        "def test_cache(cache):\n"
        "    assert cache is not None\n\n"
        "def test_unsupported_after_cache_collection():\n"
        '    raise AssertionError("must not run")\n',
        expect_cache_absent=True,
        conftest=CUSTOM_ITEM_CONFTEST,
    )
    report = destination / "vfy-coverage-boundary.json"
    report.write_text(
        json.dumps(
            {
                "stock": {
                    "pytest": "9.1.1",
                    "exit_status": stock_status,
                    "coverage": stock_coverage,
                    "result_and_history_written": True,
                },
                "candidate": {
                    "exit_status": candidate_status,
                    "coverage": candidate_coverage,
                    "coverage_matches_stock": True,
                    "result_and_history_written": True,
                    "no_cover_exception_exit_status": failure_candidate_status,
                    "no_cover_exception_coverage_matches_stock": True,
                },
                "selected_run": selected_run,
                "default_native_route": default_native_route,
                "explicit_pytest_route": explicit_pytest_route,
                "maxfail": maxfail,
                "accepted_execution_failure": accepted_failure,
                "coverage_save_failure": coverage_save_failure,
                "subprocess_coverage": subprocess_coverage,
                "no_cov": no_cov,
                "missing_coverage_adapter": missing_coverage_adapter,
                "unsupported": unsupported,
                "integration_status": (
                    "default serial synchronous native vfy coverage and no-coverage "
                    "execution are parity-checked; explicit pytest execution remains available "
                    "for internal differentials; "
                    "unsupported modes reject during bounded preflight"
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(f"{report}\n")


if __name__ == "__main__":
    main()
