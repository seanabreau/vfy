"""Required affected tests and oracle outcomes protect the real selection comparison."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import FunctionType
from typing import cast

import pytest
from benchmarks import benchmark_resources
from benchmarks.benchmark_projects import GeneratedFiles
from benchmarks.benchmark_real_selection import (
    PLATFORMDIRS_EDIT,
    RICH_EDIT,
    SelectionEngine,
    build_testmon_command,
    check_coverage_stage,
    check_native_junit,
    check_required_selection,
    compare_outcomes,
    full_oracle,
    native_outcomes,
    parse_args,
    required_affected_nodes,
    run_pair,
    run_testmon,
    selection_project,
    stash_testmon_data,
)
from benchmarks.project_manifest import EditPatch, ProjectManifest, manifest
from benchmarks.rich_workload import commit_rich_test_adaptations, install_rich_clock_fixture
from scripts.process_tree import run_captured

from tests.support.vfy_project import VfyProject


def selection_fixture(root: Path) -> tuple[VfyProject, ProjectManifest, Path]:
    project = VfyProject(root)
    project.write(".gitignore", ".vfy/\n.pytest_cache/\n__pycache__/\n")
    project.write("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    project.write("app.py", "def first():\n    return 1\ndef second():\n    return 2\n")
    project.write(
        "tests/test_app.py",
        "import pytest\nfrom app import first, second\n"
        "def test_first():\n    assert first() == 1\n"
        "@pytest.mark.xfail(reason='expected')\n"
        "def test_expected():\n    assert first() == 0\n"
        "def test_second():\n    assert second() == 2\n",
    )
    config = project.write("coverage.ini", "[run]\n")
    project.git_init()
    workload = replace(
        manifest("platformdirs"),
        tests=("tests",),
        package_path="app.py",
        coverage_args=("--cov=app", "--capture=no"),
        parallel_args=(),
        plugins=(),
        generated_files=(),
        workers=1,
    )
    return project, workload, config


@pytest.mark.parametrize("engine", ["native", "pytest-native"])
def test_selection_harness_runs_selected_engine_with_complete_oracle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: SelectionEngine
) -> None:
    project, workload, config = selection_fixture(tmp_path / "project")
    arguments = ["real-selection", "--project", "platformdirs"]
    for option, value in (
        ("--root", project.root),
        ("--python", sys.executable),
        ("--preparation-manifest", tmp_path / "manifest.json"),
        ("--artifacts", tmp_path / "artifacts"),
        ("--output", tmp_path / "result.json"),
    ):
        arguments.extend((option, str(value)))
    if engine == "pytest-native":
        arguments.append("--engine=pytest-native")
    monkeypatch.setattr(sys, "argv", arguments)
    args = parse_args()
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=project.root, text=True
    ).strip()
    result = run_pair(
        project.root,
        Path(sys.executable),
        tmp_path / "artifacts",
        revision,
        workload,
        EditPatch(
            "equivalent",
            "app.py",
            "return 1",
            "return 1 + 0",
            "local",
            ("tests/test_app.py::test_first",),
            "Preserves the result while changing the first function's code.",
        ),
        config,
        GeneratedFiles(project.root, ()),
        order=("vfy", "testmon"),
        measure=False,
        engine=args.engine,
    )
    assert result["vfy_engine"] == engine
    assert result["vfy_tests"] == ["test_app.py::test_expected", "test_app.py::test_first"]
    assert result["testmon_tests"] == result["vfy_tests"]
    assert len(cast(list[str], result["full_tests"])) == 3
    for label in ("baseline", "local-edit"):
        command = json.loads((tmp_path / f"artifacts/vfy-{label}-command.json").read_text())
        assert f"--engine={engine}" in command
        assert ("tests" in command) == (engine == "pytest-native")
    with pytest.raises(RuntimeError, match="valid execution record"):
        native_outcomes(
            tmp_path / "artifacts/vfy-outcomes.json",
            Path(sys.executable),
            project.root,
            engine="pytest-native" if engine == "native" else "native",
        )


def test_default_discovery_must_equal_the_full_manifest_oracle(tmp_path: Path) -> None:
    project, workload, config = selection_fixture(tmp_path / "project")
    project.write("tests/test_extra.py", "def test_extra():\n    assert True\n")
    workload = replace(workload, tests=("tests/test_app.py",))
    with pytest.raises(RuntimeError, match="default discovery differs"):
        full_oracle(
            project.root,
            Path(sys.executable),
            tmp_path / "artifacts",
            "baseline",
            workload,
            config,
            engine="native",
        )


@pytest.mark.skipif(sys.platform != "linux", reason="process-tree PSS requires Linux")
def test_sampled_testmon_keeps_outcome_gate_and_public_child_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready, release = tmp_path / "ready.pipe", tmp_path / "sampled.pipe"
    os.mkfifo(ready)
    os.mkfifo(release)
    collector = (
        Path(__file__).resolve().parents[2]
        / "validation/pytest_runner/benchmark_child_resources.py"
    )
    wrapper = tmp_path / "sample-ready-test.py"
    wrapper.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(collector.parent)!r})\n"
        "import benchmark_child_resources as collector\n"
        "sample_tree = collector._sample_tree\n"
        "released = False\n"
        "def sample_and_release(*args):\n"
        "    global released\n"
        "    if not released:\n"
        f"        with open({str(ready)!r}, 'rb') as stream:\n"
        "            assert stream.read(1) == b'r'\n"
        "    reading = sample_tree(*args)\n"
        "    if not released:\n"
        "        assert reading[2] > 0\n"
        f"        with open({str(release)!r}, 'wb') as stream:\n"
        "            stream.write(b'x')\n"
        "        released = True\n"
        "    return reading\n"
        "collector._sample_tree = sample_and_release\n"
        "raise SystemExit(collector.main())\n",
        encoding="utf-8",
    )

    def sample_ready_test(
        command: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return run_captured(
            [command[0], str(wrapper), *command[2:]], cwd=cwd, env=env, timeout=min(timeout, 15)
        )

    monkeypatch.setattr(benchmark_resources, "run_captured", sample_ready_test)
    # Delay actual sampling until the test body is ready, then hold it for the snapshot.
    body = (
        "def test_example():\n"
        f"    with open({str(ready)!r}, 'wb') as stream:\n"
        "        stream.write(b'r')\n"
        f"    with open({str(release)!r}, 'rb') as stream:\n"
        "        assert stream.read(1) == b'x'\n"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    test = tests / "test_example.py"
    test.write_text(body + "    assert True\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    record = artifacts / "resources/passing.json"
    elapsed, outcomes = run_testmon(
        tmp_path,
        Path(sys.executable),
        artifacts,
        "passing",
        manifest("platformdirs"),
        measure=True,
        resource_path=record,
    )
    resources = json.loads(record.read_text())
    assert resources["child_wall_ms"] == elapsed
    assert resources["child_exit_status"] == 0
    assert resources["peak_tree_pss_bytes"] > 0
    assert outcomes == sorted(
        [
            ("tests/test_example.py::test_example", phase, "passed", None)
            for phase in ("setup", "call", "teardown")
        ]
    )
    test.write_text(body + "    assert False, 'expected testmon failure'\n", encoding="utf-8")
    failed = artifacts / "resources/failing.json"
    with pytest.raises(RuntimeError, match="testmon failing failed with exit 1"):
        run_testmon(
            tmp_path,
            Path(sys.executable),
            artifacts,
            "failing",
            manifest("platformdirs"),
            measure=True,
            resource_path=failed,
        )
    failed_resources = json.loads(failed.read_text())
    assert failed_resources["child_exit_status"] == 1
    assert failed_resources["snapshot_count"] > 0
    assert failed_resources["peak_tree_pss_bytes"] > 0
    assert "AssertionError: expected testmon failure" in failed.with_suffix(".stdout").read_text()


def test_testmon_outcomes_must_match_full_oracle() -> None:
    oracle = [
        ("tests/test_a.py::test_a", "setup", "passed", None),
        ("tests/test_a.py::test_a", "call", "passed", None),
        ("tests/test_a.py::test_a", "teardown", "passed", None),
        ("tests/test_b.py::test_b", "call", "passed", None),
    ]
    assert compare_outcomes(oracle[:3], oracle) == ["test_a.py::test_a"]
    with pytest.raises(RuntimeError, match="differ"):
        compare_outcomes([(*oracle[1][:2], "failed", None)], oracle)


def test_aggregate_coverage_gate_discloses_context_differences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vfy_path = tmp_path / "vfy.coverage"
    oracle_path = tmp_path / "oracle.coverage"
    original = {
        "has_arcs": False,
        "contexts": ["test_a|call"],
        "by_context": {"test_a|call": {"pkg.py": {"lines": [1], "arcs": []}}},
        "files": {
            "pkg.py": {
                "lines": [1],
                "arcs": [],
                "tracer": None,
                "contexts": {1: ["test_a|call"]},
            }
        },
    }
    changed = {
        **original,
        "contexts": ["test_b|call"],
        "by_context": {"test_b|call": {"pkg.py": {"lines": [1], "arcs": []}}},
    }
    monkeypatch.setattr(
        "benchmarks.benchmark_real_selection.normalized_data",
        lambda path, root: original if path == vfy_path else changed,
    )
    result = check_coverage_stage(vfy_path, oracle_path, tmp_path, label="affected")
    assert result["aggregate_schema7"] == "exact"
    assert result["per_context_schema7"] == "differs"
    assert result["different_context_file_pairs"] == 2
    assert result["oracle_only_contexts"] == ["test_b|call"]
    changed["files"] = {"pkg.py": {"lines": [2], "arcs": [], "tracer": None, "contexts": {}}}
    with pytest.raises(RuntimeError, match="aggregate coverage data differs"):
        check_coverage_stage(vfy_path, oracle_path, tmp_path, label="affected")


def test_native_junit_must_account_for_executed_cases(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text('<testsuite><testcase name="a" /></testsuite>', encoding="utf-8")
    records = [("tests/test_a.py::test_a", "call", "passed", None)]
    check_native_junit(junit, records)
    with pytest.raises(RuntimeError, match="count differs"):
        check_native_junit(junit, [*records, ("tests/test_b.py::test_b", "call", "passed", None)])


def test_testmon_keeps_junit_destination_stable_between_baseline_and_edit(tmp_path: Path) -> None:
    project = manifest("platformdirs")
    junit = tmp_path / "testmon.xml"
    command = build_testmon_command(Path(sys.executable), project, junit)
    assert command == build_testmon_command(Path(sys.executable), project, junit)
    assert f"--junitxml={junit}" in command
    assert "--testmon" in command
    assert "--capture=no" in command


def test_platformdirs_selection_uses_its_testmon_lock() -> None:
    project = selection_project("platformdirs")
    assert project.dependency_lock == "benchmarks/locks/platformdirs-selection.txt"
    assert "pytest-testmon==2.2.0" in project.requirements
    assert "--capture=no" in project.coverage_args
    assert "--capture=no" in project.parallel_args


def test_reviewed_affected_target_must_be_selected() -> None:
    oracle = [
        ("tests/test_unix.py::test_user_media_dir_from_user_dirs_file", "call", "passed", None),
        ("tests/test_unix.py::test_other", "call", "passed", None),
    ]
    required = required_affected_nodes(oracle, PLATFORMDIRS_EDIT.tests)
    assert required == {"test_unix.py::test_user_media_dir_from_user_dirs_file"}
    with pytest.raises(RuntimeError, match="omitted reviewed affected tests"):
        check_required_selection(set(), required, "pytest-testmon")
    with pytest.raises(RuntimeError, match="omitted reviewed affected tests"):
        check_required_selection({"test_unix.py::test_other"}, required, "vfy")
    check_required_selection(required, required, "pytest-testmon")


def _rich_callable(line: str) -> tuple[Callable[[object], str], bytes]:
    namespace: dict[str, object] = {}
    exec(
        compile(
            f"class Stub:\n    def __init__(self, name): self.name = name\n"
            f"    def run(self):\n        {line}\n"
            f"def run(value): return Stub(value).run()\n",
            "<reviewed-rich-edit>",
            "exec",
        ),
        namespace,
    )
    stub = cast("type[object]", namespace["Stub"])
    method = cast("FunctionType", stub.__dict__["run"])
    return cast("Callable[[object], str]", namespace["run"]), method.__code__.co_code


def _platformdirs_callable(line: str) -> Callable[[str, str], str]:
    namespace: dict[str, object] = {}
    exec(
        compile(
            f"def run(prefix, value):\n    {line}\n",
            "<reviewed-platformdirs-edit>",
            "exec",
        ),
        namespace,
    )
    return cast("Callable[[str, str], str]", namespace["run"])


def test_reviewed_edits_change_bytecode_without_changing_behavior() -> None:
    rich_before, before_bytecode = _rich_callable(RICH_EDIT.before)
    rich_after, after_bytecode = _rich_callable(RICH_EDIT.after)
    assert before_bytecode != after_bytecode
    for value in (None, 0, False, "rich", object()):
        assert rich_before(value) == rich_after(value) == f"<emoji {value!r}>"

    platformdirs_before = _platformdirs_callable(PLATFORMDIRS_EDIT.before)
    platformdirs_after = _platformdirs_callable(PLATFORMDIRS_EDIT.after)
    assert (
        cast("FunctionType", platformdirs_before).__code__.co_code
        != cast("FunctionType", platformdirs_after).__code__.co_code
    )

    for prefix, value in (("", "/tmp"), ("/home/user", "/MyDocs"), ("/", "")):
        assert platformdirs_before(prefix, value) == platformdirs_after(prefix, value)


def test_rich_clock_fixture_is_scoped_in_prepared_conftest(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    conftest = tests / "conftest.py"
    conftest.write_text("import pytest\n", encoding="utf-8")
    record = install_rich_clock_fixture(tmp_path)
    assert record["path"] == "tests/conftest.py"
    prepared = conftest.read_text(encoding="utf-8")
    assert "def freeze_rich_log_clock(" in prepared
    assert "test_logging.py" in prepared
    assert "logging.setLogRecordFactory(original)" in prepared
    with pytest.raises(RuntimeError, match="already defines"):
        install_rich_clock_fixture(tmp_path)


def test_rich_adaptations_form_clean_deterministic_git_baseline(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    files = ("conftest.py", "test_console.py", "test_live.py", "test_traceback.py")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name in files:
        (tests / name).write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "tests"], cwd=tmp_path, check=True)
    identity = ["-c", "user.name=Benchmark Test", "-c", "user.email=test@invalid.local"]
    subprocess.run(["git", *identity, "commit", "-qm", "base"], cwd=tmp_path, check=True)
    upstream = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    for name in files:
        (tests / name).write_text("adapted\n", encoding="utf-8")
    first = commit_rich_test_adaptations(tmp_path)
    assert first != upstream
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=tmp_path)
    subprocess.run(
        ["git", "reset", "--hard", upstream], cwd=tmp_path, check=True, capture_output=True
    )
    for name in files:
        (tests / name).write_text("adapted\n", encoding="utf-8")
    assert commit_rich_test_adaptations(tmp_path) == first


def test_testmon_history_snapshot_includes_uncheckpointed_wal(tmp_path: Path) -> None:
    data = tmp_path / ".testmondata"
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sqlite3, sys; "
                "db = sqlite3.connect(sys.argv[1]); "
                "db.execute('PRAGMA journal_mode=WAL'); "
                "db.execute('CREATE TABLE executions (nodeid TEXT)'); "
                "db.execute(\"INSERT INTO executions VALUES ('tests/test_a.py::test_a')\"); "
                "db.commit(); os._exit(0)"
            ),
            str(data),
        ],
        check=True,
    )
    assert Path(f"{data}-wal").is_file()
    snapshot = tmp_path / "testmon.data"
    stash_testmon_data(tmp_path, snapshot)
    for suffix in ("", "-wal", "-shm", "-journal"):
        assert not Path(f"{data}{suffix}").exists()
    database = sqlite3.connect(snapshot)
    try:
        assert database.execute("SELECT nodeid FROM executions").fetchall() == [
            ("tests/test_a.py::test_a",)
        ]
    finally:
        database.close()
