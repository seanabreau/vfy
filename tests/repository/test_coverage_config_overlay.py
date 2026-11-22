"""A shared subprocess workload preserves upstream configuration and child coverage."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tomllib
from pathlib import Path

import coverage
import pytest
from benchmarks.coverage_config_overlay import prepare_subprocess_config

from tests.support.vfy_project import VfyProject


@pytest.mark.parametrize("patches", [[], ["fork"]])
def test_dotted_toml_overlay_preserves_all_original_settings(
    tmp_path: Path, patches: list[str]
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    source = root / "pyproject.toml"
    source.write_text(
        '[project]\nname = "example"\nversion = "1"\n'
        '[tool.coverage]\nrun.parallel = true\nrun.plugins = ["fixture_plugin"]\n'
        f"run.patch = {json.dumps(patches)}\n"
        'paths.other = [".", "*/example"]\npaths.source = ["src", "**/src"]\n'
        "report.fail_under = 76\nhtml.show_contexts = true\n"
        '[tool.coverage.fixture_plugin]\nunknown_setting = ["kept"] # retain plugin comment\n'
    )
    before = source.read_bytes()
    destination = tmp_path / "artifacts" / "coverage.toml"
    record = prepare_subprocess_config(Path(sys.executable), root, destination)
    expected = copy.deepcopy(tomllib.loads(before.decode()))
    expected["tool"]["coverage"]["run"]["patch"] = [*patches, "subprocess"]
    assert tomllib.loads(destination.read_text()) == expected
    assert source.read_bytes() == before
    assert "# retain plugin comment" in destination.read_text()
    original = coverage.Coverage(config_file=str(source))
    derived = coverage.Coverage(config_file=str(destination))
    for name in ("run:parallel", "run:plugins", "paths", "report:fail_under", "html:show_contexts"):
        assert derived.get_option(name) == original.get_option(name)
    assert derived.config.plugin_options == original.config.plugin_options
    assert record["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert record["config_sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()


def test_shared_subprocess_overlay_collects_child_only_module_with_both_engines(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path / "project")
    project.write("app/__init__.py", "")
    project.write("app/parent.py", "def value():\n    return 1\n")
    project.write("app/child.py", "def value():\n    return 2\n")
    project.write(
        "pyproject.toml",
        '[tool.coverage]\nrun.source = ["app"]\nrun.parallel = true\nreport.fail_under = 100\n',
    )
    project.write(
        "test_app.py",
        "import subprocess, sys\nfrom app.parent import value\n"
        "def test_parent():\n    assert value() == 1\n"
        "    subprocess.run([sys.executable, '-c', "
        "'from app.child import value; assert value() == 2'], check=True)\n",
    )
    overlay = tmp_path / "artifacts" / "coverage.toml"
    prepare_subprocess_config(Path(sys.executable), project.root, overlay)
    for engine in ("stock", "native"):
        result = project.run_engine(
            engine,
            "--cov=app",
            f"--cov-config={overlay}",
            "--cov-context=test",
            "--cov-report=term",
            "-q",
            stock=("-p", "pytest_cov.plugin"),
            independent=("--workers=1", "--all"),
            no_cov=False,
            env={"COVERAGE_FILE": str(project.root / f".coverage.{engine}")},
        )
        result.expect(0)
    assert project.lines("app/child.py", relative=".coverage.stock") == [1, 2]
    assert project.lines("app/child.py", relative=".coverage.native") == [1, 2]
