from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.support.vfy_project import CommandResult, VfyProject

ENVIRONMENT = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def _project(root: Path) -> VfyProject:
    project = VfyProject(root)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("pkg/__init__.py", "")
    project.write("pkg/a.py", "def value():\n    return 1\n")
    project.write("pkg/b.py", "def value():\n    return 2\n")
    for name, expected in (("a", 1), ("b", 2)):
        project.write(
            f"tests/test_{name}.py",
            f"from pkg.{name} import value\n\ndef test_{name}():\n"
            f"    assert value() == {expected}\n",
        )
    project.git_init()
    return project


def _run(project: VfyProject, *args: str) -> CommandResult:
    return project.run_vfy("test", *args, "-j", "1", "-q", env=ENVIRONMENT)


def _export(project: VfyProject, destination: str = ".vfy/baseline.json") -> Path:
    result = _run(project, "--engine=pytest", "--all", "--export-baseline", destination)
    result.expect(0)
    path = project.root / destination
    assert path.is_file(), result.stderr
    return path


def _coverage(project: VfyProject) -> dict[str, object]:
    data = project.data()
    return {
        str(Path(path).relative_to(project.root)): (
            tuple(data.lines(path) or ()),
            tuple(sorted(data.arcs(path) or ())),
            {line: tuple(sorted(c)) for line, c in sorted(data.contexts_by_lineno(path).items())},
        )
        for path in sorted(data.measured_files())
    }


def test_export_shares_dependency_snapshot_with_local_evidence(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write(
        "export_once.py",
        "import sys\n"
        "from pathlib import Path\n"
        "import vfy.dependency_fingerprint as dependencies\n"
        "from vfy.entry import main\n"
        "original = dependencies._compute\n"
        "calls = []\n"
        "def counted():\n"
        "    calls.append(1)\n"
        "    return original()\n"
        "dependencies._compute = counted\n"
        "sys.argv = ['vfy', 'test', '--engine=pytest', '--all', '-j', '1', '-q', "
        "'--export-baseline', '.vfy/baseline.json']\n"
        "status = main()\n"
        "Path('scans.txt').write_text(str(len(calls)))\n"
        "raise SystemExit(status)\n",
    )
    project.git_commit("add export driver")
    result = project.run_python("export_once.py", env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    diagnostic = result.stdout + result.stderr
    assert result.returncode == 0, diagnostic
    assert (tmp_path / ".vfy/baseline.json").is_file()
    assert (tmp_path / "scans.txt").read_text() == "1"


@pytest.mark.parametrize("workers", ["1", "2"])
@pytest.mark.parametrize("branch", [False, True])
def test_ancestor_selection_plan_and_complete_coverage(
    tmp_path: Path, workers: str, branch: bool
) -> None:
    project = _project(tmp_path)
    coverage_args = ["--cov=pkg", "--cov-context=test", *(["--cov-branch"] if branch else [])]
    exported = project.run_vfy(
        "test",
        "--engine=pytest",
        "--all",
        "--export-baseline",
        ".vfy/baseline.json",
        "-j",
        workers,
        "-q",
        *coverage_args,
        env=ENVIRONMENT,
    )
    exported.expect(0)
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")
    project.git_commit("change a")
    selected = project.run_vfy(
        "test",
        "--engine=pytest",
        "--baseline",
        ".vfy/baseline.json",
        "--plan-output",
        ".vfy/plan.json",
        "-j",
        workers,
        "-q",
        *coverage_args,
        env=ENVIRONMENT,
    )
    selected.expect(0)
    plan = json.loads((tmp_path / ".vfy/plan.json").read_text())
    assert plan["selection"]["selected"] == ["tests/test_a.py::test_a"]
    assert not plan["selection"]["fallback_full"]
    assert plan["baseline_commit"] != plan["target_commit"]
    replayed = _coverage(project)
    full = project.run_vfy(
        "test", "--engine=pytest", "--all", "-j", workers, "-q", *coverage_args, env=ENVIRONMENT
    )
    full.expect(0)
    assert replayed == _coverage(project)
    reference = project.run_python(
        "-m",
        "pytest",
        "-p",
        "pytest_cov.plugin",
        "-q",
        *coverage_args,
        env={**ENVIRONMENT, "COVERAGE_FILE": str(tmp_path / ".coverage")},
    )
    reference.expect(0)
    assert replayed == _coverage(project)


def test_baseline_relocates_and_plan_only_does_not_publish_evidence(tmp_path: Path) -> None:
    original = _project(tmp_path / "original")
    _export(original)
    relocated = tmp_path / "relocated"
    shutil.copytree(original.root, relocated)
    project = VfyProject(relocated)
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")
    project.git_commit("edit")
    coverage = (relocated / ".coverage").read_bytes()
    history = (relocated / ".vfy/test-history.sqlite3").read_bytes()
    result = _run(
        project,
        "--engine=pytest",
        "--baseline",
        ".vfy/baseline.json",
        "--plan-only",
        "--plan-output",
        ".vfy/plan.json",
    )
    result.expect(0)
    plan = json.loads((relocated / ".vfy/plan.json").read_text())
    assert plan["selection"]["selected"] == ["tests/test_a.py::test_a"]
    assert (relocated / ".coverage").read_bytes() == coverage
    assert (relocated / ".vfy/test-history.sqlite3").read_bytes() == history
    assert str(original.root) not in (relocated / ".vfy/baseline.json").read_text()


def test_added_test_forces_full_ci_run(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _export(project)
    project.write("tests/test_new.py", "def test_new():\n    assert True\n")
    project.git_commit("new test")
    result = _run(
        project,
        "--engine=pytest",
        "--baseline",
        ".vfy/baseline.json",
        "--plan-output",
        ".vfy/plan.json",
    )
    result.expect(0)
    plan = json.loads((tmp_path / ".vfy/plan.json").read_text())
    assert plan["selection"]["fallback_full"]
    assert len(plan["selection"]["selected"]) == 3


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_audit_reports_omitted_external_failure(tmp_path: Path, phase: str) -> None:
    project = _project(tmp_path)
    failure = "assert os.getenv('VFY_AUDIT_FAIL') != '1'"
    fixture = (
        "import os\nimport pytest\n\n@pytest.fixture\ndef external():\n"
        f"    {failure if phase == 'setup' else 'pass'}\n"
        "    yield\n"
        f"    {failure if phase == 'teardown' else 'pass'}\n"
        "\ndef test_b(external):\n"
        f"    {failure if phase == 'call' else 'pass'}\n"
    )
    project.write("tests/test_b.py", fixture)
    project.git_commit("external input test")
    _export(project)
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")
    project.git_commit("edit a")
    result = project.run_vfy(
        "test",
        "--engine=pytest",
        "--baseline",
        ".vfy/baseline.json",
        "--audit-selection",
        "--plan-output",
        ".vfy/audit.json",
        "-j",
        "1",
        "-q",
        env={**ENVIRONMENT, "VFY_AUDIT_FAIL": "1"},
    )
    result.expect(1)
    audit = json.loads((tmp_path / ".vfy/audit.json").read_text())["audit"]
    assert audit == {"complete": True, "omitted_failures": ["tests/test_b.py::test_b"]}
    assert "would have omitted" in result.stderr


@pytest.mark.parametrize("corruption", ["json", "ambiguous_paths"])
def test_corrupt_baseline_preserves_existing_evidence(tmp_path: Path, corruption: str) -> None:
    project = _project(tmp_path)
    baseline = _export(project)
    coverage = (tmp_path / ".coverage").read_bytes()
    if corruption == "json":
        baseline.write_text('{"version": 1}')
    else:
        bundle = json.loads(baseline.read_text())
        bundle["source_roots"] = ["pkg/./"]
        baseline.write_text(json.dumps(bundle))
    result = _run(project, "--engine=pytest", "--baseline", str(baseline))
    assert result.returncode == 1
    assert "failed to load CI baseline" in result.stderr
    assert (tmp_path / ".coverage").read_bytes() == coverage


def test_collection_hook_inventory_change_runs_full_scope(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write(
        "tests/test_b.py",
        "import os\nimport pytest\n\n"
        "@pytest.mark.parametrize('value', range(2 if os.getenv('VFY_MORE_TESTS') else 1))\n"
        "def test_b(value):\n    assert value >= 0\n",
    )
    project.git_commit("dynamic parametrization")
    _export(project)
    result = project.run_vfy(
        "test",
        "--engine=pytest",
        "--baseline",
        ".vfy/baseline.json",
        "--plan-output",
        ".vfy/plan.json",
        "-j",
        "1",
        "-q",
        env={**ENVIRONMENT, "VFY_MORE_TESTS": "1"},
    )
    result.expect(0)
    plan = json.loads((tmp_path / ".vfy/plan.json").read_text())["selection"]
    assert plan["fallback_full"]
    assert len(plan["selected"]) == 3
    assert any("inventory changed" in reason for reason in plan["reasons"])


@pytest.mark.parametrize("change", ["missing", "deleted", "renamed", "nonancestor"])
def test_incompatible_ci_evidence_falls_back(tmp_path: Path, change: str) -> None:
    project = _project(tmp_path)
    bundle = _export(project)
    if change == "missing":
        bundle.unlink()
    elif change == "deleted":
        project.run("git", "rm", "tests/test_b.py")
        project.git_commit("remove test")
    elif change == "renamed":
        project.run("git", "mv", "pkg/b.py", "pkg/c.py")
        project.write(
            "tests/test_b.py", "from pkg.c import value\n\ndef test_b():\n    assert value() == 2\n"
        )
        project.git_commit("rename source")
    else:
        project.run("git", "checkout", "--orphan", "unrelated")
        project.git_commit("unrelated history")
    result = _run(
        project,
        "--engine=pytest",
        "--baseline",
        ".vfy/baseline.json",
        "--plan-output",
        ".vfy/plan.json",
    )
    result.expect(0)
    assert json.loads((tmp_path / ".vfy/plan.json").read_text())["selection"]["fallback_full"]


@pytest.mark.parametrize("outcome", ["failure", "interrupt", "threshold"])
def test_unsuccessful_export_preserves_bundle(tmp_path: Path, outcome: str) -> None:
    project = _project(tmp_path)
    project.write(
        "tests/test_a.py",
        "import os\nfrom pkg.a import value\n\ndef test_a():\n"
        "    if os.getenv('VFY_INTERRUPT'):\n        raise KeyboardInterrupt\n"
        "    assert value() == 1\n",
    )
    project.git_commit("interruptible test")
    bundle = _export(project)
    previous = bundle.read_bytes()
    arguments = ["--all", "--export-baseline", ".vfy/baseline.json", "-j", "1", "-q"]
    environment = dict(ENVIRONMENT)
    if outcome == "failure":
        project.write("pkg/a.py", "def value():\n    return 0\n")
        project.git_commit("breaking change")
    elif outcome == "interrupt":
        environment["VFY_INTERRUPT"] = "1"
    else:
        arguments.extend(("--cov-fail-under=100", "--cov=pkg"))
        project.write("pkg/b.py", "def value():\n    return 2\n\ndef uncovered():\n    return 3\n")
        project.git_commit("uncovered code")
    result = project.run_vfy("test", "--engine=pytest", *arguments, env=environment)
    assert result.returncode == (2 if outcome == "interrupt" else 1), result.stdout + result.stderr
    assert bundle.read_bytes() == previous


def test_plan_output_cannot_replace_baseline(tmp_path: Path) -> None:
    project = _project(tmp_path)
    bundle = _export(project)
    previous = bundle.read_bytes()
    result = _run(
        project, "--engine=pytest", "--baseline", str(bundle), "--plan-output", str(bundle)
    )
    assert result.returncode == 1
    assert "artifact destinations" in result.stderr
    assert bundle.read_bytes() == previous


@pytest.mark.parametrize("artifact_directory", [".vfy", "."])
def test_unchanged_ci_baseline_replays_without_test_execution(
    tmp_path: Path, artifact_directory: str
) -> None:
    project = _project(tmp_path)
    bundle = _export(project, f"{artifact_directory}/baseline.json")
    plan_path = tmp_path / artifact_directory / "plan.json"
    previous = _coverage(project)
    result = _run(
        project, "--engine=pytest", "--baseline", str(bundle), "--plan-output", str(plan_path)
    )
    result.expect(0)
    plan = json.loads(plan_path.read_text())["selection"]
    assert plan["selected"] == []
    assert not plan["fallback_full"]
    assert "2 deselected" in result.stdout
    assert _coverage(project) == previous


def test_incomplete_audit_preserves_primary_interrupt_status(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write(
        "tests/test_b.py",
        "import os\n\ndef test_b():\n"
        "    if os.getenv('VFY_INTERRUPT'):\n        raise KeyboardInterrupt\n",
    )
    project.git_commit("interruptible audit")
    _export(project)
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")
    project.git_commit("edit a")
    result = project.run_vfy(
        "test",
        "--engine=pytest",
        "--baseline",
        ".vfy/baseline.json",
        "--audit-selection",
        "--plan-output",
        ".vfy/audit.json",
        "-j",
        "1",
        "-q",
        env={**ENVIRONMENT, "VFY_INTERRUPT": "1"},
    )
    result.expect(2)
    assert json.loads((tmp_path / ".vfy/audit.json").read_text())["audit"]["complete"] is False
    assert "audit is incomplete" in result.stderr


def test_source_edit_during_selected_run_discards_evidence(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write(
        "tests/test_a.py",
        "import os\nfrom pathlib import Path\nfrom pkg.a import value\n\ndef test_a():\n"
        "    assert value() == 1\n"
        "    if os.getenv('VFY_EDIT_SOURCE'):\n"
        "        Path('pkg/a.py').write_text('def value():\\n    return 999\\n')\n",
    )
    project.git_commit("source race test")
    _export(project)
    previous = (tmp_path / ".coverage").read_bytes()
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")
    project.git_commit("edit a")
    result = project.run_vfy(
        "test",
        "--engine=pytest",
        "--baseline",
        ".vfy/baseline.json",
        "-j",
        "1",
        "-q",
        env={**ENVIRONMENT, "VFY_EDIT_SOURCE": "1"},
    )
    result.expect(1)
    assert "evidence was discarded" in result.stderr
    assert (tmp_path / ".coverage").read_bytes() == previous


def test_ci_selection_includes_transitive_import_dependents(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write("pkg/middle.py", "from pkg.a import value\n")
    project.write(
        "pkg/b.py", "from pkg.middle import value as imported\n\ndef value():\n    return 2\n"
    )
    project.git_commit("transitive import chain")
    _export(project)
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")
    project.git_commit("edit imported source")
    result = _run(
        project,
        "--engine=pytest",
        "--baseline",
        ".vfy/baseline.json",
        "--plan-output",
        ".vfy/plan.json",
    )
    result.expect(0)
    plan = json.loads((tmp_path / ".vfy/plan.json").read_text())["selection"]
    assert not plan["fallback_full"]
    assert sorted(plan["selected"]) == ["tests/test_a.py::test_a", "tests/test_b.py::test_b"]
    assert "2 passed" in result.stdout


def test_ci_selection_keeps_file_scope_after_import_during_test_call(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write("pkg/a.py", "def value():\n    return 1\n\ndef other():\n    return 3\n")
    project.write("pkg/late.py", "from pkg.a import value\nRESULT = value()\n")
    project.write(
        "tests/test_b.py",
        "def test_b():\n    import pkg.late\n    assert pkg.late.RESULT == 1\n",
    )
    project.write(
        "tests/test_c.py",
        "from pkg.a import other\n\ndef test_c():\n    assert other() == 3\n",
    )
    project.write(
        "tests/test_unrelated.py",
        "from pkg.b import value\n\ndef test_unrelated():\n    assert value() == 2\n",
    )
    project.git_commit("import source during a test call")
    _export(project)
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n\ndef other():\n    return 3\n")
    project.git_commit("edit function body")

    selected = _run(
        project,
        "--engine=pytest",
        "--baseline",
        ".vfy/baseline.json",
        "--plan-output",
        ".vfy/plan.json",
    )
    selected.expect(0)
    plan = json.loads((tmp_path / ".vfy/plan.json").read_text())["selection"]
    assert sorted(plan["selected"]) == [
        "tests/test_a.py::test_a",
        "tests/test_b.py::test_b",
        "tests/test_c.py::test_c",
    ]
    assert sorted(plan["all_tests"]) == [
        "tests/test_a.py::test_a",
        "tests/test_b.py::test_b",
        "tests/test_c.py::test_c",
        "tests/test_unrelated.py::test_unrelated",
    ]
    assert not plan["fallback_full"]
    replayed = _coverage(project)
    full = _run(project, "--engine=pytest", "--all")
    full.expect(0)
    assert replayed == _coverage(project)


def test_changed_pytest_invocation_runs_full_requested_scope(tmp_path: Path) -> None:
    project = _project(tmp_path)
    baseline = _export(project)
    previous = baseline.read_bytes()
    result = project.run_vfy(
        "test",
        "--engine=pytest",
        "--baseline",
        str(baseline),
        "--plan-output",
        ".vfy/plan.json",
        "-j",
        "1",
        "-q",
        "-k",
        "test_a",
        env=ENVIRONMENT,
    )
    result.expect(0)
    plan = json.loads((tmp_path / ".vfy/plan.json").read_text())["selection"]
    assert plan["fallback_full"]
    assert plan["selected"] == ["tests/test_a.py::test_a"]
    assert "1 passed" in result.stdout
    assert baseline.read_bytes() == previous


@pytest.mark.parametrize(
    ("option", "configured"),
    [("--no-cov", False), ("--cov-append", False), ("--no-replay", False), ("--no-cov", True)],
)
def test_export_rejects_runs_without_fresh_coverage(
    tmp_path: Path, option: str, configured: bool
) -> None:
    project = _project(tmp_path)
    bundle = _export(project)
    previous = bundle.read_bytes()
    result = project.run_vfy(
        "test",
        "--engine=pytest",
        "--all",
        "--export-baseline",
        str(bundle),
        "-j",
        "1",
        *([] if configured else [option]),
        "-q",
        env={**ENVIRONMENT, **({"PYTEST_ADDOPTS": option} if configured else {})},
    )
    result.expect(1)
    assert "requires fresh coverage" in result.stderr
    assert bundle.read_bytes() == previous


def test_export_preserves_bundle_when_config_file_disables_coverage(tmp_path: Path) -> None:
    project = _project(tmp_path)
    bundle = _export(project)
    previous = bundle.read_bytes()
    project.write(".vfy/custom.ini", "[pytest]\naddopts = --no-cov\n")
    result = project.run_vfy(
        "test",
        "--engine=pytest",
        "--all",
        "--export-baseline",
        str(bundle),
        "-j",
        "1",
        "-c",
        ".vfy/custom.ini",
        "-q",
        env=ENVIRONMENT,
    )
    result.expect(1)
    assert "requires fresh coverage" in result.stderr
    assert bundle.read_bytes() == previous


@pytest.mark.parametrize("untracked_input", [False, True])
def test_local_plan_matches_execution_after_git_safety_checks(
    tmp_path: Path, untracked_input: bool
) -> None:
    project = _project(tmp_path)
    _export(project)
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")
    if untracked_input:
        project.write("runtime-input.txt", "new runtime input\n")
    result = _run(project, "--engine=pytest", "--plan-output", ".vfy/plan.json")
    result.expect(0)
    plan = json.loads((tmp_path / ".vfy/plan.json").read_text())["selection"]
    expected = ["tests/test_a.py::test_a"]
    if untracked_input:
        expected.append("tests/test_b.py::test_b")
    assert plan["fallback_full"] is untracked_input
    assert sorted(plan["selected"]) == expected
    assert f"{len(expected)} passed" in result.stdout


def test_plan_only_stdout_is_json_with_collection_diagnostics(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write(
        "conftest.py",
        "import os\nprint('project collection diagnostic')\n"
        "os.write(1, b'native collection diagnostic\\n')\n",
    )
    project.git_commit("collection diagnostic")
    result = project.run_vfy(
        "test",
        "--engine=pytest",
        "--plan-only",
        "-j",
        "1",
        "-s",
        "-q",
        env=ENVIRONMENT,
    )
    result.expect(0)
    plan = json.loads(result.stdout)
    assert sorted(plan["collected"]) == ["tests/test_a.py::test_a", "tests/test_b.py::test_b"]
    assert "project collection diagnostic" in result.stderr
    assert "native collection diagnostic" in result.stderr
    assert not (tmp_path / ".coverage").exists()
    assert not (tmp_path / ".vfy/test-history.sqlite3").exists()
