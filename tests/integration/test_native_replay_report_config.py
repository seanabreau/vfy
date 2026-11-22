"""Native replay reports use the same coverage configuration as collection."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_replayed_report_honors_explicit_cov_config(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pyproject.toml",
        '[tool.coverage.report]\nexclude_lines = ["return 42"]\n',
    )
    project.write(".coveragerc", "[run]\nsource = src\n")
    project.write("src/__init__.py", "")
    project.write("src/app.py", "def answer():\n    return 42\n")
    project.write(
        "test_app.py", "from src.app import answer\ndef test_answer():\n    assert answer() == 42\n"
    )
    project.write(".gitignore", ".vfy/\n.coverage\ncoverage.json\n.pytest_cache/\n__pycache__/\n")
    project.git_init()
    arguments = (
        "test",
        "--engine=pytest-native",
        "--workers=1",
        "--source=src",
        "--cov-config=.coveragerc",
        "--cov-report=json:coverage.json",
        "-q",
    )

    first = project.run_vfy(*arguments)
    first.expect(0)
    expected = json.loads((tmp_path / "coverage.json").read_text())
    second = project.run_vfy(*arguments)
    second.expect(0)
    assert "Reused 1 tests from passing baseline" in second.stderr
    actual = json.loads((tmp_path / "coverage.json").read_text())
    expected["meta"].pop("timestamp")
    actual["meta"].pop("timestamp")
    assert actual == expected
