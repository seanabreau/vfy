"""CLI startup preserves pytest's disabled built-in plug-ins."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@pytest.mark.parametrize(
    "engine", [pytest.param("pytest-native", marks=requires_native_pytest), "pytest"]
)
def test_disabled_builtin_is_not_imported_during_cli_preload(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_example.py",
        "import sys\n\n"
        "def test_disabled_pastebin():\n"
        "    assert '_pytest.pastebin' not in sys.modules\n",
    )
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    arguments = ("-p", "no:pastebin", "-q")
    reference = project.run_python("-m", "pytest", *arguments, env=environment)
    reference.expect(0)

    result = project.run_vfy(
        "test", f"--engine={engine}", "-j1", "--no-cov", *arguments, env=environment
    )
    assert result.returncode == reference.returncode, result.stdout + result.stderr
    assert "1 passed" in result.stdout
