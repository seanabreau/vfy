"""Public-boundary proofs for the native frontend coverage comparison."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from benchmarks import benchmark_native_coverage as harness
from benchmarks.benchmark_harness import benchmark_environment
from benchmarks.benchmark_native_coverage import command_for, coverage_inputs, run_attempt
from benchmarks.benchmark_projects import CoverageSnapshot
from benchmarks.coverage_config_overlay import prepare_subprocess_config
from benchmarks.observer_plugins import RICH_CLOCK_PLUGIN
from benchmarks.project_manifest import PROJECTS, ProjectManifest

from tests.support import VfyProject


def fixture_project(root: Path, *, subprocess_patch: bool = False) -> ProjectManifest:
    project = VfyProject(root)
    project.write(
        "package/__init__.py",
        "def answer(value):\n    if value:\n        return 42\n    return 0\n",
    )
    project.write("benchmarks/__init__.py", "OWNER = 'upstream'\n")
    project.write(
        ".coveragerc",
        "[run]\nbranch = true\n" + ("patch = subprocess\n" if subprocess_patch else ""),
    )
    if subprocess_patch:
        test = (
            "import os, subprocess, sys\nfrom package import answer\n"
            "def test_child():\n"
            "    assert answer(0) == 0\n"
            "    code = 'from package import answer; assert answer(1) == 42'\n"
            "    cmd = [sys.executable, '-c', code]\n"
            "    subprocess.run(cmd, check=True, env=os.environ.copy())\n"
        )
    else:
        test = (
            "import benchmarks\nfrom package import answer\ndef test_answer():\n"
            "    assert benchmarks.OWNER == 'upstream'\n    assert answer(1) == 42\n"
        )
    project.write("tests/test_answer.py", test)
    return ProjectManifest(
        key="fixture",
        name="fixture",
        repository="example/fixture",
        revision="a" * 40,
        package_path="package/",
        tests=("tests",),
        coverage_args=("--cov=package",),
        reports=("json:coverage.json", "xml:coverage.xml"),
        parallel_args=(),
        serial_args=(),
        plugins=(),
        environment=(),
        requirements=(),
        dependency_lock="unused",
        workers=1,
        serial_workers=0,
    )


def environment() -> dict[str, str]:
    return benchmark_environment(
        {
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONHASHSEED": "0",
        }
    )


def test_shared_subprocess_configuration_reaches_both_coverage_routes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = fixture_project(root)
    original = VfyProject(root).write(
        "pyproject.toml", "[tool.coverage]\nrun.branch = true\nreport.fail_under = 100\n"
    )
    overlay = prepare_subprocess_config(
        Path(sys.executable), root, tmp_path / "coverage.toml", original
    )
    config = Path(str(overlay["config_path"]))
    for role in harness.ROLES:
        arguments, env = command_for(
            role, project, Path(sys.executable), root, tmp_path / role, cov_config=config
        )
        if role == "vfy-independent":
            assert f"--cov-config={config}" in arguments
        else:
            assert not any(argument.startswith("--cov") for argument in arguments)
        assert env.get("VFY_ORACLE_CONFIG") == (
            str(config) if role == "coverage.py-independent" else None
        )


def test_serial_reference_candidate_and_control_keep_real_evidence(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = fixture_project(root)
    python = Path(sys.executable)
    reference, baseline = run_attempt(
        "coverage.py-independent",
        project,
        python,
        root,
        tmp_path / "reference",
        environment(),
        None,
    )
    assert reference["oracle"]["tracer"] == "CTracer"
    assert Path(reference["oracle"]["parent_shard"]).is_file()
    assert reference["oracle"]["child_shards"] == []
    assert reference["engine"] == "vfy-independent-v1"
    observer = Path(reference["environment"]["PYTHONPATH"].split(os.pathsep)[0])
    assert not (observer / "__init__.py").exists()
    for record in reference["observer_plugins"]["files"].values():
        if record["source"] is not None:
            assert record["source_sha256"] == record["derived_sha256"]
    assert baseline["coverage"]["has_arcs"] is True
    assert any("|run" in context for context in baseline["coverage"]["contexts"])
    for role in ("coverage.py-independent", "vfy-independent", "plain-independent"):
        receipt, _ = run_attempt(
            role, project, python, root, tmp_path / role, environment(), baseline
        )
        assert receipt["status"] == "passed"
        assert receipt["engine"] == "vfy-independent-v1"


def test_reference_subprocess_patch_collects_and_retains_shards(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = fixture_project(root, subprocess_patch=True)
    reference, baseline = run_attempt(
        "coverage.py-independent",
        project,
        Path(sys.executable),
        root,
        tmp_path / "reference",
        environment(),
        None,
    )
    assert reference["oracle"]["child_shards"]
    assert reference["oracle"]["parent_shard"] not in reference["oracle"]["child_shards"]
    assert reference["oracle"]["subprocess_config_sha256"]
    candidate, _ = run_attempt(
        "vfy-independent",
        project,
        Path(sys.executable),
        root,
        tmp_path / "candidate",
        environment(),
        baseline,
    )
    assert candidate["status"] == "passed"


def test_reference_combines_parent_through_configured_aliases(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = replace(fixture_project(root), package_path="canonical/")
    fixture = VfyProject(root)
    fixture.write("canonical/__init__.py", (root / "package/__init__.py").read_text())
    fixture.write(
        ".coveragerc",
        "[run]\nbranch = true\n[paths]\nsource =\n    canonical\n    */package\n",
    )
    reference, observed = run_attempt(
        "coverage.py-independent",
        project,
        Path(sys.executable),
        root,
        tmp_path / "reference",
        environment(),
        None,
    )
    assert set(observed["coverage"]["files"]) == {"canonical/__init__.py"}
    assert Path(reference["oracle"]["parent_shard"]).is_file()
    assert reference["oracle"]["child_shards"] == []


def test_reference_preserves_project_fail_under(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = fixture_project(root)
    (root / ".coveragerc").write_text("[run]\nbranch = true\n[report]\nfail_under = 100\n")
    destination = tmp_path / "reference"
    with pytest.raises(RuntimeError, match="command exited"):
        run_attempt(
            "coverage.py-independent",
            project,
            Path(sys.executable),
            root,
            destination,
            environment(),
            None,
        )
    diagnostics = (destination / "stderr.log").read_text() + (
        destination / "stdout.log"
    ).read_text()
    assert "below fail-under=100" in diagnostics
    assert json.loads((destination / "validation.json").read_text())["status"] == "failed"


def test_candidate_coverage_mismatch_blocks_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    project = fixture_project(root)
    _, baseline = run_attempt(
        "coverage.py-independent",
        project,
        Path(sys.executable),
        root,
        tmp_path / "reference",
        environment(),
        None,
    )
    actual = harness.normalized_data

    def corrupted(path: Path, project_root: Path) -> CoverageSnapshot:
        result = actual(path, project_root)
        result["contexts"].append("injected|run")
        return result

    monkeypatch.setattr(harness, "normalized_data", corrupted)
    with pytest.raises(RuntimeError, match="schema-7 mismatch"):
        run_attempt(
            "vfy-independent",
            project,
            Path(sys.executable),
            root,
            tmp_path / "candidate",
            environment(),
            baseline,
        )
    assert json.loads((tmp_path / "candidate/validation.json").read_text())["status"] == "failed"


@pytest.mark.parametrize("key", sorted(PROJECTS))
def test_project_configuration_preserves_native_cli_and_source(key: str, tmp_path: Path) -> None:
    project = PROJECTS[key]
    sources, config = coverage_inputs(project, tmp_path)
    assert sources is None or sources
    if key == "requests":
        assert config == tmp_path / ".coveragerc"
    if key == "rich":
        assert config is not None
        assert config.name == "rich.coveragerc"
    if key == "datasette":
        return
    args, _ = command_for(
        "vfy-independent", project, Path(sys.executable), tmp_path, tmp_path / "run"
    )
    assert "--engine=native" in args
    assert "--all" in args
    assert all(path in args for path in project.tests)
    assert "pytest_cov.plugin" not in args


def test_subprocess_configuration_without_child_evidence_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = fixture_project(root, subprocess_patch=True)
    (root / "tests/test_answer.py").write_text(
        "from package import answer\ndef test_answer():\n    assert answer(1) == 42\n"
    )
    with pytest.raises(RuntimeError, match="command exited"):
        run_attempt(
            "coverage.py-independent",
            project,
            Path(sys.executable),
            root,
            tmp_path / "reference",
            environment(),
            None,
        )
    stderr = (tmp_path / "reference/stderr.log").read_text()
    stdout = (tmp_path / "reference/stdout.log").read_text()
    assert "subprocess patch produced no child coverage evidence" in stderr + stdout
    oracle = json.loads((tmp_path / "reference/oracle.json").read_text())
    assert Path(oracle["parent_shard"]).is_file()
    assert oracle["child_shards"] == []


def test_rich_clock_plugin_activates_in_all_native_coverage_routes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = replace(fixture_project(root), plugins=(RICH_CLOCK_PLUGIN,))
    fixture = VfyProject(root)
    fixture.write("rich/__init__.py", "")
    fixture.write(
        "rich/file_proxy.py",
        "import io\nfrom pathlib import Path\n"
        "class FileProxy(io.TextIOBase):\n"
        "    def __init__(self, console, file):\n        self.label = console\n"
        "    def flush(self):\n"
        "        with Path('closed.txt').open('a') as output:\n"
        "            output.write(self.label + '\\n')\n",
    )
    fixture.write(
        "tests/test_logging.py",
        "import logging\nfrom pathlib import Path\nfrom rich.file_proxy import FileProxy\n"
        "from package import answer\n"
        "def test_01_clock_and_proxy():\n"
        "    record = logging.getLogRecordFactory()('test', 20, __file__, 1, 'x', (), None)\n"
        "    assert record.created == 1700000000.0\n"
        "    proxy = FileProxy('first', None)\n    del proxy\n"
        "    assert not Path('closed.txt').exists()\n    assert answer(1) == 42\n"
        "def test_02_proxy_closed():\n"
        "    assert Path('closed.txt').read_text() == 'first\\n'\n"
        "    assert answer(0) == 0\n",
    )
    env = environment()
    env["PYTEST_PLUGINS"] = RICH_CLOCK_PLUGIN
    baseline = None
    for role in harness.ROLES:
        (root / "closed.txt").unlink(missing_ok=True)
        receipt, observed = run_attempt(
            role, project, Path(sys.executable), root, tmp_path / role, env, baseline
        )
        assert receipt["status"] == "passed"
        command = receipt["command"]
        assert command[command.index(RICH_CLOCK_PLUGIN) - 1] == "-p"
        assert receipt["environment"]["PYTEST_PLUGINS"] == RICH_CLOCK_PLUGIN
        if baseline is None:
            baseline = observed


def test_rich_clock_rejects_other_projects_before_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "native-coverage",
            "--project",
            "attrs",
            "--root",
            str(tmp_path),
            "--python",
            sys.executable,
            "--preparation-manifest",
            str(tmp_path / "manifest"),
            "--build-manifest",
            str(tmp_path / "build"),
            "--artifacts",
            str(tmp_path / "out"),
            "--validate-only",
            "--rich-clock",
        ],
    )
    with pytest.raises(SystemExit, match="only available for Rich"):
        harness.main()
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("filtered", [False, True])
def test_cli_compresses_only_validated_samples_and_preserves_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filtered: bool
) -> None:
    import gzip  # noqa: PLC0415 - used only by this archive round-trip test
    import hashlib  # noqa: PLC0415 - used only by this archive round-trip test
    from types import SimpleNamespace  # noqa: PLC0415 - avoids changing the real CLI interface

    from benchmarks.cli import main as benchmark_main  # noqa: PLC0415 - test the public dispatcher

    root = tmp_path / "project"
    project = replace(fixture_project(root), dependency_lock="benchmarks/locks/click.txt")
    (root / "tests/test_answer.py").write_text(
        "import pytest\nfrom package import answer\n"
        "@pytest.mark.parametrize('value', range(200))\n"
        "def test_answer(value):\n    assert answer(value) in (0, 42)\n"
    )
    repository = Path(harness.__file__).resolve().parents[1]
    lock_digest = hashlib.sha256((repository / project.dependency_lock).read_bytes()).hexdigest()
    metadata = {
        "project": project.key,
        "revision": project.revision,
        "checked_out_revision": project.revision,
        "python": sys.executable,
        "dependency_lock": project.dependency_lock,
        "dependency_lock_sha256": lock_digest,
        "vfy_wheel": {"source_revision": "b" * 40},
    }
    monkeypatch.setitem(harness.PROJECTS, project.key, project)
    monkeypatch.setattr(
        harness, "require_clean", lambda _path: SimpleNamespace(revision=project.revision)
    )
    monkeypatch.setattr(harness, "require_revision", lambda *_args: None)
    monkeypatch.setattr(harness, "preparation_metadata", lambda *_args, **_kwargs: metadata)
    monkeypatch.setattr(
        harness, "independent_coverage_artifact", lambda *_args: {"identity": "same"}
    )
    monkeypatch.setattr(harness, "selected_interpreter_metadata", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(harness, "prepared_project_provenance", lambda *_args, **_kwargs: {})
    artifacts = tmp_path / "artifacts"
    output = tmp_path / "result.json"
    assert (
        benchmark_main(
            [
                "run",
                "native-coverage",
                "--project",
                project.key,
                "--root",
                str(root),
                "--python",
                sys.executable,
                "--preparation-manifest",
                str(tmp_path / "preparation.json"),
                "--build-manifest",
                str(tmp_path / "build.json"),
                "--artifacts",
                str(artifacts),
                "--output",
                str(output),
                "--mode",
                "diagnostic",
                "--warmups",
                "1",
                "--runs",
                "1",
                "--compress-samples",
                *(
                    [
                        "--timing-route",
                        "coverage.py-independent",
                        "--timing-route",
                        "vfy-independent",
                    ]
                    if filtered
                    else []
                ),
            ]
        )
        == 0
    )
    result = json.loads(output.read_text())
    selected = list(harness.ROLES[:2] if filtered else harness.ROLES)
    assert len(result["attempts"]) == 3 + 2 * len(selected)
    assert [record["role"] for record in result["attempts"][:3]] == list(harness.ROLES)
    assert {record["role"] for record in result["attempts"][3:]} == set(selected)
    assert result["methodology"]["timing_routes"] == selected
    preflight = json.loads((artifacts / "preflight.json").read_text())
    for filename, digest in preflight["proof_sha256"].items():
        path = Path(filename)
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert all("retained_artifacts" not in record for record in result["attempts"][:3])
    compressed = 0
    for record in result["attempts"][3:]:
        retained = record["retained_artifacts"]
        assert record["status"] == "passed"
        for original_name, info in retained.items():
            path = Path(info["path"])
            payload = (
                gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
            )
            assert hashlib.sha256(payload).hexdigest() == info["sha256"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == info["retained_sha256"]
            assert len(payload) == info["bytes"]
            if path.suffix == ".gz":
                compressed += 1
                assert not path.with_name(original_name).exists()
        validation = Path(retained["validation.json"]["path"])
        assert "retained_artifacts" not in json.loads(validation.read_text())
    assert compressed > 0
