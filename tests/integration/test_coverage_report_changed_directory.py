"""Relative source reports survive a test changing the process directory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from tests.support import VfyProject


def _report_after_direct_chdir(tmp_path: Path, engine: Literal["stock", "native"]) -> str:
    directory = tmp_path / engine
    project = VfyProject(directory)
    project.write(
        "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["src"]\ntestpaths = ["tests"]\n'
        '\n[tool.coverage.run]\nrelative_files = true\nsource = ["cwd_pkg"]\n'
        '\n[tool.coverage.paths]\nsource = ["src/cwd_pkg", '
        '".tox/*/lib/python*/site-packages/cwd_pkg"]\n',
    )
    project.write("src/cwd_pkg/__init__.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_chdir.py",
        "import os\nfrom cwd_pkg import value\n\n"
        "def test_value_after_direct_chdir(tmp_path):\n"
        "    os.chdir(tmp_path)\n"
        "    assert value() == 1\n",
    )
    report = tmp_path / f"{engine}-coverage.json"
    data_file = tmp_path / f".coverage-{engine}"
    options = (
        "-q",
        "--cov=cwd_pkg",
        "--cov-context=test",
        f"--cov-report=json:{report}",
        "tests",
    )
    environment = {
        "COVERAGE_FILE": str(data_file),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    result = (
        project.run_python("-m", "pytest", "-p", "pytest_cov.plugin", *options, env=environment)
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--workers=1", *options, env=environment)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert report.is_file(), result.stdout + result.stderr
    document = report.read_text()
    files = json.loads(document)["files"]
    assert set(files) == {"src/cwd_pkg/__init__.py"}
    source = files["src/cwd_pkg/__init__.py"]
    assert source["executed_lines"] == [1, 2]
    assert source["missing_lines"] == []
    data = project.data(data_file)
    assert set(data.measured_files()) == {"src/cwd_pkg/__init__.py"}
    assert data.lines("src/cwd_pkg/__init__.py") == [1, 2]
    assert data.contexts_by_lineno("src/cwd_pkg/__init__.py")[2] == [
        "tests/test_chdir.py::test_value_after_direct_chdir|run"
    ]
    return document


def test_coverage_report_resolves_initial_source_after_direct_chdir(tmp_path: Path) -> None:
    reports = {
        engine: json.loads(_report_after_direct_chdir(tmp_path, engine))["files"]
        for engine in ("stock", "native")
    }
    assert reports["native"] == reports["stock"]
