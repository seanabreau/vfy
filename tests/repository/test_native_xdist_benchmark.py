"""Distributed latency gates preserve full inventories and literal JUnit evidence."""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import pytest
from benchmarks.benchmark_harness import BenchmarkMode, SamplingConfig
from benchmarks.benchmark_provenance import SourceState
from validation.pytest_runner import (
    benchmark_native_xdist as benchmark,
    check_native_adoption_matrix as matrix,
)
from validation.pytest_runner.check_native_adoption_matrix import Project

from tests.support import VfyProject


def _iterator_warning_fixture(
    tmp_path: Path, engine: Literal["stock", "native"]
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    runner = tmp_path / "runner"
    filename = (
        runner
        / "lib/python3.12/site-packages"
        / ("_pytest/python.py" if engine == "stock" else "vfy/independent/protocol.py")
    )
    filename.parent.mkdir(parents=True, exist_ok=True)
    source_line = (
        "metafunc.parametrize(*marker.args, **marker.kwargs, _param_mark=marker)"
        if engine == "stock"
        else "items = operation.collect()"
    )
    line = 124 if engine == "stock" else 2
    filename.write_text(
        "\n" * 123 + source_line + "\n"
        if engine == "stock"
        else "def perform_collect():\n    " + source_line + "\n"
    )
    payload = (
        "Passing a non-Collection iterable to parametrize is deprecated.\n"
        "Test: test_panel.py::test_render_panel, argvalues type: zip\n"
        "Please convert to a list or tuple.\n"
        "See https://docs.pytest.org/en/stable/deprecations.html#parametrize-iterators"
    )
    metadata = {
        "nodeid": "",
        "category": "PytestRemovedIn10Warning",
        "raw_message": payload,
        "filename": str(filename),
        "lineno": line,
        "source_line": source_line,
        "message": f"{filename}:{line}: PytestRemovedIn10Warning: {payload}\n  {source_line}",
        "emitter_route": "builtin-marker-parametrize" if engine == "stock" else None,
    }
    python = runner / "bin/python"
    warning = matrix.normalize_session_warning(
        {key: metadata[key] for key in ("nodeid", "message", "filename", "lineno")},
        tmp_path / "project",
        python,
        tmp_path / "base",
    )
    return warning, metadata, python


def test_framework_iterator_warning_projection_preserves_payload_and_multiplicity(
    tmp_path: Path,
) -> None:
    results = []
    for engine in ("stock", "native"):
        warning, metadata, python = _iterator_warning_fixture(tmp_path / engine, engine)
        projected, receipts = benchmark.framework_warning_projection(
            [warning, warning],
            [metadata, metadata],
            tmp_path / engine / "project",
            python,
            tmp_path / engine / "base",
            engine=engine,
        )
        assert [row["index"] for row in receipts] == [0, 1]
        assert [row["raw_warning"] for row in receipts] == [warning, warning]
        assert projected[0]["category"] == "PytestRemovedIn10Warning"
        assert projected[0]["message"] == metadata["raw_message"]
        results.append(projected)
    assert results[0] == results[1]


@pytest.mark.parametrize("change", ["category", "payload", "origin", "rendering", "route"])
@pytest.mark.parametrize("engine", ["stock", "native"])
def test_framework_warning_projection_leaves_other_diagnostics_literal(
    tmp_path: Path, change: str, engine: Literal["stock", "native"]
) -> None:
    warning, metadata, python = _iterator_warning_fixture(tmp_path, engine)
    if change == "category":
        metadata["category"] = "UserWarning"
    elif change == "payload":
        metadata["raw_message"] += "\nadditional diagnostic"
    elif change == "origin":
        metadata["filename"] = str(tmp_path / "project/conftest.py")
        warning["filename"] = "conftest.py"
    elif change == "rendering":
        warning["message"] += "\ncustom warning formatter"
        metadata["message"] += "\ncustom warning formatter"
    elif engine == "stock":
        metadata["emitter_route"] = None
    else:
        Path(metadata["filename"]).write_text(
            "def direct_hook():\n    items = operation.collect()\n"
        )
    projected, receipts = benchmark.framework_warning_projection(
        [warning], [metadata], tmp_path / "project", python, tmp_path / "base", engine=engine
    )
    assert projected == [warning]
    assert receipts == []


def test_semantic_warning_proof_retains_truthful_raw_diagnostic_difference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = {}
    for engine in ("stock", "native"):
        side = tmp_path / engine
        warning, metadata, python = _iterator_warning_fixture(side, engine)
        projected, receipts = benchmark.framework_warning_projection(
            [warning], [metadata], side / "project", python, side / "base", engine=engine
        )
        records[engine] = {
            "cases": [],
            "observation_normalizations": [],
            "session_warnings": projected,
            "raw_session_warnings": [warning],
            "warning_origin_normalizations": receipts,
        }
    monkeypatch.setattr(
        benchmark, "independent_execution", lambda *args, **kwargs: records["native"]
    )
    output = tmp_path / "proof"
    benchmark.independent_proof(
        output,
        tmp_path / "project",
        python,
        tmp_path / "base",
        benchmark.Route("native", 1, False),
        records["stock"],
    )
    difference = json.loads((output / "execution-difference.json").read_text())
    assert difference["difference"] is None
    assert difference["raw_diagnostic_difference"] is not None
    for engine in ("stock", "native"):
        receipt = difference["warning_origin_normalizations"][engine][0]
        assert receipt["raw_warning"] == records[engine]["raw_session_warnings"][0]


@pytest.mark.skipif(sys.platform != "linux", reason="collected fork requires Linux")
def test_native_benchmark_workers_execute_with_explicit_fixture_plugin(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_cases.py",
        "import os\nfrom pathlib import Path\nimport pytest\n"
        "@pytest.mark.parametrize('value', [0, 1])\n"
        "def test_value(value, mocker):\n"
        "    function = mocker.Mock(return_value=value)\n"
        "    assert function() == value\n"
        "    function.assert_called_once_with()\n"
        "    Path(f'worker.{value}').write_text(str(os.getpid()))\n",
    )
    descriptor = Project("fixture", "", "", ".", ("tests",), (), plugins=("pytest_mock",))
    arguments = benchmark.command(
        descriptor,
        tmp_path,
        Path(sys.executable),
        tmp_path / "run",
        None,
        benchmark.Route("pytest-native", 2, False),
        capture="sys",
    )
    result = project.run(
        *arguments,
        env={
            "VFY_WORKER_START": "fork",
            "VFY_NATIVE_SCHEDULING": "cases",
        },
    )
    result.expect(0)
    assert len({(tmp_path / f"worker.{value}").read_text() for value in (0, 1)}) == 2
    assert (
        len(
            benchmark.adoption.junit_records(
                tmp_path / "run/junit.xml", tmp_path, tmp_path / "base"
            )
        )
        == 2
    )


@pytest.mark.parametrize("workers", [0, 2])
def test_public_stock_xdist_accepts_disabled_tmpdir_without_changing_upstream(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    config = project.write("pytest.ini", "[pytest]\naddopts = -p no:tmpdir -p no:legacypath\n")
    original = config.read_bytes()
    project.write(
        "tests/test_cases.py",
        "import pytest\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_value(value):\n"
        "    print('case', value)\n"
        "    assert value > 0\n",
    )
    descriptor = Project("fixture", "", "", ".", ("tests",), ())
    python = Path(sys.executable)
    temporary = benchmark.resolve_basetemp(descriptor, tmp_path, python, tmp_path / "config", 60)
    assert temporary is None
    arguments = benchmark.command(
        descriptor, tmp_path, python, tmp_path / "run", temporary, benchmark.Route("stock", workers)
    )
    result = project.run(*arguments)
    result.expect(0)
    cases = benchmark.adoption.junit_records(
        tmp_path / "run/junit.xml", tmp_path, tmp_path / "unused"
    )
    assert len(cases) == 2
    assert all(any(child["tag"] == "system-out" for child in case["children"]) for case in cases)
    assert config.read_bytes() == original


def test_explicit_capture_override_retains_upstream_addopts(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    config = project.write("pytest.ini", "[pytest]\naddopts = --capture=sys\n")
    descriptor = Project("fixture", "", "", ".", ("tests",), ())
    python = Path(sys.executable)
    with pytest.raises(ValueError, match="conflicts"):
        benchmark.resolve_basetemp(
            descriptor, tmp_path, python, tmp_path / "strict", 60, capture="no"
        )
    benchmark.resolve_basetemp(
        descriptor,
        tmp_path,
        python,
        tmp_path / "override",
        60,
        capture="no",
        override_upstream_capture=True,
    )
    assert "--capture=sys" in config.read_text()
    arguments = benchmark.command(
        descriptor,
        tmp_path,
        python,
        tmp_path / "run",
        None,
        benchmark.Route("stock", 4),
        capture="no",
    )
    assert "--capture=no" in arguments


def test_collection_skips_are_not_runtest_junit_cases(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("tests/test_run.py", "def test_runs():\n    assert True\n")
    project.write(
        "tests/test_optional.py", "import pytest\npytest.importorskip('missing_optional_module')\n"
    )
    result = project.run_python("-P", "-m", "pytest", "-q", "--junitxml=results.xml", "tests")
    result.expect(0)
    cases = benchmark.adoption.junit_records(tmp_path / "results.xml", tmp_path, tmp_path / "base")
    assert len(cases) == 2
    assert len(benchmark.runtest_junit_cases(cases)) == 1
    assert benchmark.runtest_junit_cases(cases)[0]["identity"][1] == "test_runs"


@pytest.mark.parametrize("workers", [0, 2, 4])
def test_equal_worker_commands_preserve_no_cov_and_identical_junit_options(
    tmp_path: Path, workers: int
) -> None:
    descriptor = Project(
        "fixture",
        "",
        "",
        ".",
        ("tests", "package"),
        (),
        stock_plugins=("pytest_cov.plugin",),
        outcome_args=("--no-cov",),
    )
    commands = {
        engine: benchmark.command(
            descriptor,
            tmp_path,
            Path(sys.executable),
            tmp_path / engine,
            None,
            benchmark.Route(engine, workers),
        )
        for engine in ("stock", "pytest-native")
    }
    for engine, arguments in commands.items():
        if engine == "pytest-native":
            assert "--engine=pytest-native" in arguments
        worker_option = "--numprocesses" if engine == "pytest-native" else "-n"
        assert arguments[arguments.index(worker_option) + 1] == str(workers)
        assert "--dist=load" in arguments
        assert "--no-cov" in arguments
        assert "junit_logging=all" in arguments
        assert "junit_log_passing_tests=true" in arguments
        assert arguments[-2:] == ["tests", "package"]
        assert "--basetemp" not in arguments
    assert "pytest_cov.plugin" in commands["stock"]
    assert "pytest_cov.plugin" not in commands["pytest-native"]


@pytest.mark.parametrize("key", ["black", "fastapi"])
def test_real_project_serial_worker_defaults_do_not_override_parallel_route(
    tmp_path: Path, key: str
) -> None:
    project = matrix.PROJECTS_BY_KEY[key]
    for route in (
        benchmark.Route("stock", 4),
        benchmark.Route("pytest-native", 4),
        benchmark.Route("pytest-native", 4, False),
    ):
        arguments = benchmark.command(
            project, tmp_path, Path(sys.executable), tmp_path / route.name, None, route
        )
        assert arguments[-len(project.tests) :] == list(project.tests)
        assert "-n0" not in arguments
        assert "--numprocesses=0" not in arguments
        if route.xdist:
            worker_option = "--numprocesses" if route.engine == "pytest-native" else "-n"
            assert arguments.count(worker_option) == 1
            assert arguments[arguments.index(worker_option) + 1] == "4"
        else:
            assert "-n" not in arguments
            assert "--workers=4" in arguments
        if key == "black":
            assert arguments[arguments.index("--run-optional") + 1] == "no_jupyter"


def test_rich_clock_fixture_is_loaded_for_each_parallel_route(tmp_path: Path) -> None:
    descriptor = Project("rich", "", "", ".", ("tests",), ())
    tests = tmp_path / "tests"
    tests.mkdir()
    conftest = tests / "conftest.py"
    conftest.write_text("import pytest\n")
    preparation = benchmark.serial.prepare_benchmark_inputs(descriptor, tmp_path, rich_clock=True)
    assert preparation["conftest"] == "tests/conftest.py"
    assert "def freeze_rich_log_clock(" in conftest.read_text()
    routes = (
        benchmark.Route("stock", 4),
        benchmark.Route("pytest-native", 4),
        benchmark.Route("pytest-native", 4, False),
    )
    for route in routes:
        arguments = benchmark.command(
            descriptor, tmp_path, Path(sys.executable), tmp_path / route.name, None, route
        )
        assert "rich_clock_plugin" not in arguments
        assert "junit_logging=all" in arguments
    assert (
        benchmark.serial.prepare_benchmark_inputs(
            Project("packaging", "", "", ".", ("tests",), ()), tmp_path, rich_clock=True
        )
        == {}
    )


def test_local_checkout_uses_clean_pinned_git_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "module.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(source), "add", "module.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Benchmark",
            "-c",
            "user.email=benchmark@example.invalid",
            "commit",
            "-qm",
            "pin",
        ],
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    project = Project("fixture", "", revision, ".", ("tests",), ())
    destination = benchmark.serial.checkout_local(project, tmp_path / "worktree", source)
    assert (destination / "module.py").read_text() == "value = 1\n"
    assert benchmark.serial.git(destination, "rev-parse", "HEAD") == revision
    (source / "module.py").write_text("value = 2\n")
    with pytest.raises(subprocess.CalledProcessError):
        benchmark.serial.checkout_local(project, tmp_path / "another", source)


def test_native_proof_rejects_duplicate_execution_even_when_case_count_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [tmp_path / f"native.json.gw{index}" for index in range(2)]
    for path in paths:
        path.write_text(json.dumps({"cases": [{"nodeid": "tests/test_case.py::test_first"}]}))
    monkeypatch.setattr(
        benchmark.adoption,
        "native_execution_records",
        lambda *_args, **_kwargs: [{"path": str(path)} for path in paths],
    )
    monkeypatch.setattr(
        benchmark.matrix, "normalize_native", lambda raw, *_args: (raw["cases"], [])
    )
    with pytest.raises(ValueError, match="executed node IDs differ"):
        benchmark.native_proof(
            tmp_path,
            tmp_path,
            Path(sys.executable),
            tmp_path,
            benchmark.Route("pytest-native", 2),
            [],
            ["tests/test_case.py::test_first", "tests/test_case.py::test_second"],
        )


def test_sample_capture_mismatch_fails_gate_and_omits_preflight_instrumentation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = Project("fixture", "", "", ".", ("tests",), ())
    route = benchmark.Route("pytest-native", 2)
    expected = [{"identity": ["test_case", "test_value"], "children": []}]
    monkeypatch.setattr(benchmark.serial, "restore_project", lambda *_args: None)
    monkeypatch.setattr(
        benchmark.adoption,
        "junit_records",
        lambda *_args: [
            {**expected[0], "children": [{"tag": "system-out", "text": "changed capture"}]}
        ],
    )

    def invoke(
        arguments: list[str], _root: Path, environment: dict[str, str], *_args: object
    ) -> dict[str, Any]:
        assert "PYTEST_NATIVE_RESULT" not in environment
        assert "VFY_PYTEST_RECORD_SESSION" not in environment
        assert "pytest_execution_probe" not in arguments
        assert "--history" in arguments
        return {"exit_status": 0, "seconds": 1.0}

    monkeypatch.setattr(benchmark.matrix, "invoke", invoke)
    with pytest.raises(ValueError, match="JUnit testcase multiset changed"):
        benchmark.sample(
            descriptor,
            tmp_path,
            tmp_path,
            Path(sys.executable),
            tmp_path / "samples",
            None,
            [route],
            {route.name: expected},
            SamplingConfig(0, 1, BenchmarkMode.DIAGNOSTIC),
            60,
            resources=False,
        )


def test_distributed_stock_cannot_skip_work_from_the_serial_inventory(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    project = VfyProject(snapshot)
    project.write(
        "tests/test_cases.py",
        "import pytest\n"
        "def test_value(request):\n"
        "    if hasattr(request.config, 'workerinput'):\n"
        "        pytest.skip('worker-only skip')\n"
        "    assert True\n",
    )
    descriptor = Project("fixture", "", "", ".", ("tests",), ())
    root = tmp_path / "project"
    output = tmp_path / "preflight"
    allowed, _expected, proof = benchmark.preflight(
        descriptor,
        root,
        snapshot,
        Path(sys.executable),
        output,
        tmp_path / "pytest-tmp",
        [benchmark.Route("stock", 0), benchmark.Route("stock", 2)],
        tmp_path / "unused-stock-wheel.whl",
        60,
    )
    assert allowed == []
    assert proof["stock-xdist-0"]["passed"]
    assert proof["stock-xdist-0"]["cases"] == proof["stock-xdist-2"]["cases"] == 1
    assert proof["stock-xdist-2"]["passed"] is False
    assert proof["stock-xdist-2"]["errors"] == [
        "distributed stock inventory or outcomes differ from serial stock"
    ]


@pytest.mark.parametrize("option", ["--workers", "--independent-workers"])
def test_duplicate_worker_selection_is_rejected_before_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark",
            "--wheel",
            "unused.whl",
            "--build-manifest",
            "unused.json",
            "--work-root",
            str(tmp_path / "work"),
            "--output",
            str(tmp_path / "output"),
            "--project",
            "packaging",
            option,
            "2",
            "2",
        ],
    )
    with pytest.raises(SystemExit) as error:
        benchmark.main()
    assert error.value.code == 2
    assert not (tmp_path / "work").exists()
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("timed_evidence", ["full", "junit"])
def test_independent_benchmark_runs_explicit_engine_and_gates_each_sample(
    tmp_path: Path,
    workers: int,
    timed_evidence: Literal["full", "junit"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "snapshot"
    project = VfyProject(snapshot)
    project.write(
        "conftest.py",
        "import pytest\n"
        "@pytest.fixture\ndef resource():\n"
        "    print('setup output')\n"
        "    yield 1\n"
        "    print('teardown output')\n",
    )
    project.write(
        "tests/test_cases.py",
        "import pytest\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_value(value, resource):\n"
        "    print('call output', value)\n"
        f"    print('x' * {70000 if workers == 1 else 1})\n"
        "    assert value >= resource\n"
        "@pytest.mark.skip(reason='benchmark skip')\n"
        "def test_skip(): pass\n",
    )
    descriptor = Project("fixture", "", "", ".", ("tests",), ())
    root, output = tmp_path / "project", tmp_path / "preflight"
    route = benchmark.Route("native", workers, False)
    references = [benchmark.Route("stock", 0)]
    if workers > 1:
        references.append(benchmark.Route("stock", workers))
    routes = [*references, route]
    allowed, expected, proof = benchmark.preflight(
        descriptor,
        root,
        snapshot,
        Path(sys.executable),
        output,
        tmp_path / "pytest-tmp",
        routes,
        tmp_path / "unused-stock-wheel.whl",
        60,
        capture="sys",
    )
    assert all(row["passed"] for row in proof.values()), proof
    assert route in allowed
    if workers == 1:
        assert proof[route.name]["independent_execution"]["path"].endswith(".gz")
    timing_routes = [
        next(reference for reference in references if reference.name == route.reference),
        route,
    ]
    execution_expected = {
        route.name: {
            **proof[route.name]["independent_execution"],
            "preflight_passed": proof[route.name]["passed"],
        }
    }

    def run_samples(metadata: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return benchmark.sample(
            descriptor,
            root,
            snapshot,
            Path(sys.executable),
            tmp_path / "samples",
            tmp_path / "pytest-tmp",
            timing_routes,
            expected,
            SamplingConfig(0, 1, BenchmarkMode.DIAGNOSTIC),
            60,
            resources=False,
            capture="sys",
            execution_expected=metadata,
            timed_evidence=timed_evidence,
        )

    invalid_proofs = [
        {},
        {route.name: {**execution_expected[route.name], "preflight_passed": False}},
        {route.name: {**execution_expected[route.name], "retained_sha256": "0" * 64}},
    ]
    for invalid in invalid_proofs:
        with monkeypatch.context() as guard:

            def reject_sample_start(*_args: object) -> None:
                pytest.fail("sampling started without valid full preflight proof")

            guard.setattr(benchmark.serial, "restore_project", reject_sample_start)
            with pytest.raises(ValueError, match="preflight phase parity"):
                run_samples(invalid)
    result = run_samples(execution_expected)
    assert result["timed_evidence"] == timed_evidence
    assert result["independent_per_sample_phase_validation"] == (timed_evidence == "full")
    for record in result["samples"]:
        assert record["per_sample_phase_validation"] == (
            record["route"] == route.name and timed_evidence == "full"
        )
    assert result["comparisons"][route.name]["reference"] == route.reference
    candidate = tmp_path / "samples" / f"sample-00-{route.name}"
    arguments = json.loads((candidate / "command.json").read_text())["argv"]
    assert "--engine=native" in arguments
    assert ("--history" in arguments) == (timed_evidence == "full")
    assert any(arg.startswith("--native-result-json=") for arg in arguments) == (
        timed_evidence == "full"
    )
    raw_path = candidate / "independent.json"
    if timed_evidence == "junit":
        assert not raw_path.exists()
        assert not raw_path.with_suffix(".json.gz").exists()
        candidate_record = next(row for row in result["samples"] if row["route"] == route.name)
        assert "history.json" not in candidate_record["artifacts"]
        return
    if raw_path.exists():
        raw = json.loads(raw_path.read_text())
    else:
        with gzip.open(raw_path.with_suffix(".json.gz"), "rt", encoding="utf-8") as stream:
            raw = json.load(stream)
    assert raw["engine"] == "vfy-independent-v1"
    raw["engine"] = "pytest-runner-native-v1"
    raw_path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="vfy-independent-v1"):
        benchmark.independent_execution(
            candidate, root, Path(sys.executable), tmp_path / "pytest-tmp", route
        )


def test_independent_benchmark_rejects_phase_difference_hidden_by_junit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "snapshot"
    project = VfyProject(snapshot)
    project.write("tests/test_cases.py", "def test_value(): assert True\n")
    descriptor = Project("fixture", "", "", ".", ("tests",), ())
    original = benchmark.independent_execution

    def altered_execution(
        output: Path,
        root: Path,
        python: Path,
        basetemp: Path,
        route: benchmark.Route,
        *,
        project: Project | None = None,
        require_active_workers: bool = False,
    ) -> dict[str, Any]:
        execution = original(
            output,
            root,
            python,
            basetemp,
            route,
            project=project,
            require_active_workers=require_active_workers,
        )
        execution["cases"][0]["reports"][0]["exception"] = "unreported setup difference"
        return execution

    monkeypatch.setattr(benchmark, "independent_execution", altered_execution)
    route = benchmark.Route("native", 1, False)
    allowed, expected, proof = benchmark.preflight(
        descriptor,
        tmp_path / "project",
        snapshot,
        Path(sys.executable),
        tmp_path / "preflight",
        tmp_path / "pytest-tmp",
        [benchmark.Route("stock", 0), route],
        tmp_path / "unused-stock-wheel.whl",
        60,
    )
    assert not allowed
    assert proof[route.name]["errors"] == [
        "independent phase records or warnings differ from full stock"
    ]
    assert (
        benchmark.adoption.junit_records(
            tmp_path / "preflight" / route.name / "junit.xml",
            tmp_path / "project",
            tmp_path / "pytest-tmp",
        )
        == expected["stock-xdist-0"]
    )


def test_failed_project_parity_returns_nonzero_harness_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark",
            "--wheel",
            "unused.whl",
            "--build-manifest",
            "unused.json",
            "--work-root",
            str(tmp_path / "work"),
            "--output",
            str(tmp_path / "output"),
            "--project",
            "click",
            "--workers",
            "0",
            "--independent-workers",
            "1",
            "--timing-route",
            "stock-xdist-0",
            "--timing-route",
            "native-j1",
            "--validate-only",
        ],
    )
    monkeypatch.setattr(benchmark, "require_clean", lambda _root: SourceState("a" * 40, False))

    def fail_parity(*_args: object) -> None:
        raise ValueError("independent phase records differ")

    monkeypatch.setattr(benchmark, "run_project", fail_parity)
    assert benchmark.main() == 1
    result = json.loads((tmp_path / "output/result.json").read_text())
    assert result["projects"][0]["status"] == "blocked"
    assert "independent phase records differ" in result["projects"][0]["error"]


def test_rejected_independent_route_cannot_measure_surviving_native_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark",
            "--wheel",
            "unused.whl",
            "--build-manifest",
            "unused.json",
            "--work-root",
            str(tmp_path / "work"),
            "--output",
            str(tmp_path / "output"),
            "--project",
            "click",
            "--workers",
            "0",
            "--independent-workers",
            "2",
        ],
    )
    monkeypatch.setattr(benchmark, "require_clean", lambda _root: SourceState("a" * 40, False))
    monkeypatch.setattr(
        benchmark.serial,
        "prepare_runtime",
        lambda *_args: (tmp_path / "project", tmp_path / "snapshot", Path(sys.executable), {}),
    )
    monkeypatch.setattr(benchmark, "resolve_basetemp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        benchmark,
        "preflight",
        lambda *_args, **_kwargs: (
            [benchmark.Route("pytest-native", 0)],
            {},
            {
                "stock-xdist-0": {"passed": True},
                "pytest-native-xdist-0": {"passed": True},
                "stock-xdist-2": {"passed": True},
                "native-j2": {"passed": False},
            },
        ),
    )
    monkeypatch.setattr(
        benchmark, "sample", lambda *_args, **_kwargs: pytest.fail("invalid timing started")
    )
    assert benchmark.main() == 1
    result = json.loads((tmp_path / "output/result.json").read_text())
    assert "native-j2" in result["projects"][0]["error"]


@pytest.mark.parametrize("key", ["click", "attrs"])
def test_xfail_addresses_use_reviewed_rules_in_preflight_and_samples(
    tmp_path: Path,
    key: str,
) -> None:
    snapshot = tmp_path / "snapshot"
    project = VfyProject(snapshot)
    if key == "click":
        project.write(
            "tests/test_chain.py",
            "import pytest\n"
            "class CliRunner:\n    __module__ = 'click.testing'\n"
            "@pytest.fixture\ndef runner(): return CliRunner()\n"
            "@pytest.mark.xfail\ndef test_group_chaining(runner): assert False\n",
        )
        expected_rules = {"clirunner-address"}
    else:
        project.write("tests/__init__.py", "")
        project.write(
            "tests/test_setattr.py",
            "import attr, pytest\nfrom attr import setters\n"
            "class TestSetAttr:\n"
            "    @pytest.mark.xfail(raises=attr.exceptions.FrozenAttributeError)\n"
            "    def test_slotted_confused(self):\n"
            "        @attr.s(slots=True)\n"
            "        class A:\n            x = attr.ib(on_setattr=setters.frozen)\n"
            "        class B(A): pass\n"
            "        @attr.s(slots=True)\n"
            "        class C(B):\n            x = attr.ib()\n"
            "        C(1).x = 2\n",
        )
        expected_rules = {
            "test-instance-address",
            "failed-instance-address",
            "setattr-function-address",
        }
    descriptor = Project(key, "", matrix.PROJECTS_BY_KEY[key].revision, ".", ("tests",), ())
    root, output, basetemp = tmp_path / "project", tmp_path / "preflight", tmp_path / "pytest-tmp"
    route = benchmark.Route("native", 1, False)
    routes = [benchmark.Route("stock", 0), route]
    allowed, expected, proof = benchmark.preflight(
        descriptor,
        root,
        snapshot,
        Path(sys.executable),
        output,
        basetemp,
        routes,
        tmp_path / "unused-stock-wheel.whl",
        60,
    )
    assert all(row["passed"] for row in proof.values()), proof
    normalized = json.loads(Path(proof[route.name]["independent_execution"]["path"]).read_text())
    assert {rule["rule"] for rule in normalized["observation_normalizations"]} == expected_rules
    assert all(rule["occurrences"] == 1 for rule in normalized["observation_normalizations"])
    raw = json.loads((output / route.name / "independent.json").read_text())
    assert "object at 0x" in raw["cases"][0]["phases"][1]["detail"]
    result = benchmark.sample(
        descriptor,
        root,
        snapshot,
        Path(sys.executable),
        tmp_path / "samples",
        basetemp,
        allowed,
        expected,
        SamplingConfig(0, 1, BenchmarkMode.DIAGNOSTIC),
        60,
        resources=False,
        execution_expected={
            route.name: {
                **proof[route.name]["independent_execution"],
                "preflight_passed": proof[route.name]["passed"],
            }
        },
    )
    assert len(result["samples"]) == 2


@pytest.mark.parametrize("drift", ["outcome", "count", "status"])
def test_junit_timing_rejects_real_execution_drift_after_full_preflight(
    tmp_path: Path, drift: str
) -> None:
    snapshot = tmp_path / "snapshot"
    project = VfyProject(snapshot)
    project.write("tests/test_cases.py", "def test_body(): assert True\n")
    descriptor = Project("fixture", "", "", ".", ("tests",), ())
    root, output, basetemp = tmp_path / "project", tmp_path / "preflight", tmp_path / "pytest-tmp"
    route = benchmark.Route("native", 1, False)
    _allowed, expected, proof = benchmark.preflight(
        descriptor,
        root,
        snapshot,
        Path(sys.executable),
        output,
        basetemp,
        [benchmark.Route("stock", 0), route],
        tmp_path / "unused-stock-wheel.whl",
        60,
    )
    assert all(row["passed"] for row in proof.values()), proof
    source = "import pytest\ndef test_body():\n    print('deliberate benchmark drift')\n"
    if drift == "outcome":
        source += "    pytest.skip('changed outcome')\n"
    elif drift == "count":
        source += "    assert True\ndef test_extra(): pass\n"
    else:
        source += "    assert False, 'changed status'\n"
    project.write("tests/test_cases.py", source)
    with pytest.raises(ValueError, match="sample status or JUnit testcase multiset changed"):
        benchmark.sample(
            descriptor,
            root,
            snapshot,
            Path(sys.executable),
            tmp_path / "samples",
            basetemp,
            [route],
            expected,
            SamplingConfig(0, 1, BenchmarkMode.DIAGNOSTIC),
            60,
            resources=False,
            timed_evidence="junit",
            execution_expected={
                route.name: {
                    **proof[route.name]["independent_execution"],
                    "preflight_passed": proof[route.name]["passed"],
                }
            },
        )
    junit = tmp_path / "samples" / f"sample-00-{route.name}" / "junit.xml"
    assert "deliberate benchmark drift" in junit.read_text()


@pytest.mark.parametrize(
    "topology",
    [
        None,
        {"requested_workers": 2, "active_workers": 1, "worker_pids": [11]},
        {"requested_workers": 2, "active_workers": 2, "worker_pids": [11, 11]},
        {"requested_workers": 2, "active_workers": 2, "worker_pids": [11, 0]},
        {"requested_workers": 4, "active_workers": 2, "worker_pids": [11, 12]},
    ],
)
def test_independent_preflight_rejects_missing_or_inactive_workers(
    tmp_path: Path, topology: object
) -> None:
    (tmp_path / "independent.json").write_text(json.dumps({"execution_topology": topology}))
    with pytest.raises(ValueError, match=r"topology is missing|did not activate"):
        benchmark.independent_execution(
            tmp_path,
            tmp_path,
            Path(sys.executable),
            tmp_path,
            benchmark.Route("native", 2, False),
            require_active_workers=True,
        )
