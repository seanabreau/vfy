"""Reject an unverified xdist module before its lifecycle hooks run."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("version_mismatch", [False, True])
def test_untrusted_xdist_module_fails_before_configure(
    tmp_path: Path, version_mismatch: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("xdist/__init__.py", "")
    project.write(
        "xdist/plugin.py",
        "from pathlib import Path\n"
        "def pytest_configure(config):\n    Path('configured.txt').touch()\n",
    )
    project.write("test_app.py", "def test_pass():\n    assert True\n")
    if version_mismatch:
        project.write("pytest_xdist-99.dist-info/METADATA", "Name: pytest-xdist\nVersion: 99\n")
    completed = project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-p", "xdist.plugin")
    assert completed.returncode != 0
    transcript = completed.stdout + completed.stderr
    assert "requires the installed upstream xdist plugin" in transcript
    assert not (project.root / "configured.txt").exists()
