"""An absent CodSpeed package is not synthesized by plugin admission."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
@pytest.mark.skipif(
    importlib.util.find_spec("pytest_codspeed") is not None,
    reason="requires an environment without pytest-codspeed",
)
def test_absent_codspeed_preserves_module_import_error(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_absent.py",
        "import importlib.util\n"
        "from pathlib import Path\n"
        "def test_absent():\n"
        "    assert importlib.util.find_spec('pytest_codspeed') is None\n"
        "    Path('ran').touch()\n",
    )
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    result.expect(0)
    assert (tmp_path / "ran").exists()
    (tmp_path / "ran").unlink()

    result = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-q", "-p", "pytest_codspeed.plugin"
    )
    assert result.returncode != 0
    assert "No module named 'pytest_codspeed'" in result.stdout + result.stderr
    assert not (tmp_path / "ran").exists()
