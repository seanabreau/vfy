from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import cast

import pytest
from benchmarks import benchmark_projects, benchmark_workflows
from benchmarks.benchmark_projects import (
    CoverageSnapshot,
    GeneratedFiles,
    benchmark_environment,
    first_schema7_difference,
    git_value,
    normalized_data,
    resource_sequence,
    retain_parity_snapshot,
    run,
    validate_parity,
    validate_sample,
    write_partial_result,
)
from benchmarks.observer_plugins import OUTCOME_PLUGIN, stage_plugins
from benchmarks.project_manifest import ProjectManifest
from coverage import CoverageData
from scripts.process_tree import run_captured
from validation.pytest_runner.benchmark_child_resources import _rollup_bytes

from tests.support import VfyProject


@pytest.mark.parametrize(
    ("context", "override"),
    [(None, None), ("", None), ("none", None), ("test", "--cov-context=none")],
)
def test_independent_coverage_requires_test_context_before_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    context: str | None,
    override: str | None,
) -> None:
    artifacts = tmp_path / "artifacts"
    argv = [
        "benchmark_projects",
        "--project",
        "attrs",
        "--phase",
        "serial",
        "--vfy-engine",
        "native",
        "--root",
        str(tmp_path),
        "--artifacts",
        str(artifacts),
        "--validate-only",
        "--build-manifest",
        str(tmp_path / "original-build.json"),
    ]
    if context is not None:
        argv += ["--context", context]
    if override is not None:
        argv.append(f"--pytest-args={override}")
    monkeypatch.setattr(sys, "argv", argv)

    def unexpected_preparation(*_args: object, **_kwargs: object) -> dict[str, object]:
        pytest.fail("invalid context started environment inspection")

    monkeypatch.setattr(benchmark_projects, "selected_interpreter_metadata", unexpected_preparation)
    with pytest.raises(SystemExit, match="--context=test"):
        benchmark_projects.main()
    assert not artifacts.exists()


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "wrong-engine",
        "internal-error",
        "report",
        "junit-count",
        "junit-outcome",
        "missing-proof",
        "failed-proof",
        "changed-proof",
    ],
)
def test_independent_coverage_attempt_uses_real_oracle_and_rejects_changed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    """The actual owned CLI must preserve full public coverage and execution evidence."""
    from copy import deepcopy  # noqa: PLC0415 - retained XML corruption is test-local
    from functools import partial  # noqa: PLC0415 - one bound real harness invocation

    from scripts import process_tree  # noqa: PLC0415 - intercept only after a real child exits

    root = tmp_path / "project"
    project = xml_report_project(root)
    fixture = VfyProject(root)
    fixture.write(
        "package/__init__.py",
        "def answer(value):\n"
        "    if value:\n        return 42\n"
        "    return 0\n"
        "def never_called():\n    return -1\n",
    )
    fixture.write(
        "tests/test_answer.py",
        "import pytest\nfrom package import answer\n"
        "@pytest.fixture\ndef resource():\n"
        "    assert answer(False) == 0\n    yield 1\n"
        "    assert answer(False) == 0\n"
        "def test_answer(resource):\n    assert answer(resource) == 42\n",
    )
    fixture.write(".coveragerc", "[run]\nbranch = True\n")
    artifacts = tmp_path / "reference"
    environment = benchmark_environment(
        {"PYTHONPATH": str(stage_plugins(tmp_path / "observer-path"))}
    )
    expected_data, expected_outcomes = validate_parity(
        root,
        project,
        python=sys.executable,
        cov_config=root / ".coveragerc",
        extra_args=(),
        tests=project.tests,
        environment=environment,
        artifacts=artifacts,
        phases=("serial",),
        coverage_context="test",
    )
    commands = {
        name: benchmark_projects.sequence_commands(
            sys.executable,
            project,
            plugin=plugin,
            reports=("xml:coverage.xml", "json:coverage.json"),
            cov_config=root / ".coveragerc",
            extra_args=(),
            tests=project.tests,
            phases=("serial",),
            outcome_path=tmp_path / "unused.jsonl",
            coverage_context="test",
        )
        for name, plugin in (
            ("pytest-cov", "pytest_cov.plugin"),
            ("vfy-independent", "vfy.pytest_plugin"),
            ("plain pytest", None),
        )
    }
    attempt = partial(
        benchmark_projects.independent_coverage_attempt,
        root=root,
        environment=environment,
        expected_data=expected_data,
        expected_outcomes=expected_outcomes,
        reference_report=artifacts / ".vfy-benchmark-pytest-cov.json",
    )
    stock = attempt(
        "pytest-cov",
        commands["pytest-cov"],
        destination=tmp_path / "stock-preflight",
        expected_junit=None,
        preflight=True,
        execution_proof=None,
    )
    junit = json.loads(Path(str(stock["junit_reference"])).read_text())
    real_run = process_tree.run_captured

    def corrupt_after_real_child(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        completed = real_run(argv, cwd=cwd, env=env, timeout=timeout)
        if fault in {"wrong-engine", "internal-error"}:
            option = next(arg for arg in argv if arg.startswith("--native-result-json="))
            path = Path(option.partition("=")[2])
            raw = json.loads(path.read_text())
            if fault == "wrong-engine":
                raw["engine"] = "pytest-runner-native-v1"
            else:
                raw["internal_errors"] = [{"message": "injected with successful exit status"}]
            path.write_text(json.dumps(raw))
        elif fault == "report":
            option = next(arg for arg in argv if arg.startswith("--cov-report=json:"))
            path = Path(option.partition("json:")[2])
            raw = json.loads(path.read_text())
            raw["totals"]["covered_lines"] += 1
            path.write_text(json.dumps(raw))
        else:
            option = next(arg for arg in argv if arg.startswith("--junitxml="))
            path = Path(option.partition("=")[2])
            xml = ET.parse(path)
            suite = next(xml.getroot().iter("testsuite"))
            case = next(suite.iter("testcase"))
            if fault == "junit-count":
                suite.append(deepcopy(case))
            elif fault == "junit-outcome":
                ET.SubElement(case, "failure", {"message": "injected after child exit"})
            xml.write(path)
        return completed

    if fault in {"wrong-engine", "internal-error"}:
        monkeypatch.setattr(process_tree, "run_captured", corrupt_after_real_child)
        expected_error = ValueError if fault == "wrong-engine" else RuntimeError
        expected_message = (
            "vfy-independent-v1 result"
            if fault == "wrong-engine"
            else "preflight has internal errors"
        )
        with pytest.raises(
            expected_error,
            match=expected_message,
        ):
            attempt(
                "vfy-independent",
                commands["vfy-independent"],
                destination=tmp_path / "owned-preflight",
                expected_junit=junit,
                preflight=True,
                execution_proof=None,
            )
        return
    owned = attempt(
        "vfy-independent",
        commands["vfy-independent"],
        destination=tmp_path / "owned-preflight",
        expected_junit=junit,
        preflight=True,
        execution_proof=None,
    )
    proof = cast(dict[str, object] | None, owned["execution_proof"])
    assert proof is not None
    assert proof["passed"] is True
    assert expected_data["has_arcs"]
    assert any("|setup" in context for context in expected_data["contexts"])
    assert any("|run" in context for context in expected_data["contexts"])
    assert any("|teardown" in context for context in expected_data["contexts"])

    attempt(
        "plain pytest",
        commands["plain pytest"],
        destination=tmp_path / "plain-preflight",
        expected_junit=junit,
        preflight=True,
        execution_proof=None,
    )
    proof["preflight_passed"] = True
    if fault in {"missing-proof", "failed-proof", "changed-proof"}:

        def unexpected_child(*_args: object, **_kwargs: object) -> None:
            pytest.fail("invalid preflight evidence started a child")

        monkeypatch.setattr(process_tree, "run_captured", unexpected_child)
        if fault == "changed-proof":
            path = Path(str(proof["path"]))
            path.write_text(path.read_text() + "\n")
        elif fault == "failed-proof":
            proof["preflight_passed"] = False
        else:
            proof = None
        with pytest.raises(ValueError, match="preflight proof"):
            attempt(
                "vfy-independent",
                commands["vfy-independent"],
                destination=tmp_path / "sample",
                expected_junit=junit,
                preflight=False,
                execution_proof=proof,
            )
        assert not (tmp_path / "sample").exists()
        return
    if fault is not None:
        monkeypatch.setattr(process_tree, "run_captured", corrupt_after_real_child)
        with pytest.raises(RuntimeError, match=r"JSON coverage report|JUnit testcase multiset"):
            attempt(
                "vfy-independent",
                commands["vfy-independent"],
                destination=tmp_path / "sample",
                expected_junit=junit,
                preflight=False,
                execution_proof=proof,
            )
        assert json.loads((tmp_path / "sample/validation.json").read_text())["status"] == "failed"
        return
    for name in commands:
        observed = attempt(
            name,
            commands[name],
            destination=tmp_path / "sample" / name.replace(" ", "-"),
            expected_junit=junit,
            preflight=False,
            execution_proof=proof,
        )
        assert observed["status"] == "passed"
        assert not (Path(str(observed["directory"])) / "independent.json").exists()


def test_tree_rollup_treats_esrch_as_exited_address_space(monkeypatch: pytest.MonkeyPatch) -> None:
    fields = ["S", "1", "2", *(["0"] * 16), "42"]
    stat = f"123 (child) {' '.join(fields)}"

    def read_text(path: Path, **_kwargs: object) -> str:
        if path.name == "stat":
            return stat
        raise ProcessLookupError(errno.ESRCH, "address space exited")

    monkeypatch.setattr(Path, "read_text", read_text)
    assert _rollup_bytes(123, 42) is None


@pytest.mark.parametrize(
    ("rollup", "message"),
    [
        (FileNotFoundError("missing"), "smaps_rollup missing for live PID"),
        (PermissionError("denied"), "denied"),
        ("Pss: broken\nRss: 8 kB\n", "invalid Pss in smaps_rollup"),
    ],
)
def test_tree_rollup_rejects_stable_missing_or_malformed_data(
    monkeypatch: pytest.MonkeyPatch, rollup: BaseException | str, message: str
) -> None:
    fields = ["S", "1", "2", *(["0"] * 16), "42"]
    stat = f"123 (child) {' '.join(fields)}"

    def read_text(path: Path, **_kwargs: object) -> str:
        if path.name == "stat":
            return stat
        if isinstance(rollup, BaseException):
            raise rollup
        return rollup

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises((RuntimeError, PermissionError), match=message):
        _rollup_bytes(123, 42)


@pytest.mark.skipif(
    sys.platform != "linux", reason="the resource collector measures Linux RSS and PSS"
)
def test_serial_resource_sequence_records_kernel_peak_and_exact_child(tmp_path: Path) -> None:
    child = [sys.executable, "-c", "print('resource child ran')"]
    record_path = tmp_path / "evidence" / "sample-1-plain-pytest.json"
    elapsed, steps, record = resource_sequence(
        [("serial", child)],
        root=tmp_path,
        environment=dict(os.environ),
        record_path=record_path,
    )

    assert elapsed > 0
    assert steps == {"serial": elapsed}
    assert record["method"] == "linux-single-child-rusage"
    assert record["child_command"] == child
    assert record["child_exit_status"] == 0
    assert isinstance(record["peak_rss_bytes"], int)
    assert record["peak_rss_bytes"] > 0
    assert record_path.with_suffix(".stdout").read_text(encoding="utf-8").strip() == (
        "resource child ran"
    )
    with pytest.raises(ValueError, match="one serial"):
        resource_sequence(
            [("parallel", child)],
            root=tmp_path,
            environment=dict(os.environ),
            record_path=tmp_path / "parallel.json",
        )


@pytest.mark.skipif(
    sys.platform != "linux", reason="the resource collector measures Linux RSS and PSS"
)
def test_tree_resource_sequence_counts_concurrent_public_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampled = tmp_path / "sampled"
    ready = [tmp_path / name for name in ("left-ready", "right-ready")]
    # Release all allocations only after two actual readings of the three live processes.
    collector = (
        Path(benchmark_projects.__file__).resolve().parents[1]
        / "validation/pytest_runner/benchmark_child_resources.py"
    )
    wrapper = tmp_path / "collector.py"
    wrapper.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(collector.parent)!r})\n"
        "import benchmark_child_resources as collector\n"
        "sample_tree = collector._sample_tree\n"
        "live_readings = 0\n"
        "def sample_and_release(*args):\n"
        "    global live_readings\n"
        f"    ready = all(Path(path).exists() for path in {[str(path) for path in ready]!r})\n"
        "    reading = sample_tree(*args)\n"
        "    if ready and reading[2] == 3:\n"
        "        live_readings += 1\n"
        "        if live_readings == 2:\n"
        f"            Path({str(sampled)!r}).touch()\n"
        "    return reading\n"
        "collector._sample_tree = sample_and_release\n"
        "raise SystemExit(collector.main())\n",
        encoding="utf-8",
    )
    real_run = benchmark_projects.run

    def run_observed_collector(
        arguments: list[str], *, root: Path, environment: dict[str, str]
    ) -> None:
        assert arguments[:2] == [sys.executable, str(collector)]
        real_run([arguments[0], str(wrapper), *arguments[2:]], root=root, environment=environment)

    monkeypatch.setattr(benchmark_projects, "run", run_observed_collector)
    worker = (
        "import sys,time\n"
        "from pathlib import Path\n"
        "data=bytearray(16*1024*1024); data[::4096]=b'x'*4096\n"
        "Path(sys.argv[2]).touch()\n"
        "deadline = time.monotonic() + 10\n"
        "while not Path(sys.argv[1]).exists():\n"
        "    if time.monotonic() >= deadline:\n"
        "        raise RuntimeError('collector did not sample both live workers twice')\n"
        "    time.sleep(.01)\n"
    )
    parent = (
        "import subprocess,sys\n"
        "children = []\n"
        "try:\n"
        "    for ready in sys.argv[3:]:\n"
        "        children.append(subprocess.Popen(\n"
        "            [sys.executable, '-c', sys.argv[1], sys.argv[2], ready]))\n"
        "    for child in children:\n"
        "        assert child.wait(timeout=15) == 0\n"
        "finally:\n"
        "    for child in children:\n"
        "        if child.poll() is None:\n"
        "            child.kill()\n"
        "        child.wait(timeout=3)\n"
    )
    argv = [sys.executable, "-c", parent, worker, str(sampled), *(str(path) for path in ready)]
    record_path = tmp_path / "tree.json"
    outside_ready = tmp_path / "outside-ready"
    outside = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time\nfrom pathlib import Path\n"
            "data=bytearray(64*1024*1024); data[::4096]=b'x'*16384\n"
            "Path(sys.argv[1]).write_text('ready')\n"
            "deadline = time.monotonic() + 15\n"
            "while not Path(sys.argv[2]).exists():\n"
            "    if time.monotonic() >= deadline:\n"
            "        raise RuntimeError('collector did not acknowledge the live workers')\n"
            "    time.sleep(.01)\n",
            str(outside_ready),
            str(sampled),
        ],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not outside_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert outside_ready.exists()
        elapsed, steps, record = resource_sequence(
            [("serial", argv)],
            root=tmp_path,
            environment=dict(os.environ),
            record_path=record_path,
            tree=True,
        )
        assert outside.poll() in (None, 0)
    finally:
        outside.terminate()
        try:
            outside.wait(timeout=3)
        except subprocess.TimeoutExpired:
            outside.kill()
            outside.wait(timeout=3)

    assert elapsed > 0
    assert steps["serial"] == elapsed
    assert record["method"] == "linux-process-tree-smaps-rollup"
    assert record["child_command"] == argv
    assert record["child_exit_status"] == 0
    peak_count = record["peak_process_count"]
    snapshots = record["snapshot_count"]
    peak_pss = record["peak_tree_pss_bytes"]
    peak_rss = record["peak_tree_rss_bytes"]
    assert isinstance(peak_count, int)
    assert isinstance(snapshots, int)
    assert isinstance(peak_pss, int)
    assert isinstance(peak_rss, int)
    assert peak_count >= 3
    assert record["tracked_process_count"] == 3
    assert snapshots >= 2
    assert peak_pss >= 32 * 1024 * 1024
    assert peak_rss >= peak_pss
    assert record["tree_scope"] == (
        "sum of simultaneous live child and descendant PSS; collector excluded"
    )


@pytest.mark.skipif(
    sys.platform != "linux", reason="the resource collector measures Linux RSS and PSS"
)
def test_tree_resource_collector_preserves_public_child_exit_status(tmp_path: Path) -> None:
    collector = (
        Path(benchmark_projects.__file__).resolve().parents[1]
        / "validation/pytest_runner/benchmark_child_resources.py"
    )
    record = tmp_path / "failure.json"
    sampled = tmp_path / "sampled"
    wrapper = tmp_path / "collector.py"
    wrapper.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(collector.parent)!r})\n"
        "import benchmark_child_resources as collector\n"
        "sample_tree = collector._sample_tree\n"
        "def sample_and_release(*args):\n"
        "    reading = sample_tree(*args)\n"
        "    if reading[2]:\n"
        f"        Path({str(sampled)!r}).touch()\n"
        "    return reading\n"
        "collector._sample_tree = sample_and_release\n"
        "raise SystemExit(collector.main())\n",
        encoding="utf-8",
    )
    # The child exits only after an actual live reading, independent of sampler scheduling.
    child = (
        "import sys,time\n"
        "from pathlib import Path\n"
        "sampled = Path(sys.argv[1])\n"
        "deadline = time.monotonic() + 10\n"
        "while not sampled.exists():\n"
        "    if time.monotonic() >= deadline:\n"
        "        raise RuntimeError('collector did not sample the live child')\n"
        "    time.sleep(.01)\n"
        "raise SystemExit(7)\n"
    )
    finished = run_captured(
        [
            sys.executable,
            str(wrapper),
            "--tree",
            "--record",
            str(record),
            "--stdout",
            str(tmp_path / "stdout.txt"),
            "--stderr",
            str(tmp_path / "stderr.txt"),
            "--",
            sys.executable,
            "-c",
            child,
            str(sampled),
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=15,
    )
    assert finished.returncode == 7
    observed = json.loads(record.read_text(encoding="utf-8"))
    assert observed["child_exit_status"] == 7
    assert observed["snapshot_count"] >= 1


@pytest.mark.parametrize(
    ("mode", "warmups", "runs", "message"),
    [
        ("diagnostic", "1", "3", "selected project interpreter does not exist"),
        ("publication", "1", "3", "require at least 3 warmups and 15 runs"),
        ("diagnostic", "-1", "3", "require nonnegative warmups and positive runs"),
        ("diagnostic", "0", "0", "require nonnegative warmups and positive runs"),
    ],
)
def test_project_cli_allows_short_diagnostics_but_preserves_publication_counts(
    tmp_path: Path, mode: str, warmups: str, runs: str, message: str
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks.benchmark_projects",
            "--project",
            "rich",
            "--root",
            str(tmp_path),
            "--python",
            str(tmp_path / "missing-python"),
            "--output",
            str(tmp_path / "result.json"),
            "--mode",
            mode,
            "--warmups",
            warmups,
            "--runs",
            runs,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert message in completed.stderr


def xml_report_project(root: Path) -> ProjectManifest:
    root.mkdir()
    project = VfyProject(root)
    project.write("package/__init__.py", "def answer():\n    return 42\n")
    project.write(
        "tests/test_answer.py",
        "from package import answer\n\ndef test_answer():\n    assert answer() == 42\n",
    )
    return ProjectManifest(
        key="xml-fixture",
        name="XML report fixture",
        repository="example/xml-fixture",
        revision="a" * 40,
        package_path="package/",
        tests=("tests/test_answer.py",),
        coverage_args=("--cov=package",),
        reports=("xml:coverage.xml", "term-missing"),
        parallel_args=(),
        serial_args=(),
        plugins=(),
        environment=(),
        requirements=(),
        dependency_lock="requirements.txt",
        workers=1,
        serial_workers=0,
    )


def test_observed_stock_coverage_keeps_application_identity_with_missing_alias_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    project = xml_report_project(root)
    fixture = VfyProject(root)
    fixture.write("pytest.ini", "[pytest]\n")
    fixture.write(".coveragerc", "[run]\nbranch = true\n[paths]\nother =\n    .\n    */package\n")
    fixture.write(
        "tests/test_answer.py",
        "import coverage\nfrom package import answer\ndef test_answer():\n"
        "    assert coverage.Coverage.current()._collector.tracer_name() == 'CTracer'\n"
        "    assert answer() == 42\n",
    )
    outcomes = tmp_path / "outcomes.jsonl"
    data_file = tmp_path / "coverage-data"
    environment = benchmark_workflows.workflow_environment(
        project,
        Path(sys.executable),
        root=root,
        observer_root=stage_plugins(tmp_path / "observer-plugins"),
    )
    environment.update(COVERAGE_FILE=str(data_file), VFY_BENCHMARK_OUTCOMES=str(outcomes))
    run(
        benchmark_projects.command(
            sys.executable,
            "pytest_cov.plugin",
            project,
            reports=("",),
            cov_config=root / ".coveragerc",
            extra_args=(),
            tests=project.tests,
            phase="serial",
            outcome_path=outcomes,
        ),
        root=root,
        environment=environment,
    )
    data = CoverageData(basename=str(data_file))
    data.read()
    assert set(data.measured_files()) == {str(root / "package" / "__init__.py")}
    assert set(benchmark_projects.outcome_records(outcomes)) == {
        ("tests/test_answer.py::test_answer", phase, "passed", None)
        for phase in ("setup", "call", "teardown")
    }


def test_parity_validates_manifest_xml_reports_with_real_pytest_plugins(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    project = xml_report_project(root)
    artifacts = tmp_path / "artifacts"

    data, outcomes = validate_parity(
        root,
        project,
        python=sys.executable,
        cov_config=None,
        extra_args=(),
        tests=project.tests,
        environment=benchmark_environment(),
        artifacts=artifacts,
    )

    assert data["files"]
    assert any(outcome == "passed" for _nodeid, _phase, outcome, _xfail in outcomes)
    pytest_cov_xml = artifacts / ".vfy-benchmark-pytest-cov.xml"
    vfy_xml = artifacts / ".vfy-benchmark-vfy.xml"
    assert ET.parse(pytest_cov_xml).getroot().tag == "coverage"
    assert ET.parse(vfy_xml).getroot().tag == "coverage"


def test_parity_report_override_drops_unrequested_xml(tmp_path: Path) -> None:
    project = xml_report_project(tmp_path / "project")
    reports, xml_path = benchmark_projects.parity_report_arguments(
        project, "vfy", tmp_path, ("term", "json:coverage.json")
    )
    assert len(reports) == 1
    assert reports[0].startswith("json:")
    assert xml_path is None


def test_source_and_context_override_only_instrumented_command(tmp_path: Path) -> None:
    project = xml_report_project(tmp_path / "project")
    commands = benchmark_projects.sequence_commands(
        sys.executable,
        project,
        plugin="vfy.pytest_plugin",
        reports=("term",),
        cov_config=Path("/dev/null"),
        extra_args=(),
        tests=project.tests,
        coverage_context="test",
        source_override="package",
    )
    argv = commands[0][1]
    assert argv[argv.index("--cov-reset") + 1] == "--cov=package"
    assert "--cov-context=test" in argv
    plain = benchmark_projects.sequence_commands(
        sys.executable,
        project,
        plugin=None,
        reports=(),
        cov_config=None,
        extra_args=(),
        tests=project.tests,
        coverage_context="test",
        source_override="package",
    )[0][1]
    assert "--cov-context=test" not in plain
    assert "--cov-reset" not in plain


def test_project_scope_records_suite_exclusion(tmp_path: Path) -> None:
    project = xml_report_project(tmp_path / "project")
    assert (
        benchmark_projects.workload_scope(
            project,
            selected_tests=False,
            extra_args=("-k", "not test_connect_timeout"),
            qualification="four network timeout cases excluded",
        )
        == "full project suite; four network timeout cases excluded"
    )


def test_parity_rejects_xml_semantic_mismatch_from_real_pytest_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    project = xml_report_project(root)
    artifacts = tmp_path / "artifacts"
    real_run = benchmark_projects.run

    def alter_vfy_xml(
        arguments: list[str],
        *,
        root: Path,
        environment: dict[str, str],
        capture_output: bool = False,
        log_path: Path | None = None,
        timeout: float = 1800,
    ) -> None:
        real_run(
            arguments,
            root=root,
            environment=environment,
            capture_output=capture_output,
            log_path=log_path,
            timeout=timeout,
        )
        if "vfy.pytest_plugin" not in arguments:
            return
        xml_argument = next(
            argument for argument in arguments if argument.startswith("--cov-report=xml:")
        )
        xml_path = Path(xml_argument.removeprefix("--cov-report=xml:"))
        report_root = ET.parse(xml_path).getroot()
        covered = int(report_root.attrib["lines-covered"])
        report_root.set("lines-covered", str(covered + 1))
        ET.ElementTree(report_root).write(xml_path, encoding="utf-8", xml_declaration=True)

    monkeypatch.setattr(benchmark_projects, "run", alter_vfy_xml)
    with pytest.raises(RuntimeError, match="XML report output differs"):
        validate_parity(
            root,
            project,
            python=sys.executable,
            cov_config=None,
            extra_args=(),
            tests=project.tests,
            environment=benchmark_environment(),
            artifacts=artifacts,
        )


def test_timed_project_sequence_retains_each_pytest_phase(tmp_path: Path) -> None:
    (tmp_path / "test_case.py").write_text("def test_passes():\n    assert True\n")
    outcomes = tmp_path / "outcomes.jsonl"
    benchmark_projects.timed_sequence(
        [
            (
                "serial",
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-p",
                    OUTCOME_PLUGIN,
                    "-q",
                    "test_case.py",
                ],
            )
        ],
        root=tmp_path,
        environment=benchmark_environment(
            {"PYTHONPATH": str(stage_plugins(tmp_path.parent / f"{tmp_path.name}-observer-path"))}
        ),
        outcome_file=outcomes,
    )
    assert set(benchmark_projects.outcome_records(outcomes)) == {
        ("test_case.py::test_passes", phase, "passed", None)
        for phase in ("setup", "call", "teardown")
    }


def test_parity_restores_declared_generated_files_between_engines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = tmp_path / "generated.txt"
    absent = tmp_path / "absent.txt"
    generated.write_text("original", encoding="utf-8")
    generated.chmod(0o640)
    original_mode = generated.stat().st_mode & 0o777
    state = GeneratedFiles(tmp_path, ("generated.txt", "absent.txt"))
    observed: list[tuple[str, int, bool]] = []

    def fake_run(
        arguments: list[str],
        *,
        environment: dict[str, str],
        **_kwargs: object,
    ) -> None:
        observed.append(
            (
                generated.read_text(encoding="utf-8"),
                generated.stat().st_mode & 0o777,
                absent.exists(),
            )
        )
        generated.write_text("changed", encoding="utf-8")
        absent.write_text("created", encoding="utf-8")
        report = next(
            Path(argument.removeprefix("--cov-report=json:"))
            for argument in arguments
            if argument.startswith("--cov-report=json:")
        )
        report.write_text(
            json.dumps(
                {
                    "files": {"package/module.py": {"executed_lines": [1]}},
                    "totals": {},
                }
            ),
            encoding="utf-8",
        )
        Path(environment["VFY_BENCHMARK_OUTCOMES"]).write_text("", encoding="utf-8")

    project = ProjectManifest(
        key="example",
        name="Example",
        repository="example/example",
        revision="a" * 40,
        package_path="package/",
        tests=("tests",),
        coverage_args=("--cov=package",),
        reports=(),
        parallel_args=(),
        serial_args=(),
        plugins=(),
        environment=(),
        requirements=(),
        dependency_lock="requirements.txt",
        workers=1,
        serial_workers=0,
    )
    monkeypatch.setattr(benchmark_projects, "run", fake_run)
    monkeypatch.setattr(
        benchmark_projects,
        "normalized_data",
        lambda *_args, **_kwargs: {"same": True},
    )
    monkeypatch.setattr(benchmark_projects, "outcome_records", lambda _path: [])

    validate_parity(
        tmp_path,
        project,
        python=sys.executable,
        cov_config=None,
        extra_args=(),
        tests=project.tests,
        environment={},
        artifacts=tmp_path / "artifacts",
        restore_generated_files=state.restore,
    )

    assert observed == [("original", original_mode, False), ("original", original_mode, False)]
    assert generated.read_text(encoding="utf-8") == "original"
    assert generated.stat().st_mode & 0o777 == original_mode
    assert not absent.exists()


def test_generated_file_restore_validates_every_path_before_mutation(tmp_path: Path) -> None:
    root = tmp_path / "project"
    nested = root / "nested"
    outside = tmp_path / "outside"
    nested.mkdir(parents=True)
    outside.mkdir()
    safe = root / "safe.txt"
    safe.write_text("original", encoding="utf-8")
    state = GeneratedFiles(root, ("safe.txt", "nested/generated.txt"))

    safe.write_text("changed", encoding="utf-8")
    nested.rmdir()
    try:
        nested.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    with pytest.raises(RuntimeError, match="parent escaped"):
        state.restore()
    assert safe.read_text(encoding="utf-8") == "changed"
    assert not (outside / "generated.txt").exists()


def test_parity_restores_generated_files_after_engine_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = tmp_path / "generated.txt"
    state = GeneratedFiles(tmp_path, ("generated.txt",))

    def fail_after_mutation(*_args: object, **_kwargs: object) -> None:
        generated.write_text("created", encoding="utf-8")
        raise RuntimeError("engine failed")

    monkeypatch.setattr(benchmark_projects, "run", fail_after_mutation)
    project = benchmark_projects.PROJECTS["rich"]
    with pytest.raises(RuntimeError, match="engine failed"):
        validate_parity(
            tmp_path,
            project,
            python=sys.executable,
            cov_config=None,
            extra_args=(),
            tests=project.tests,
            environment={},
            artifacts=tmp_path / "artifacts",
            restore_generated_files=state.restore,
        )
    assert not generated.exists()


def test_benchmark_commands_ignore_ambient_collection_and_git_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poisoned = {
        "PYTEST_ADDOPTS": "--cov-append --unexpected-option",
        "COVERAGE_PROCESS_START": "missing.coveragerc",
        "COVERAGE_RCFILE": "missing.coveragerc",
        "PYTHONPATH": "wrong-package",
        "PYTHONHOME": "missing-python",
        "GIT_DIR": "missing-repository",
    }
    for key, value in poisoned.items():
        monkeypatch.setenv(key, value)
    environment = benchmark_environment()
    script = "import os; assert not any(key in os.environ for key in " + repr(list(poisoned)) + ")"
    run([sys.executable, "-c", script], root=tmp_path, environment=environment)
    subprocess.run(["git", "init", "-q", str(tmp_path)], env=environment, check=True)
    assert git_value(tmp_path, "rev-parse", "--is-inside-work-tree") == "true"


def test_parity_detects_swapped_arc_contexts_with_identical_line_attribution(
    tmp_path: Path,
) -> None:
    source = str(tmp_path / "app.py")
    forward = {(1, 2), (2, 3), (3, 1)}
    backward = {(1, 3), (3, 2), (2, 1)}
    paths = [tmp_path / "reference", tmp_path / "swapped"]
    for path, contexts in zip(paths, [(forward, backward), (backward, forward)], strict=True):
        data = CoverageData(str(path))
        for context, arcs in zip(["first", "second"], contexts, strict=True):
            data.set_context(context)
            data.add_arcs({source: arcs})
        data.write()

    reference, swapped = [normalized_data(path, tmp_path) for path in paths]
    assert reference != swapped
    first, second = [value["files"]["app.py"] for value in [reference, swapped]]
    # This regression preserves both the union and every line's contexts.
    assert first["arcs"] == second["arcs"]
    assert first["contexts"] == second["contexts"]


def test_parity_retains_file_tracer_identity(tmp_path: Path) -> None:
    source = str(tmp_path / "app.py")
    paths = [tmp_path / "reference", tmp_path / "changed"]
    for path, tracer in zip(paths, ["plugin.first", "plugin.second"], strict=True):
        data = CoverageData(str(path))
        data.add_lines({source: {1}})
        data.add_file_tracers({source: tracer})
        data.write()
    assert normalized_data(paths[0], tmp_path) != normalized_data(paths[1], tmp_path)


def test_timed_sample_must_preserve_validated_outcomes_and_data(tmp_path: Path) -> None:
    source = str(tmp_path / "app.py")
    data_path = tmp_path / ".coverage"
    data = CoverageData(str(data_path))
    data.set_context("test_app.py::test_value|run")
    data.add_lines({source: {1}})
    data.write()
    expected_data = normalized_data(data_path, tmp_path)
    outcomes = tmp_path / "outcomes.jsonl"
    outcomes.write_text(
        "\n".join(
            f'{{"nodeid":"test_app.py::test_value","when":"{phase}","outcome":"passed"}}'
            for phase in ("setup", "call", "teardown")
        )
        + "\n",
        encoding="utf-8",
    )
    expected_outcomes = [
        ("test_app.py::test_value", phase, "passed", None)
        for phase in ("call", "setup", "teardown")
    ]
    validate_sample(
        "vfy",
        root=tmp_path,
        outcome_file=outcomes,
        expected_outcomes=expected_outcomes,
        coverage_file=data_path,
        expected_data=expected_data,
        stage="sample",
    )

    data = CoverageData(str(data_path))
    data.add_lines({source: {2}})
    data.write()
    with pytest.raises(RuntimeError, match="sample changed schema-7 coverage data"):
        validate_sample(
            "vfy",
            root=tmp_path,
            outcome_file=outcomes,
            expected_outcomes=expected_outcomes,
            coverage_file=data_path,
            expected_data=expected_data,
            stage="sample",
        )


def test_parity_snapshot_keeps_gate_data_when_timed_files_change(tmp_path: Path) -> None:
    raw = tmp_path / ".vfy-benchmark-vfy"
    report = tmp_path / ".vfy-benchmark-vfy.json"
    raw.write_bytes(b"gate coverage data")
    report.write_text('{"files": {}}', encoding="utf-8")
    data = cast(CoverageSnapshot, {"files": {"app.py": {"contexts": {1: ["test|run"]}}}})
    outcomes = [("test_app.py::test_value", "call", "passed", None)]

    snapshot = retain_parity_snapshot(tmp_path, data, outcomes)
    raw.write_bytes(b"timed coverage data")
    report.write_text('{"files": {"changed.py": {}}}', encoding="utf-8")
    repeated_snapshot = retain_parity_snapshot(tmp_path, data, outcomes)

    assert (snapshot / raw.name).read_bytes() == b"gate coverage data"
    assert (repeated_snapshot / raw.name).read_bytes() == b"timed coverage data"
    assert repeated_snapshot.name == "parity-snapshot-2"
    assert json.loads((snapshot / report.name).read_text()) == {"files": {}}
    saved_data = json.loads((snapshot / "schema7.json").read_text())
    assert saved_data["files"]["app.py"]["contexts"]["1"] == ["test|run"]
    assert json.loads((snapshot / "outcomes.json").read_text()) == [list(outcomes[0])]


def test_schema7_difference_names_changed_file_and_context_line() -> None:
    expected = {"files": {"rich/_log_render.py": {"contexts": {64: ["first|run"]}}}}
    actual = {"files": {"rich/_log_render.py": {"contexts": {64: ["second|run"]}}}}
    difference = first_schema7_difference(expected, actual)
    assert "rich/_log_render.py" in difference
    assert "64" in difference
    assert "first|run" in difference
    assert "second|run" in difference


def test_partial_project_result_checkpoints_diagnostic_raw_samples(
    tmp_path: Path,
) -> None:
    output = tmp_path / "rich.json"
    metadata: dict[str, object] = {
        "benchmark": "projects",
        "mode": "diagnostic",
        "requested_mode": "publication",
        "project_commit": "a" * 40,
        "vfy_commit": "b" * 40,
        "commands": {"vfy": [{"phase": "parallel", "argv": ["python", "-m", "pytest"]}]},
        "environment": {"python": "3.13"},
        "parity": {"status": "exact", "artifact": "artifacts/parity.json"},
    }
    samples = {"pytest-cov": [12.5], "vfy": [10.0], "plain pytest": []}
    step_samples = {
        "pytest-cov": {"parallel": [12.4]},
        "vfy": {"parallel": [9.9]},
        "plain pytest": {"parallel": []},
    }

    partial_path = write_partial_result(
        output,
        metadata,
        warmups_requested=3,
        warmups_completed=3,
        runs_requested=15,
        samples=samples,
        step_samples=step_samples,
    )

    assert partial_path == output.with_suffix(".partial.json")
    first = json.loads(partial_path.read_text(encoding="utf-8"))
    assert first["status"] == "incomplete"
    assert first["diagnostic"] is True
    assert first["publication_ready"] is False
    assert first["mode"] == "diagnostic"
    assert first["requested_mode"] == "publication"
    assert first["sampling"] == {
        "warmups_requested": 3,
        "warmups_completed": 3,
        "measured_runs_requested_per_tool": 15,
        "completed_measured_runs_by_tool": {
            "pytest-cov": 1,
            "vfy": 1,
            "plain pytest": 0,
        },
    }
    assert first["raw_samples_ms"] == samples
    assert first["step_samples_ms"] == step_samples
    assert first["commands"] == metadata["commands"]
    assert first["environment"] == metadata["environment"]
    assert first["project_commit"] == metadata["project_commit"]
    assert first["parity"] == metadata["parity"]

    samples["plain pytest"].append(8.0)
    step_samples["plain pytest"]["parallel"].append(7.9)
    write_partial_result(
        output,
        metadata,
        warmups_requested=3,
        warmups_completed=3,
        runs_requested=15,
        samples=samples,
        step_samples=step_samples,
    )
    second = json.loads(partial_path.read_text(encoding="utf-8"))
    assert second["sampling"]["completed_measured_runs_by_tool"]["plain pytest"] == 1
    assert second["raw_samples_ms"]["plain pytest"] == [8.0]
    assert second["step_samples_ms"]["plain pytest"]["parallel"] == [7.9]


def test_ecosystem_failure_retains_both_streams_in_log(tmp_path: Path) -> None:
    probe = VfyProject(tmp_path).write(
        "failure.py",
        "import sys\nprint('assertion details')\n"
        "print('warning details', file=sys.stderr)\nsys.exit(1)\n",
    )
    log = tmp_path / "failure.log"
    with pytest.raises(RuntimeError):
        run(
            [sys.executable, str(probe)],
            root=tmp_path,
            environment=benchmark_environment(),
            log_path=log,
        )
    output = log.read_text(encoding="utf-8")
    assert "assertion details" in output
    assert "warning details" in output


def test_ecosystem_timeout_retains_both_streams_in_log(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    command = project.write(
        "hang.py",
        "import sys, time\n"
        "print('stdout-before-timeout', flush=True)\n"
        "print('stderr-before-timeout', file=sys.stderr, flush=True)\n"
        "time.sleep(30)\n",
    )
    log = tmp_path / "command.log"
    deadline = 5 if sys.platform == "win32" else 2
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        run(
            [sys.executable, str(command)],
            root=tmp_path,
            environment=dict(os.environ),
            log_path=log,
            timeout=deadline,
        )
    assert time.monotonic() - started < deadline + 15, "waited for the command's natural exit"
    assert failure.value.timeout == deadline
    output = log.read_text(encoding="utf-8")
    assert "stdout-before-timeout" in output
    assert "stderr-before-timeout" in output
