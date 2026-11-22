from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from coverage import CoverageData as ReferenceData

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject
from vfy import Coverage


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
@pytest.mark.parametrize("first_engine", ["pytest-native", "pytest"])
def test_replay_reuses_the_same_engine_and_runs_after_switching_engines(
    tmp_path: Path, first_engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_case.py",
        "from pathlib import Path\n\n"
        "def test_case():\n"
        "    path = Path('.vfy/executions')\n"
        "    path.parent.mkdir(exist_ok=True)\n"
        "    with path.open('a') as stream:\n"
        "        stream.write('run\\n')\n",
    )
    project.git_init()
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": None}
    other_engine = "pytest" if first_engine == "pytest-native" else "pytest-native"
    for engine, expected_executions, expected_reuse in (
        (first_engine, 1, False),
        (first_engine, 1, True),
        (other_engine, 2, False),
        (other_engine, 2, True),
    ):
        completed = project.run_vfy("test", f"--engine={engine}", "--workers=1", env=environment)
        completed.expect(0)
        assert (tmp_path / ".vfy/executions").read_text().splitlines() == [
            "run"
        ] * expected_executions
        assert ("Reused 1 tests from passing baseline" in completed.stderr) is expected_reuse


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
def test_native_replay_rejects_a_stock_baseline_with_unsupported_runtime_hooks(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", "def pytest_runtest_call(item):\n    pass\n")
    project.write("test_case.py", "def test_case():\n    assert True\n")
    project.git_init()
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": None}
    baseline = project.run_vfy("test", "--engine=pytest", "--workers=1", env=environment)
    baseline.expect(0)

    completed = project.run_vfy("test", "--engine=pytest-native", "--workers=1", env=environment)

    completed.expect(4)
    assert "native pytest does not execute conftest hook 'pytest_runtest_call'" in completed.stderr
    assert "Reused 1 tests from passing baseline" not in completed.stderr


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native test engine does not support Windows",
)
def test_full_run_does_not_save_green_baseline_after_source_changes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("src/value.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_value.py",
        "from pathlib import Path\n"
        "from src.value import value\n\n"
        "def test_value():\n"
        "    assert value() == 1\n"
        "    Path('src/value.py').write_text('def value():\\n    return 2\\n')\n",
    )
    project.git_init()

    completed = project.run_vfy("test", "--engine=native", "--all", "-j", "1", "-q")

    completed.expect(0)
    assert (tmp_path / ".coverage").is_file()
    assert not (tmp_path / ".vfy/replay.coverage").exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native test engine does not support Windows",
)
def test_safe_selection_tracks_from_package_import_submodule(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("pkg/__init__.py", "")
    project.write("pkg/settings.py", "VALUE = 1\n\ndef identity():\n    return 0\n")
    project.write(
        "pkg/client.py", "from pkg import settings\n\ndef value():\n    return settings.VALUE\n"
    )
    project.write(
        "tests/test_direct.py",
        "import pkg.settings as settings\n\ndef test_direct():\n"
        "    assert settings.identity() == 0\n",
    )
    project.write(
        "tests/test_client.py",
        "from pkg.client import value\n\ndef test_value():\n    assert value() == 1\n",
    )
    project.git_init()
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    baseline = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "-j", "1", "-q", env=environment
    )
    baseline.expect(0)
    project.write("pkg/settings.py", "VALUE = 222\n\ndef identity():\n    return 0\n")
    selected = project.run_vfy("test", "--engine=pytest-native", "-j", "1", "-q", env=environment)
    full = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "-j", "1", "-q", env=environment
    )
    assert selected.returncode == full.returncode == 1, selected.stdout + selected.stderr
    assert "test_value" in selected.stdout


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native test engine does not support Windows",
)
def test_new_full_baseline_refreshes_committed_importers(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("pkg/__init__.py", "")
    project.write("pkg/settings.py", "VALUE = 1\n\ndef identity():\n    return 0\n")
    project.write(
        "tests/test_direct.py",
        "import pkg.settings as settings\n\ndef test_direct():\n"
        "    assert settings.identity() == 0\n",
    )
    project.git_init()
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    first = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "-j", "1", "-q", env=environment
    )
    first.expect(0)
    project.write(
        "pkg/client.py", "from pkg import settings\n\ndef value():\n    return settings.VALUE\n"
    )
    project.write(
        "tests/test_client.py",
        "from pkg.client import value\n\ndef test_value():\n    assert value() == 1\n",
    )
    project.git_commit("add a static dependent")
    baseline = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "-j", "1", "-q", env=environment
    )
    baseline.expect(0)
    project.write("pkg/settings.py", "VALUE = 222\n\ndef identity():\n    return 0\n")
    selected = project.run_vfy("test", "--engine=pytest-native", "-j", "1", "-q", env=environment)
    full = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "-j", "1", "-q", env=environment
    )
    assert selected.returncode == full.returncode == 1, selected.stdout + selected.stderr
    assert "test_value" in selected.stdout


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native test engine does not support Windows",
)
def test_reverted_import_uses_immutable_green_graph(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("pkg/__init__.py", "")
    project.write("pkg/base.py", "VALUE = 1\n")
    import_line = "from pkg.base import VALUE"
    local_line = "VALUE = 1".ljust(len(import_line))
    original = f"{import_line}\n\ndef read():\n    return VALUE\n"
    temporary = f"{local_line}\n\ndef read():\n    return VALUE + 0\n"
    dependent = project.write("pkg/dependent.py", original)
    project.write(
        "tests/test_dependent.py",
        "from pkg.dependent import read\n\ndef test_read():\n    assert read() == 1\n",
    )
    project.git_init()
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    baseline = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "-j", "1", "-q", env=environment
    )
    baseline.expect(0)
    baseline_mtime = dependent.stat().st_mtime_ns

    project.write("pkg/dependent.py", temporary)
    os.utime(dependent, ns=(baseline_mtime, baseline_mtime))
    selected = project.run_vfy("test", "--engine=pytest-native", "-j", "1", "-q", env=environment)
    selected.expect(0)
    assert "Reused 0 tests" in selected.stderr

    project.run("git", "restore", "pkg/dependent.py")
    os.utime(dependent, ns=(baseline_mtime, baseline_mtime))
    project.write("pkg/base.py", "VALUE = 222\n")
    reverted = project.run_vfy("test", "--engine=pytest-native", "-j", "1", "-q", env=environment)
    reverted.expect(1)
    assert "test_read" in reverted.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
def test_safe_selection_keeps_patch_headers_inside_changed_source(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("app.py", 'def value():\n    return """\n++ b/original.py\n"""\n')
    project.write(
        "test_app.py",
        "from app import value\n\ndef test_value():\n    assert 'original' in value()\n",
    )
    project.write("test_other.py", "def test_other():\n    assert True\n")
    project.git_init()
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    baseline = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "-j", "1", "-q", env=environment
    )
    baseline.expect(0)
    project.write("app.py", 'def value():\n    return """\n++ b/phantom.py\n"""\n')
    selected = project.run_vfy("test", "--engine=pytest-native", "-j", "1", "-q", env=environment)
    oracle = project.run_python("-m", "pytest", "-q", env=environment)
    assert selected.returncode == oracle.returncode == 1, selected.stdout + selected.stderr
    assert "Selected 1 / 2 tests" in selected.stderr
    assert "test_value" in selected.stdout
    assert "1 failed, 1 passed" in oracle.stdout, oracle.stdout + oracle.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
@pytest.mark.parametrize("subdirectory", [False, True], ids=["root", "subdirectory"])
def test_safe_selection_decodes_git_quoted_paths_from_repository_root(
    tmp_path: Path, subdirectory: bool
) -> None:
    repository = VfyProject(tmp_path)
    repository.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    repository.write("pkg/__init__.py", "")
    for module, test in (
        ("aï", "first"),
        ("middle", "plain"),
        ("zï", "last"),
        ("unrelated", "other"),
    ):
        repository.write(f"pkg/{module}.py", "def value():\n    return 1\n")
        repository.write(
            f"pkg/tests/test_{test}.py",
            f"from pkg.{module} import value\n\ndef test_{test}():\n    assert value() == 1\n",
        )
    repository.git_init()
    repository.run("git", "config", "core.quotePath", "true")
    project = VfyProject(tmp_path / "pkg") if subdirectory else repository
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONPATH": str(tmp_path)}
    baseline = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "-j", "1", "-q", env=environment
    )
    baseline.expect(0)
    for module in ("aï", "middle", "zï"):
        repository.write(f"pkg/{module}.py", "def value():\n    return 222\n")
    patch = repository.run("git", "diff", "HEAD", "--unified=0")
    assert '+++ "b/pkg/a\\303\\257.py"' in patch.stdout, patch.stdout
    selected = project.run_vfy("test", "--engine=pytest-native", "-j", "1", "-q", env=environment)
    oracle = project.run_python("-m", "pytest", "-q", env=environment)
    assert selected.returncode == oracle.returncode == 1, selected.stdout + selected.stderr
    assert "Selected 3 / 4 tests" in selected.stderr
    for test in ("test_first", "test_plain", "test_last"):
        assert test in selected.stdout, selected.stdout
    assert "3 failed, 1 passed" in oracle.stdout, oracle.stdout + oracle.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
def test_safe_selection_follows_relative_imports_in_package_initializers(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("pkg/__init__.py", "from .base import VALUE\n")
    project.write("pkg/base.py", "VALUE = 1\n\ndef identity():\n    return 0\n")
    project.write(
        "tests/test_direct.py",
        "from pkg.base import identity\n\ndef test_direct():\n    assert identity() == 0\n",
    )
    project.write(
        "tests/test_export.py",
        "import pkg\n\ndef test_export():\n    assert pkg.VALUE == 1\n",
    )
    project.write("tests/test_other.py", "def test_other():\n    assert True\n")
    project.git_init()
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    baseline = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "-j", "1", "-q", env=environment
    )
    baseline.expect(0)
    project.write("pkg/base.py", "VALUE = 222\n\ndef identity():\n    return 0\n")
    selected = project.run_vfy("test", "--engine=pytest-native", "-j", "1", "-q", env=environment)
    oracle = project.run_python("-m", "pytest", "-q", env=environment)
    assert selected.returncode == oracle.returncode == 1, selected.stdout + selected.stderr
    assert "Selected 2 / 3 tests" in selected.stderr
    assert "test_export" in selected.stdout, selected.stdout
    assert "1 failed, 2 passed" in oracle.stdout, oracle.stdout + oracle.stderr


def test_green_replay_runs_only_affected_tests_and_reconstructs_complete_coverage(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text(
        ".coverage\n.vfy/\n.runs\n.pytest_cache/\n__pycache__/\n", encoding="utf-8"
    )
    source = tmp_path / "src"
    tests = tmp_path / "tests"
    source.mkdir()
    tests.mkdir()
    (source / "__init__.py").write_text("", encoding="utf-8")
    first = source / "first.py"
    first.write_text(
        "def value(flag):\n    if flag:\n        return 1\n    return 0\n", encoding="utf-8"
    )
    second = source / "second.py"
    second.write_text(
        "def value(flag):\n    if flag:\n        return 2\n    return 0\n", encoding="utf-8"
    )
    (tests / "test_first.py").write_text(
        "from pathlib import Path\n"
        "from src.first import value\n\n"
        "def test_first():\n"
        "    Path('.runs').open('a').write('first\\n')\n"
        "    assert value(True) == 1\n",
        encoding="utf-8",
    )
    (tests / "test_second.py").write_text(
        "from pathlib import Path\n"
        "from src.second import value\n\n"
        "def test_second():\n"
        "    Path('.runs').open('a').write('second\\n')\n"
        "    assert value(True) == 2\n",
        encoding="utf-8",
    )
    project = VfyProject(tmp_path)
    project.git_init()

    def run(*arguments: str) -> int:
        return project.run_vfy(*arguments).returncode

    pytest_arguments = [
        "-q",
        "-n",
        "2",
        "--cov-decisions",
        "--cov-report=term",
        "--cov-report=json:coverage.json",
        "--cov-fail-under=60",
    ]
    command = ["test", "--engine=pytest", "--source", "src", *pytest_arguments]
    assert run("test", "--engine=pytest", "--all", "--source", "src", *pytest_arguments) == 0
    cache = tmp_path / ".vfy" / "replay.coverage"
    assert cache.is_file()
    assert sorted((tmp_path / ".runs").read_text(encoding="utf-8").splitlines()) == [
        "first",
        "second",
    ]

    (tmp_path / ".runs").unlink()
    cache.write_bytes(b"not a coverage database")
    assert run(*command) == 0
    assert sorted((tmp_path / ".runs").read_text(encoding="utf-8").splitlines()) == [
        "first",
        "second",
    ]
    ReferenceData(str(cache)).read()

    (tmp_path / ".runs").unlink()
    (tmp_path / ".coverage").unlink()
    assert run(*command) == 0
    assert not (tmp_path / ".runs").exists()
    assert (tmp_path / ".coverage").is_file()

    first.write_text(
        "def value(flag):\n    if flag:\n        return int('1')\n    return 0\n", encoding="utf-8"
    )
    assert run(*command) == 0

    assert (tmp_path / ".runs").read_text(encoding="utf-8").splitlines() == ["first"]
    saved = Coverage(data_file=str(tmp_path / ".coverage"), config_file=False, decisions=True)
    saved.load()
    semantic_contexts = {
        observation.context for observation in saved.get_decision_data().observations
    }
    reference_data = ReferenceData(str(tmp_path / ".coverage"))
    reference_data.read()
    contexts = {
        context
        for filename in reference_data.measured_files()
        for line_contexts in reference_data.contexts_by_lineno(filename).values()
        for context in line_contexts
        if context.endswith("|run")
    }
    assert contexts == {
        "tests/test_first.py::test_first|run",
        "tests/test_second.py::test_second|run",
    }
    assert {
        "tests/test_first.py::test_first|run",
        "tests/test_second.py::test_second|run",
    } <= semantic_contexts
    reference = ReferenceData(str(tmp_path / ".coverage"))
    reference.read()
    assert {str(first.resolve()), str(second.resolve())} <= set(reference.measured_files())
    json_report = json.loads((tmp_path / "coverage.json").read_text(encoding="utf-8"))
    second_report = json_report["files"]["src/second.py"]
    assert second_report["summary"]["covered_lines"] > 0

    green = cache.read_bytes()
    first.write_text(
        "def value(flag):\n    if flag:\n        return 99\n    return 0\n", encoding="utf-8"
    )
    assert run(*command) == 1
    assert cache.read_bytes() == green


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native test engine does not support Windows",
)
def test_incremental_result_matches_an_independent_full_run_after_edit(tmp_path: Path) -> None:
    """The selected workflow must preserve the same coverage as a full oracle."""

    source = tmp_path / "src"
    tests = tmp_path / "tests"
    source.mkdir()
    tests.mkdir()
    (source / "__init__.py").write_text("", encoding="utf-8")
    first = source / "first.py"
    second = source / "second.py"
    first.write_text("def value():\n    return 1\n", encoding="utf-8")
    second.write_text("def value():\n    return 2\n", encoding="utf-8")
    (tests / "test_first.py").write_text(
        "from src.first import value\n\ndef test_first():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    (tests / "test_second.py").write_text(
        "from src.second import value\n\ndef test_second():\n    assert value() == 2\n",
        encoding="utf-8",
    )
    project = VfyProject(tmp_path)
    project.git_init()

    def run(*arguments: str) -> int:
        return project.run_vfy(*arguments).returncode

    common = ["--source", "src", "-q", "--cov=src", "--cov-report=term"]
    assert run("test", "--engine=pytest-native", "--all", *common) == 0
    first.write_text("def value():\n    return int('1')\n", encoding="utf-8")

    selected_file = tmp_path / "selected.coverage"
    assert run("test", "--engine=pytest-native", "--data-file", str(selected_file), *common) == 0
    full_file = tmp_path / "full.coverage"
    assert (
        run("test", "--engine=pytest-native", "--all", "--data-file", str(full_file), *common) == 0
    )

    selected = ReferenceData(str(selected_file))
    full = ReferenceData(str(full_file))
    selected.read()
    full.read()
    assert selected.measured_files() == full.measured_files()
    for filename in full.measured_files():
        assert selected.lines(filename) == full.lines(filename)
        assert selected.arcs(filename) == full.arcs(filename)
        assert selected.contexts_by_lineno(filename) == full.contexts_by_lineno(filename)


@pytest.mark.parametrize(
    "engine",
    [
        pytest.param("native", marks=requires_native_pytest),
        pytest.param("pytest-native", marks=requires_native_pytest),
        "pytest",
    ],
)
@pytest.mark.parametrize("fault", ["missing", "corrupt", "mode"])
def test_reconstruction_failure_preserves_baseline_data_and_reports(
    tmp_path: Path, engine: str, fault: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("app.py", "def first():\n    return 1\ndef second():\n    return 2\n")
    project.write(
        "test_app.py",
        "import json, os\nfrom pathlib import Path\nfrom app import first, second\n"
        "def test_first():\n"
        "    assert first() == 1\n"
        "    marker = Path('.vfy/fault')\n"
        "    if marker.exists():\n"
        "        specification = json.loads(Path(os.environ['VFY_REPLAY_MERGE']).read_text())\n"
        "        baseline = Path(specification['baseline'])\n"
        "        fault = marker.read_text()\n"
        "        if fault == 'missing':\n"
        "            baseline.unlink()\n"
        "        elif fault == 'corrupt':\n"
        "            baseline.write_bytes(b'not a coverage database')\n"
        "        else:\n"
        "            from vfy import CoverageData\n"
        "            baseline.unlink()\n"
        "            data = CoverageData(basename=str(baseline))\n"
        "            data.add_lines({'app.py': [1]})\n"
        "            data.write()\n"
        "        Path('.vfy/injected').write_text(fault)\n"
        "def test_second():\n"
        "    assert second() == 2\n",
    )
    project.git_init()
    arguments = (
        "test",
        f"--engine={engine}",
        "--workers=1",
        "--cov=app",
        "--cov-branch",
        "--cov-context=test",
        "--cov-report=json:.vfy/report.json",
        "-q",
    )
    project.run_vfy(*arguments, "--all").expect(0)
    saved = {
        name: (tmp_path / name).read_bytes()
        for name in (".coverage", ".vfy/replay.coverage", ".vfy/report.json")
    }
    project.write("app.py", "def first():\n    return int('1')\ndef second():\n    return 2\n")
    project.write(".vfy/fault", fault)

    failed = project.run_vfy(*arguments)

    assert (tmp_path / ".vfy/injected").read_text() == fault
    assert failed.returncode != 0, failed.stdout + failed.stderr
    diagnostic = failed.stdout + failed.stderr
    assert (
        "cannot replay line and branch coverage together"
        if fault == "mode"
        else "replay-baseline.coverage"
    ) in diagnostic
    assert saved == {name: (tmp_path / name).read_bytes() for name in saved}
