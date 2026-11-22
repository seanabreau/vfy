"""Public command-line checks for the shared benchmark harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import benchmarks.cli as benchmark_cli
import pytest
from benchmarks import (
    benchmark_evidence,
    benchmark_projects as project_benchmark,
    benchmark_provenance,
    benchmark_static_report as static_benchmark,
    prepare_projects as project_preparation,
)
from benchmarks.benchmark_harness import BenchmarkMode, SamplingConfig, benchmark_environment
from benchmarks.benchmark_provenance import SourceState
from benchmarks.cli import (
    _merge_sampled_payloads,
    _parser,
    compare_results,
    main as benchmark_main,
    write_comparison_artifacts,
)
from benchmarks.project_manifest import manifest
from coverage import CoverageData

import vfy

ROOT = Path(__file__).resolve().parents[2]


def test_compare_parser_preserves_revision_workload_arguments() -> None:
    parsed = _parser().parse_args(
        [
            "compare",
            "--mode",
            "diagnostic",
            "--benchmark",
            "report",
            "--base-revision",
            "a" * 40,
            "--",
            "--files",
            "2",
            "--shards",
            "1",
        ]
    )
    assert parsed.paths == ["--", "--files", "2", "--shards", "1"]


def test_benchmark_cli_lists_and_runs_report_diagnostic(tmp_path: Path) -> None:
    installed = tmp_path / "site-packages"
    shadow = installed / "benchmarks"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text(
        'raise RuntimeError("unrelated installed benchmark package")\n', encoding="utf-8"
    )
    environment = benchmark_environment({"PATH": os.defpath, "PYTHONPATH": str(installed)})
    listed = subprocess.run(
        [sys.executable, "-m", "benchmarks", "list"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert listed.returncode == 0, listed.stderr
    assert "report\tcoverage collection and reports" in listed.stdout
    assert "static-report\tprepared source analysis and reports" in listed.stdout

    help_result = subprocess.run(
        [sys.executable, "-m", "benchmarks", "validate", "static-report", "--help"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--data-file" in help_result.stdout

    output = tmp_path / "report.json"
    measured = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks",
            "run",
            "report",
            "--mode",
            "diagnostic",
            "--files",
            "2",
            "--shards",
            "2",
            "--report-formats",
            "terminal",
            "json",
            "--warmups",
            "0",
            "--runs",
            "1",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert measured.returncode == 0, measured.stderr
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["benchmark"] == "report"
    assert result["methodology"]["mode"] == "diagnostic"
    assert result["methodology"]["report_formats"] == ["terminal", "json"]
    assert result["provenance"]["workload"]["files"] == 2
    assert result["provenance"]["workload"]["report_formats"] == ["terminal", "json"]
    benchmark_names = {item["name"] for item in result["benchmarks"]}
    assert "terminal report" in benchmark_names
    assert "JSON report" in benchmark_names
    assert "XML report" not in benchmark_names
    assert "line collection" in benchmark_names
    assert "branch collection" in benchmark_names
    assert any(name.startswith("combine ") for name in benchmark_names)
    assert "pytest line collection + terminal report" in benchmark_names
    assert "pytest branch collection + terminal report" in benchmark_names
    assert result["provenance"]["source"]["revision"]


def test_direct_report_entrypoint_works_from_a_clean_cwd(tmp_path: Path) -> None:
    environment = benchmark_environment({"PATH": os.defpath})
    completed = subprocess.run(
        [sys.executable, str(ROOT / "benchmarks" / "benchmark_report.py"), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage: benchmark_report.py" in completed.stdout


def test_evidence_adapter_accepts_diagnostic_forwarding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_evidence",
            "--comparison-revision",
            "a" * 40,
            "--output",
            str(tmp_path / "evidence.json"),
            "--diagnostic",
        ],
    )
    args = benchmark_evidence.parse_args()
    assert args.mode == "diagnostic"


@pytest.mark.parametrize("spec", benchmark_cli.SPECS, ids=lambda item: item.name)
def test_dispatch_forwards_only_declared_modes_and_validation(
    monkeypatch: pytest.MonkeyPatch, spec: benchmark_cli.BenchmarkSpec
) -> None:
    captured: list[str] = []

    class FakeModule:
        def main(self) -> int:
            captured.extend(sys.argv[1:])
            return 0

    monkeypatch.setattr(
        benchmark_cli.importlib,
        "import_module",
        lambda name: FakeModule() if name == spec.module else None,
    )
    diagnostic_args = ["--mode", "diagnostic", "--warmups", "0", "--runs", "1"]
    if spec.mode_forwarding is not benchmark_cli.ModeForwarding.UNSUPPORTED:
        assert benchmark_cli._invoke(spec, "run", diagnostic_args) == 0
        if spec.mode_forwarding is benchmark_cli.ModeForwarding.DIAGNOSTIC_FLAG:
            assert "--diagnostic" in captured
            assert "--mode" not in captured
        else:
            assert "--diagnostic" not in captured
            assert captured[captured.index("--mode") + 1] == "diagnostic"
    else:
        with pytest.raises(SystemExit, match="does not support diagnostic mode"):
            benchmark_cli._invoke(spec, "run", diagnostic_args)

    captured.clear()
    if spec.supports_validation:
        assert benchmark_cli._invoke(spec, "validate", []) == 0
        assert "--validate-only" in captured
        if spec.mode_forwarding is benchmark_cli.ModeForwarding.DIAGNOSTIC_FLAG:
            assert "--diagnostic" in captured
            assert "--mode" not in captured
        elif spec.mode_forwarding is benchmark_cli.ModeForwarding.MODE_OPTION:
            assert captured[captured.index("--mode") + 1] == "diagnostic"
        else:
            assert "--diagnostic" not in captured
            assert "--mode" not in captured
    else:
        with pytest.raises(SystemExit, match="does not support validation-only"):
            benchmark_cli._invoke(spec, "validate", [])


# The name `benchmark` would collide with pytest-benchmark's fixture.
@pytest.mark.parametrize("workload", ["mutation", "edit-loop"])
def test_legacy_workload_help_has_no_import_or_forwarding_failure(workload: str) -> None:
    environment = benchmark_environment({"PATH": os.defpath})
    completed = subprocess.run(
        [sys.executable, "-m", "benchmarks", "run", workload, "--help"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout


@pytest.mark.parametrize(
    "command",
    [
        ["run", "mutation", "--mode", "diagnostic", "--help"],
        ["validate", "mutation", "--help"],
    ],
)
def test_mutation_supports_declared_cli_capabilities(command: list[str]) -> None:
    environment = benchmark_environment({"PATH": os.defpath})
    completed = subprocess.run(
        [sys.executable, "-m", "benchmarks", *command],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout


def test_validate_dispatches_mutation_scoped_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    class FakeModule:
        def main(self) -> int:
            captured.extend(sys.argv[1:])
            return 0

    monkeypatch.setattr(
        benchmark_cli.importlib,
        "import_module",
        lambda name: FakeModule() if name == "benchmarks.benchmark_mutation" else None,
    )
    spec = next(item for item in benchmark_cli.SPECS if item.name == "mutation")
    assert benchmark_cli._invoke(spec, "validate", ["--datasette-only"]) == 0
    assert captured == ["--datasette-only", "--diagnostic", "--validate-only"]


def test_static_report_cli_round_trip_consumes_project_validation_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exercise prepare, project-data production, static timing, and compare dispatch."""

    prepared = tmp_path / "prepared"
    environment = prepared / "environment"
    metadata = prepared / "artifacts"
    project_root = prepared / "project"
    source = project_root / "rich" / "__init__.py"
    selected_python = Path(sys.executable).absolute()

    def fake_prepare(project: object, destination: Path, **kwargs: object) -> None:
        assert getattr(project, "key", None) == "rich"
        assert kwargs["environment"] == environment
        assert kwargs["metadata"] == metadata
        assert kwargs["python"] == "python3.12"
        destination.mkdir(parents=True, exist_ok=True)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("value = 1\nprint(value)\n", encoding="utf-8")
        environment.mkdir()
        metadata.mkdir()
        (metadata / "manifest.json").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(project_preparation, "prepare", fake_prepare)
    assert (
        benchmark_main(
            [
                "prepare",
                "projects",
                "rich",
                str(prepared),
                "--environment",
                str(environment),
                "--metadata",
                str(metadata),
                "--python",
                "python3.12",
            ]
        )
        == 0
    )
    assert source.is_file()
    assert environment.is_dir()
    assert metadata.is_dir()

    validation_artifacts = metadata / "baseline"
    database = validation_artifacts / ".vfy-benchmark-pytest-cov"

    wheel_revision = "d" * 40
    extension_hash = "e" * 64
    wheel_hash = "f" * 64

    def fake_selected_interpreter(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "python": "3.12.0",
            "vfy": "test",
            "pytest_cov": "test",
            "extension_sha256": extension_hash,
            "build_profile": "release",
        }

    def fake_preparation_metadata(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "manifest": str(metadata / "manifest.json"),
            "vfy_wheel": {
                "source_revision": wheel_revision,
                "native_profile": "release",
                "extension_sha256": extension_hash,
                "wheel_sha256": wheel_hash,
                "source_root": str(tmp_path),
            },
        }

    def fake_project_provenance(
        _root: Path, *, workload: object, **_kwargs: object
    ) -> dict[str, object]:
        return {
            "source": {"revision": "a" * 40, "dirty": False},
            "environment": {
                "platform": "test-platform",
                "machine": "test-machine",
                "python": "3.12.0",
                "tools": {"python": "3.12.0"},
            },
            "workload": workload,
            "validation": {"status": "passed"},
        }

    def fake_validate_parity(
        root: Path,
        _project: object,
        *,
        artifacts: Path | None = None,
        **_kwargs: object,
    ) -> tuple[project_benchmark.CoverageSnapshot, list[object]]:
        assert root == project_root
        assert artifacts == validation_artifacts
        validation_artifacts.mkdir(parents=True, exist_ok=True)
        coverage_data = CoverageData(str(database))
        coverage_data.add_lines({str(source): {1, 2}})
        coverage_data.write()
        return project_benchmark.normalized_data(database, root), []

    monkeypatch.setattr(
        project_benchmark, "selected_interpreter_metadata", fake_selected_interpreter
    )
    monkeypatch.setattr(project_benchmark, "preparation_metadata", fake_preparation_metadata)
    monkeypatch.setattr(project_benchmark, "prepared_project_provenance", fake_project_provenance)
    monkeypatch.setattr(project_benchmark, "validate_parity", fake_validate_parity)
    monkeypatch.setattr(project_benchmark, "git_value", lambda *_args: "a" * 40)
    assert (
        benchmark_main(
            [
                "validate",
                "projects",
                "--project",
                "rich",
                "--root",
                str(project_root),
                "--python",
                str(selected_python),
                "--preparation-manifest",
                str(metadata / "manifest.json"),
                "--artifacts",
                str(validation_artifacts),
            ]
        )
        == 0
    )
    assert database.is_file()
    default = json.loads(capsys.readouterr().out)
    assert default["phases"] == ["parallel"]
    assert default["commands"]["pytest-cov"][0]["phase"] == "parallel"
    assert (
        benchmark_main(
            [
                "validate",
                "projects",
                "--project",
                "fastapi",
                "--phase",
                "serial",
                "--root",
                str(project_root),
                "--python",
                str(selected_python),
                "--preparation-manifest",
                str(metadata / "manifest.json"),
                "--artifacts",
                str(validation_artifacts),
            ]
        )
        == 0
    )
    serial = json.loads(capsys.readouterr().out)
    assert serial["phases"] == ["serial"]
    assert serial["workload"]["phases"] == ["serial"]
    assert serial["provenance"]["workload"]["phases"] == ["serial"]
    for commands in serial["commands"].values():
        assert [command["phase"] for command in commands] == ["serial"]
        argv = commands[0]["argv"]
        assert argv[argv.index("-n") + 1] == "0"

    monkeypatch.setattr(
        static_benchmark,
        "_preparation_record",
        lambda *args, **kwargs: (
            {
                "project": "rich",
                "revision": manifest("rich").revision,
                "python": str(selected_python),
                "manifest": str(metadata / "manifest.json"),
                "vfy_wheel": {
                    "source_revision": "d" * 40,
                    "native_profile": "release",
                    "extension_sha256": "e" * 64,
                    "wheel_sha256": "f" * 64,
                    "source_root": str(tmp_path),
                },
            },
            {"extension_sha256": "e" * 64},
        ),
    )
    monkeypatch.setattr(
        static_benchmark,
        "require_clean",
        lambda _path: SourceState("a" * 40, False),
    )
    monkeypatch.setattr(static_benchmark, "require_revision", lambda *_args: None)
    monkeypatch.setattr(
        static_benchmark,
        "_initial_report_parity",
        lambda _root, data_file: {"files": {}, "totals": {"covered_lines": 2}},
    )
    consumed_paths: list[Path] = []

    def fake_sample_reports(
        **kwargs: object,
    ) -> tuple[dict[str, list[float]], dict[str, object], list[str]]:
        data_file = kwargs["data_file"]
        assert isinstance(data_file, Path)
        assert data_file == database
        assert data_file.is_file()
        consumed_paths.append(data_file)
        summary = {
            "samples_ms": [1.0],
            "median_ms": 1.0,
            "mean_ms": 1.0,
            "stdev_ms": 0.0,
            "min_ms": 1.0,
            "max_ms": 1.0,
        }
        return (
            {"coverage.py": [1.0], "vfy": [1.0]},
            {"coverage.py": dict(summary), "vfy": dict(summary)},
            [],
        )

    monkeypatch.setattr(static_benchmark, "_sample_reports", fake_sample_reports)
    revisions = iter(("b" * 40, "c" * 40))

    def fake_provenance(_root: Path, *, workload: object, **_kwargs: object) -> dict[str, object]:
        return {
            "source": {"revision": next(revisions), "dirty": False},
            "environment": {
                "platform": "test-platform",
                "machine": "test-machine",
                "python": "3.12.0",
                "tools": {"python": "3.12.0"},
            },
            "workload": workload,
            "validation": {"status": "passed"},
        }

    monkeypatch.setattr(static_benchmark, "prepared_project_provenance", fake_provenance)
    outputs = [tmp_path / "baseline.json", tmp_path / "candidate.json"]
    for output in outputs:
        assert (
            benchmark_main(
                [
                    "run",
                    "static-report",
                    "--mode",
                    "diagnostic",
                    "--project",
                    "rich",
                    "--prepared",
                    str(prepared),
                    "--root",
                    str(project_root),
                    "--python",
                    str(selected_python),
                    "--preparation-manifest",
                    str(metadata / "manifest.json"),
                    "--data-file",
                    str(database),
                    "--warmups",
                    "0",
                    "--runs",
                    "1",
                    "--artifacts",
                    str(tmp_path / "raw"),
                    "--output",
                    str(output),
                ]
            )
            == 0
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["benchmark"] == "static-report"
        assert payload["database"]["path"] == str(database)

    assert consumed_paths == [database, database]
    capsys.readouterr()
    assert (
        benchmark_main(["compare", "--mode", "diagnostic", str(outputs[1]), str(outputs[0])]) == 0
    )
    comparison = json.loads(capsys.readouterr().out)
    assert comparison["records"]


def test_static_report_diagnostic_runs_real_source_and_report_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the static workload against a clean fixture and real schema-7 data."""

    prepared = tmp_path / "prepared"
    project_root = prepared / "project"
    source = project_root / "rich" / "__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class Contract:\n"
        "    def render(\n"
        "        self,\n"
        "    ) -> str:  # pragma: no cover\n"
        "        ...\n\n"
        "# adjacent comment\n"
        "value = 1\n",
        encoding="utf-8",
    )
    (project_root / ".coveragerc").write_text(
        "[report]\nexclude_lines =\n    pragma: no cover\n",
        encoding="utf-8",
    )
    for command in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "benchmark@example.invalid"],
        ["git", "config", "user.name", "Benchmark Fixture"],
        ["git", "add", "."],
        ["git", "commit", "--quiet", "-m", "fixture"],
    ):
        subprocess.run(command, cwd=project_root, check=True, capture_output=True, text=True)

    data_file = prepared / "artifacts" / "baseline" / ".vfy-benchmark-pytest-cov"
    data_file.parent.mkdir(parents=True)
    coverage_data = CoverageData(str(data_file))
    coverage_data.add_lines({str(source.resolve()): {1, 8}})
    coverage_data.write()

    selected_python = Path(sys.executable).absolute()
    monkeypatch.setattr(
        static_benchmark,
        "_preparation_record",
        lambda *_args, **_kwargs: (
            {
                "project": "rich",
                "revision": manifest("rich").revision,
                "python": str(selected_python),
                "manifest": str(prepared / "artifacts" / "manifest.json"),
                "vfy_wheel": {
                    "source_revision": "d" * 40,
                    "native_profile": "release",
                    "extension_sha256": "e" * 64,
                    "wheel_sha256": "f" * 64,
                    "source_root": str(tmp_path),
                },
            },
            {"extension_sha256": "e" * 64},
        ),
    )
    monkeypatch.setattr(
        static_benchmark,
        "prepared_project_provenance",
        lambda *_args, **_kwargs: {"validation": {"status": "passed"}},
    )
    output = tmp_path / "static-report.json"
    result = static_benchmark._run(
        argparse.Namespace(
            project="rich",
            prepared=prepared,
            root=project_root,
            python=selected_python,
            preparation_manifest=prepared / "artifacts" / "manifest.json",
            data_file=data_file,
            source=None,
            warmups=0,
            runs=1,
            mode="diagnostic",
            diagnostic=True,
            validate_only=False,
            artifacts=tmp_path / "retained",
            output=output,
        )
    )

    assert result["benchmark"] == "static-report"
    methodology = result.get("methodology")
    assert isinstance(methodology, dict)
    assert methodology.get("mode") == "diagnostic"
    source_analysis = result.get("source_analysis")
    assert isinstance(source_analysis, dict)
    assert source_analysis.get("files") == 1
    reports = result.get("reports")
    assert isinstance(reports, dict)
    for name in ("coverage.py", "vfy"):
        report = reports.get(name)
        assert isinstance(report, dict)
        samples = report.get("samples_ms")
        assert isinstance(samples, list)
        assert samples
    database = result.get("database")
    assert isinstance(database, dict)
    assert isinstance(database.get("sha256"), str)
    assert output.is_file()


def _static_publication_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[argparse.Namespace, Path]:
    """Build a prepared project with a real wheel binding for publication tests."""

    prepared = tmp_path / "prepared"
    project_root = prepared / "project"
    source = project_root / "rich" / "__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\nvalue += 1\n", encoding="utf-8")
    for command in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "benchmark@example.invalid"],
        ["git", "config", "user.name", "Benchmark Fixture"],
        ["git", "add", "."],
        ["git", "commit", "--quiet", "-m", "fixture"],
    ):
        subprocess.run(command, cwd=project_root, check=True, capture_output=True, text=True)
    checked_out_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=project_root, text=True
    ).strip()

    data_file = prepared / "artifacts" / "baseline" / ".vfy-benchmark-pytest-cov"
    data_file.parent.mkdir(parents=True)
    coverage_data = CoverageData(str(data_file))
    coverage_data.add_lines({str(source.resolve()): {1, 2}})
    coverage_data.write()

    extension = next(
        path.resolve()
        for path in sorted(Path(vfy.__file__).parent.glob("_native.*"))
        if path.suffix in {".so", ".pyd"}
    )
    extension_sha256 = hashlib.sha256(extension.read_bytes()).hexdigest()
    wheel = tmp_path / "vfy.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.write(extension, f"vfy/{extension.name}")
    wheel_sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
    vfy_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    original_source_state = benchmark_provenance.source_state

    def publication_source_state(root: Path) -> SourceState:
        # Keep the artifact checks independent of edits in the controller checkout.
        if root.resolve() == ROOT:
            return SourceState(vfy_revision, False)
        return original_source_state(root)

    monkeypatch.setattr(benchmark_provenance, "source_state", publication_source_state)
    build = {
        "source_root": str(ROOT),
        "source_revision": vfy_revision,
        "native_profile": "release",
        "extension_sha256": extension_sha256,
        "wheel_path": str(wheel),
        "wheel_sha256": wheel_sha256,
        "source_inputs": {},
    }
    build_manifest = prepared / "vfy-build.json"
    build_manifest.write_text(json.dumps(build) + "\n", encoding="utf-8")
    preparation_manifest = prepared / "artifacts" / "manifest.json"
    preparation_manifest.write_text(
        json.dumps(
            {
                "project": "rich",
                "revision": manifest("rich").revision,
                "python": str(Path(sys.executable).absolute()),
                "checked_out_revision": checked_out_revision,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return (
        argparse.Namespace(
            project="rich",
            prepared=prepared,
            root=project_root,
            python=Path(sys.executable).absolute(),
            preparation_manifest=preparation_manifest,
            data_file=data_file,
            source=None,
            warmups=3,
            runs=15,
            mode="publication",
            diagnostic=False,
            validate_only=False,
            artifacts=tmp_path / "retained",
            output=tmp_path / "static-report.json",
        ),
        build_manifest,
    )


def test_static_report_publication_consumes_prepared_provenance_without_env_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publication crosses the runner boundary with only prepared provenance."""

    args, build_manifest = _static_publication_fixture(tmp_path, monkeypatch)
    monkeypatch.delenv("VFY_BENCHMARK_BUILD_MANIFEST", raising=False)

    result = static_benchmark._run(args)

    provenance = result["provenance"]
    assert isinstance(provenance, dict)
    environment = provenance["environment"]
    assert isinstance(environment, dict)
    artifact = environment["artifact"]
    assert isinstance(artifact, dict)
    assert artifact["build_manifest"] == str(build_manifest)
    methodology = result["methodology"]
    assert isinstance(methodology, dict)
    assert methodology["mode"] == "publication"


def test_static_report_rejects_build_manifest_change_after_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publication rejects a prepared build manifest changed after measurement."""

    args, build_manifest = _static_publication_fixture(tmp_path, monkeypatch)
    monkeypatch.delenv("VFY_BENCHMARK_BUILD_MANIFEST", raising=False)
    original_sample_reports = static_benchmark._sample_reports

    def sample_then_tamper(
        *,
        args: argparse.Namespace,
        root: Path,
        raw: Path,
        data_file: Path,
        expected: dict[str, object],
        sampling: SamplingConfig,
    ) -> tuple[dict[str, list[float]], dict[str, object], list[str]]:
        result = original_sample_reports(
            args=args,
            root=root,
            raw=raw,
            data_file=data_file,
            expected=expected,
            sampling=sampling,
        )
        build = json.loads(build_manifest.read_text(encoding="utf-8"))
        build["extension_sha256"] = "0" * 64
        build_manifest.write_text(json.dumps(build) + "\n", encoding="utf-8")
        return result

    monkeypatch.setattr(static_benchmark, "_sample_reports", sample_then_tamper)

    with pytest.raises(RuntimeError, match="prepared vfy build changed"):
        static_benchmark._run(args)
    assert json.loads(build_manifest.read_text(encoding="utf-8"))["extension_sha256"] == "0" * 64
    assert not args.output.is_file()


def _result(path: Path, revision: str) -> None:
    path.write_text(
        json.dumps(
            {
                "benchmark": "report",
                "methodology": {"validation": "passed", "command": ["report"]},
                "provenance": {
                    "source": {"revision": revision, "dirty": False},
                    "validation": {"status": "passed"},
                    "workload": {"files": 2},
                    "environment": {
                        "platform": "test-platform",
                        "machine": "test-machine",
                        "python": "3.12.0",
                        "tools": {"python": "3.12.0", "coverage": "7.0"},
                    },
                },
                "benchmarks": {
                    "vfy": {
                        "median_ms": 999.0,
                        "mean_ms": 999.0,
                        "stdev_ms": 999.0,
                        "min_ms": 999.0,
                        "max_ms": 999.0,
                        "samples_ms": [2.0, 2.0],
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _publication_result(path: Path, revision: str, *, cpu: str = "test-cpu") -> None:
    _result(path, revision)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["methodology"] = {
        "mode": "publication",
        "warmups": 3,
        "measured_runs": 15,
        "parity": "passed",
    }
    timing = payload["benchmarks"]["vfy"]
    timing["samples_ms"] = [2.0 + index / 100 for index in range(15)]
    payload["provenance"]["engine_source"] = {"revision": revision}
    payload["provenance"]["environment"].update(
        {
            "cpu": cpu,
            "logical_cpus": 8,
            "memory_bytes": 16 * 1024**3,
            "python_implementation": "CPython",
            "python_build": "main test-build",
            "python_abi": "cpython-312",
            "python_debug": False,
            "python_gil": "enabled",
        }
    )
    payload["provenance"]["environment"]["artifact"] = {
        "native_profile": "release",
        "build_manifest": "/tmp/manifest.json",
        "wheel_sha256": "a" * 64,
        "extension_sha256": "b" * 64,
        "source_revision": revision,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_compare_recomputes_raw_summaries_and_emits_chart(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.json"
    baseline = tmp_path / "baseline.json"
    _result(candidate, "a" * 40)
    _result(baseline, "b" * 40)
    report = compare_results(candidate, baseline, mode=BenchmarkMode.DIAGNOSTIC)
    records = report["records"]
    summary = report["summary"]
    assert isinstance(records, dict)
    assert isinstance(summary, dict)
    vfy = records["benchmarks.vfy"]
    assert isinstance(vfy, dict)
    assert vfy["uncertainty_kind"] == "independent"
    assert summary["chart_labels"] == ["benchmarks.vfy"]
    artifacts = write_comparison_artifacts(report, tmp_path / "presentation")
    assert Path(artifacts["markdown"]).read_text(encoding="utf-8").startswith("| Workload")
    assert Path(artifacts["svg"]).read_text(encoding="utf-8").startswith("<svg")


def test_compare_writes_chart_for_zero_samples(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.json"
    baseline = tmp_path / "baseline.json"
    for path, revision in ((candidate, "a" * 40), (baseline, "b" * 40)):
        _result(path, revision)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["benchmarks"]["vfy"]["samples_ms"] = [0.0, 0.0]
        path.write_text(json.dumps(payload), encoding="utf-8")
    report = compare_results(candidate, baseline, mode=BenchmarkMode.DIAGNOSTIC)
    artifacts = write_comparison_artifacts(report, tmp_path / "presentation")
    assert 'width="0.00"' in Path(artifacts["svg"]).read_text(encoding="utf-8")


def test_revision_blocks_merge_raw_samples_and_sampling_counts() -> None:
    merged = _merge_sampled_payloads(
        [
            {
                "methodology": {"warmups": 3, "measured_runs": 15},
                "benchmarks": {
                    "vfy": {
                        "median_ms": 999.0,
                        "mean_ms": 999.0,
                        "stdev_ms": 999.0,
                        "min_ms": 999.0,
                        "max_ms": 999.0,
                        "samples_ms": [1.0, 3.0],
                    }
                },
            },
            {
                "methodology": {"warmups": 3, "measured_runs": 15},
                "benchmarks": {
                    "vfy": {
                        "median_ms": 999.0,
                        "mean_ms": 999.0,
                        "stdev_ms": 999.0,
                        "min_ms": 999.0,
                        "max_ms": 999.0,
                        "samples_ms": [5.0, 7.0],
                    }
                },
            },
        ]
    )
    methodology = merged["methodology"]
    assert isinstance(methodology, dict)
    assert methodology["warmups"] == 6
    assert methodology["measured_runs"] == 30
    benchmarks = merged["benchmarks"]
    assert isinstance(benchmarks, dict)
    vfy = benchmarks["vfy"]
    assert isinstance(vfy, dict)
    assert vfy["samples_ms"] == [1.0, 3.0, 5.0, 7.0]
    assert vfy["median_ms"] == 4.0
    assert merged["sampling_blocks"] == 2


def test_revision_blocks_merge_timing_records_inside_lists() -> None:
    def block(sample: float) -> dict[str, object]:
        return {
            "benchmarks": [
                {
                    "vfy": {
                        "median_ms": sample,
                        "mean_ms": sample,
                        "stdev_ms": 0.0,
                        "min_ms": sample,
                        "max_ms": sample,
                        "samples_ms": [sample],
                    }
                }
            ]
        }

    merged = _merge_sampled_payloads([block(1.0), block(3.0)])
    benchmarks = merged["benchmarks"]
    assert isinstance(benchmarks, list)
    timing = benchmarks[0]["vfy"]
    assert isinstance(timing, dict)
    assert timing["samples_ms"] == [1.0, 3.0]
    assert timing["median_ms"] == 2.0


def test_revision_blocks_reject_changed_identity_or_validation(tmp_path: Path) -> None:
    payloads: list[dict[str, object]] = []
    for revision in ("a" * 40, "a" * 40):
        path = tmp_path / f"benchmark-block-{revision[:4]}-{len(payloads)}.json"
        _publication_result(path, revision)
        payloads.append(json.loads(path.read_text(encoding="utf-8")))
    provenance = payloads[1].get("provenance")
    assert isinstance(provenance, dict)
    provenance["workload"] = {"files": 3}
    with pytest.raises(ValueError, match="identity"):
        _merge_sampled_payloads(
            payloads,
            mode=BenchmarkMode.PUBLICATION,
            expected_revision="a" * 40,
        )

    first_provenance = payloads[0].get("provenance")
    assert isinstance(first_provenance, dict)
    provenance["workload"] = first_provenance["workload"]
    provenance["validation"] = {"status": "failed"}
    with pytest.raises(ValueError, match="validation"):
        _merge_sampled_payloads(
            payloads,
            mode=BenchmarkMode.PUBLICATION,
            expected_revision="a" * 40,
        )


def test_raw_timing_samples_reject_bool_and_numeric_strings(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.json"
    baseline = tmp_path / "baseline.json"
    _result(candidate, "a" * 40)
    _result(baseline, "b" * 40)
    for path, value in ((candidate, True), (baseline, "2.0")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["benchmarks"]["vfy"]["samples_ms"] = [value]
        path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid raw timing samples"):
        compare_results(candidate, baseline, mode=BenchmarkMode.DIAGNOSTIC)


@pytest.mark.parametrize("sample_ids", [[], ["one"], ["one", "one"]])
def test_compare_rejects_invalid_sample_ids(tmp_path: Path, sample_ids: list[str]) -> None:
    candidate = tmp_path / "candidate.json"
    baseline = tmp_path / "baseline.json"
    _result(candidate, "a" * 40)
    _result(baseline, "b" * 40)
    for path in (candidate, baseline):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["benchmarks"]["vfy"]["sample_ids"] = sample_ids
        path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="sample IDs"):
        compare_results(candidate, baseline, mode=BenchmarkMode.DIAGNOSTIC)


def test_compare_requires_semantic_parity_evidence(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.json"
    baseline = tmp_path / "baseline.json"
    _result(candidate, "a" * 40)
    _result(baseline, "b" * 40)
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    payload["methodology"].pop("validation")
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="semantic"):
        compare_results(candidate, baseline)


def test_compare_cannot_relabel_diagnostic_as_publication(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.json"
    baseline = tmp_path / "baseline.json"
    _result(candidate, "a" * 40)
    _result(baseline, "b" * 40)
    for path, revision in ((candidate, "a" * 40), (baseline, "b" * 40)):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["methodology"]["mode"] = "diagnostic"
        payload["provenance"]["engine_source"] = {"revision": revision}
        payload["provenance"]["environment"]["artifact"] = {
            "native_profile": "release",
            "build_manifest": "/tmp/manifest.json",
            "wheel_sha256": "a" * 64,
            "extension_sha256": "b" * 64,
            "source_revision": revision,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="diagnostic sampling"):
        compare_results(candidate, baseline)


def test_publication_requires_actual_samples_and_hardware_identity(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.json"
    baseline = tmp_path / "baseline.json"
    _publication_result(candidate, "a" * 40)
    _publication_result(baseline, "b" * 40)
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    payload["benchmarks"]["vfy"]["samples_ms"] = [2.0]
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="raw samples"):
        compare_results(candidate, baseline)

    _publication_result(candidate, "a" * 40)
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    payload["provenance"]["environment"]["cpu"] = "different-cpu"
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="identities"):
        compare_results(candidate, baseline)


@pytest.mark.parametrize(("field", "value"), [("warmups", float("nan")), ("measured_runs", 15.5)])
def test_publication_rejects_invalid_sampling_counts(
    tmp_path: Path, field: str, value: float
) -> None:
    candidate = tmp_path / "candidate.json"
    baseline = tmp_path / "baseline.json"
    _publication_result(candidate, "a" * 40)
    _publication_result(baseline, "b" * 40)
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    payload["methodology"][field] = value
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="insufficient"):
        compare_results(candidate, baseline)


def test_publication_rejects_empty_artifact_hash(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.json"
    baseline = tmp_path / "baseline.json"
    _publication_result(candidate, "a" * 40)
    _publication_result(baseline, "b" * 40)
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    payload["provenance"]["environment"]["artifact"]["wheel_sha256"] = ""
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="wheel hash"):
        compare_results(candidate, baseline)


def test_prepared_custom_lock_uses_recorded_path_and_digest(tmp_path: Path) -> None:
    lock = tmp_path / "custom.txt"
    lock.write_text("locked dependency\n")
    metadata = {
        "dependency_lock": str(lock),
        "dependency_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
    }
    assert project_benchmark.verify_prepared_lock(metadata) == lock
    lock.write_text("changed\n")
    with pytest.raises(RuntimeError, match="recorded digest"):
        project_benchmark.verify_prepared_lock(metadata)


def test_prepare_build_uses_private_cargo_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "shared"))
    monkeypatch.setattr(project_preparation.subprocess, "check_output", lambda *a, **kw: "")
    monkeypatch.setattr(project_preparation, "git_value", lambda *a: "a" * 40)
    monkeypatch.setattr(project_preparation, "_source_input_hashes", lambda *a: {})
    monkeypatch.setattr(project_preparation.shutil, "which", lambda name: name)

    def run(command: list[str], **kwargs: object) -> None:
        commands.append(command)
        if command[0] == "maturin":
            (tmp_path / "wheels" / "vfy-fake.whl").write_bytes(b"wheel")

    monkeypatch.setattr(project_preparation, "run_checked", run)
    project_preparation.install_vfy(tmp_path / "python", tmp_path, vfy_root=tmp_path)
    command = commands[0]
    assert command[command.index("--target-dir") + 1] == str(tmp_path / "cargo-target")
    assert command[command.index("--target-dir") + 1] != str(tmp_path / "shared")


def test_prepared_historical_relative_lock_is_verified() -> None:
    lock_name = "benchmarks/locks/attrs.txt"
    lock = Path(__file__).resolve().parents[2] / lock_name
    metadata = {
        "dependency_lock": lock_name,
        "dependency_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
    }
    assert project_benchmark.verify_prepared_lock(metadata) == lock
