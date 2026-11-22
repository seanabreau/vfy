"""Unavailable independent runners reject invocations before test-side effects."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.skipif(sys.platform != "win32", reason="Windows runner availability")
@pytest.mark.parametrize("engine", [[], ["--engine=native"]], ids=["default", "explicit"])
def test_unsupported_independent_runner_preserves_evidence(
    tmp_path: Path, engine: list[str]
) -> None:
    project = VfyProject(tmp_path)
    previous = b"previous evidence"
    (tmp_path / ".coverage").write_bytes(previous)
    project.write("conftest.py", "from pathlib import Path\nPath('imported').touch()\n")
    project.write("test_body.py", "def test_body(): pass\n")
    result = project.run_vfy("test", *engine, "--all")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "independent pytest is supported only on Linux and macOS" in result.stderr
    assert not (tmp_path / "imported").exists()
    assert (tmp_path / ".coverage").read_bytes() == previous


@pytest.mark.skipif(sys.platform != "win32", reason="portable Windows configuration projection")
def test_independent_configuration_errors_remain_available_on_windows(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = 'unterminated\n")
    result = project.run_vfy("test", "--engine=native", "--no-cov")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "invalid addopts" in result.stderr
