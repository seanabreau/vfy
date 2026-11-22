"""Exercise controller evidence workflows with native execution and the pytest oracle."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support.history import read_history
from tests.support.vfy_project import CommandResult, VfyProject


@pytest.fixture(params=["pytest-native", "pytest"])
def engine(request: pytest.FixtureRequest) -> str:
    selected = str(request.param)
    if selected == "pytest-native" and not sys.platform.startswith(("linux", "darwin")):
        pytest.skip("native pytest requires Linux or macOS")
    return selected


def _project(root: Path, engine: str) -> VfyProject:
    project = VfyProject(root)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("pkg/__init__.py", "")
    for name, value in (("a", 1), ("b", 2)):
        project.write(f"pkg/{name}.py", f"def value():\n    return {value}\n")
        project.write(
            f"tests/test_{name}.py",
            f"from pkg.{name} import value\ndef test_{name}():\n    assert value() == {value}\n",
        )
    project.git_init()
    return project


def _run(project: VfyProject, engine: str, *args: str, workers: str = "1") -> CommandResult:
    return project.run_vfy(
        "test",
        f"--engine={engine}",
        f"--workers={workers}",
        *args,
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _coverage(project: VfyProject) -> dict[str, object]:
    data = project.data()
    observed: dict[str, object] = {}
    for path in sorted(data.measured_files()):
        if not Path(path).is_relative_to(project.root / "pkg"):
            continue
        per_context: dict[str, object] = {}
        for context in sorted(data.measured_contexts()):
            data.set_query_context(context)
            per_context[context] = (data.lines(path), data.arcs(path))
        data.set_query_contexts(None)
        observed[str(Path(path).relative_to(project.root))] = (
            data.lines(path),
            data.arcs(path),
            data.contexts_by_lineno(path),
            per_context,
        )
    return observed


@pytest.mark.parametrize("workers", ["1", "2"])
def test_ci_baseline_selects_changed_tests_and_restores_complete_coverage(
    tmp_path: Path, engine: str, workers: str
) -> None:
    project = _project(tmp_path, engine)
    baseline = ".vfy/baseline.json"
    exported = _run(project, engine, "--all", "--export-baseline", baseline, workers=workers)
    exported.expect(0)
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")
    project.git_commit("change a")
    before = {
        name: (tmp_path / name).read_bytes() for name in (".coverage", ".vfy/test-history.sqlite3")
    }
    planned = _run(
        project,
        engine,
        "--baseline",
        baseline,
        "--plan-only",
        "--plan-output",
        ".vfy/plan.json",
        workers=workers,
    )
    planned.expect(0)
    assert before == {name: (tmp_path / name).read_bytes() for name in before}
    plan = json.loads((tmp_path / ".vfy/plan.json").read_text())["selection"]
    assert plan["selected"] == ["tests/test_a.py::test_a"]
    assert not plan["fallback_full"]
    selected = _run(
        project,
        engine,
        "--baseline",
        baseline,
        "--plan-output",
        ".vfy/plan.json",
        workers=workers,
    )
    selected.expect(0)
    assert "1 passed" in selected.stdout
    replayed = _coverage(project)
    full = _run(project, "pytest", "--all", workers=workers)
    full.expect(0)
    assert replayed == _coverage(project)


def test_unchanged_ci_baseline_replays_without_execution(tmp_path: Path, engine: str) -> None:
    project = _project(tmp_path, engine)
    exported = _run(project, engine, "--all", "--export-baseline", ".vfy/baseline.json")
    exported.expect(0)
    previous = _coverage(project)
    result = _run(
        project,
        engine,
        "--baseline",
        ".vfy/baseline.json",
        "--plan-output",
        ".vfy/plan.json",
    )
    result.expect(0)
    plan = json.loads((tmp_path / ".vfy/plan.json").read_text())["selection"]
    assert plan["selected"] == []
    assert not plan["fallback_full"]
    assert "2 deselected" in result.stdout
    assert _coverage(project) == previous


def test_changed_parameter_inventory_forces_full_native_ci_run(tmp_path: Path, engine: str) -> None:
    project = _project(tmp_path, engine)
    project.write(
        "tests/test_b.py",
        "import os\nimport pytest\n"
        "@pytest.mark.parametrize('value', range(2 if os.getenv('MORE_CASES') else 1))\n"
        "def test_b(value):\n    assert value >= 0\n",
    )
    project.git_commit("dynamic parameters")
    exported = _run(project, engine, "--all", "--export-baseline", ".vfy/baseline.json")
    exported.expect(0)
    result = project.run_vfy(
        "test",
        f"--engine={engine}",
        "--baseline",
        ".vfy/baseline.json",
        "--plan-output",
        ".vfy/plan.json",
        "--workers=1",
        "-q",
        env={"MORE_CASES": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(0)
    plan = json.loads((tmp_path / ".vfy/plan.json").read_text())["selection"]
    assert plan["fallback_full"]
    assert plan["selected"] == [
        "tests/test_a.py::test_a",
        "tests/test_b.py::test_b[0]",
        "tests/test_b.py::test_b[1]",
    ]


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_native_ci_audit_reports_omitted_failures(tmp_path: Path, engine: str, phase: str) -> None:
    project = _project(tmp_path, engine)
    failure = "assert not os.getenv('AUDIT_FAIL')"
    project.write(
        "tests/test_b.py",
        "import os\nimport pytest\n"
        "@pytest.fixture\ndef external():\n"
        f"    {failure if phase == 'setup' else 'pass'}\n"
        "    yield\n"
        f"    {failure if phase == 'teardown' else 'pass'}\n"
        "def test_b(external):\n"
        f"    {failure if phase == 'call' else 'pass'}\n",
    )
    project.git_commit("external input")
    exported = _run(project, engine, "--all", "--export-baseline", ".vfy/baseline.json")
    exported.expect(0)
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")
    project.git_commit("edit a")
    result = project.run_vfy(
        "test",
        f"--engine={engine}",
        "--baseline",
        ".vfy/baseline.json",
        "--audit-selection",
        "--plan-output",
        ".vfy/audit.json",
        "--workers=1",
        "-q",
        env={"AUDIT_FAIL": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(1)
    assert json.loads((tmp_path / ".vfy/audit.json").read_text())["audit"] == {
        "complete": True,
        "omitted_failures": ["tests/test_b.py::test_b"],
    }


@pytest.mark.parametrize("workers", ["1", "2"])
def test_failure_confirmation_runs_serially_and_keeps_primary_failure(
    tmp_path: Path, engine: str, workers: str
) -> None:
    project = _project(tmp_path, engine)
    project.write(
        "tests/test_a.py",
        "import os\nfrom pathlib import Path\nimport pytest\n"
        "@pytest.mark.asyncio\n"
        "async def test_a():\n"
        "    marker = Path('.vfy/attempt')\n"
        "    marker.parent.mkdir(exist_ok=True)\n"
        "    if not marker.exists():\n"
        "        marker.write_text('primary')\n"
        "        assert False\n"
        "    assert os.getenv('VFY_NATIVE_WORKERS', '1') == '1'\n"
        "    assert 'PYTEST_XDIST_WORKER' not in os.environ\n",
    )
    project.git_commit("unstable test")
    result = _run(project, engine, "--confirm-failures", "--no-cov", workers=workers)
    result.expect(1)
    assert "1 failed test passed on confirmation; the original run remains failed" in result.stderr
    history = read_history(tmp_path / ".vfy/test-history.sqlite3")
    observations = history["tests"]["tests/test_a.py::test_a"]["observations"]
    assert [(item["attempt"], item["outcome"]) for item in observations] == [
        ("primary", "failed"),
        ("confirmation", "passed"),
    ]
    assert observations[0]["run_id"] == observations[1]["run_id"]
    successful = history["tests"]["tests/test_b.py::test_b"]
    assert [(item["attempt"], item["outcome"]) for item in successful["observations"]] == [
        ("primary", "passed"),
    ]
    assert successful["duration_ms"] >= 0

    assert json.loads((tmp_path / ".pytest_cache/v/cache/lastfailed").read_text()) == {
        "tests/test_a.py::test_a": True
    }


def test_corrupt_baseline_preserves_existing_native_evidence(tmp_path: Path, engine: str) -> None:
    project = _project(tmp_path, engine)
    exported = _run(project, engine, "--all", "--export-baseline", ".vfy/baseline.json")
    exported.expect(0)
    before = {
        name: (tmp_path / name).read_bytes() for name in (".coverage", ".vfy/test-history.sqlite3")
    }
    project.write(".vfy/baseline.json", '{"version": 1}')
    result = _run(project, engine, "--baseline", ".vfy/baseline.json")
    result.expect(1)
    assert "failed to load CI baseline" in result.stderr
    assert before == {name: (tmp_path / name).read_bytes() for name in before}


def test_native_plan_only_stdout_is_json_and_does_not_publish_evidence(
    tmp_path: Path, engine: str
) -> None:
    project = _project(tmp_path, engine)
    result = _run(project, engine, "--plan-only")
    result.expect(0)
    plan = json.loads(result.stdout)
    assert sorted(plan["collected"]) == ["tests/test_a.py::test_a", "tests/test_b.py::test_b"]
    assert not (tmp_path / ".coverage").exists()
    assert not (tmp_path / ".vfy/test-history.sqlite3").exists()


def test_source_edit_during_native_ci_run_discards_evidence(tmp_path: Path, engine: str) -> None:
    project = _project(tmp_path, engine)
    project.write(
        "tests/test_a.py",
        "import os\nfrom pathlib import Path\nfrom pkg.a import value\n"
        "def test_a():\n"
        "    assert value() == 1\n"
        "    if os.getenv('EDIT_SOURCE'):\n"
        "        Path('pkg/a.py').write_text('def value():\\n    return 999\\n')\n",
    )
    project.git_commit("source race fixture")
    exported = _run(project, engine, "--all", "--export-baseline", ".vfy/baseline.json")
    exported.expect(0)
    previous = (tmp_path / ".coverage").read_bytes()
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")
    project.git_commit("change a")
    result = project.run_vfy(
        "test",
        f"--engine={engine}",
        "--workers=1",
        "--baseline",
        ".vfy/baseline.json",
        "-q",
        env={"EDIT_SOURCE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(1)
    assert "evidence was discarded" in result.stderr
    assert (tmp_path / ".coverage").read_bytes() == previous
