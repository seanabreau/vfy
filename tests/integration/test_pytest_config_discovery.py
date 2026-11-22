"""Public proof of pytest's configuration discovery before plug-in activation."""

from __future__ import annotations

import shlex
from importlib.metadata import version
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = pytest.mark.skipif(
    version("pytest") != "9.1.1",
    reason="configuration discovery differential uses pinned pytest 9.1.1",
)


@requires_native_pytest
@pytest.mark.parametrize("source", ["cli", "environment"])
@pytest.mark.parametrize("attached", [False, True], ids=["separate", "equals"])
@pytest.mark.parametrize(
    "plugin_request",
    [
        [],
        ["-p", "vfy.pytest_plugin"],
        ["-pvfy.pytest_plugin"],
        ["-qp", "vfy.pytest_plugin"],
    ],
)
def test_public_discovery_precedes_positive_plugin_requests(
    tmp_path: Path, source: str, attached: bool, plugin_request: list[str]
) -> None:
    for native in (False, True):
        project = VfyProject(tmp_path / ("native" if native else "stock"))
        # Only the selected configuration permits this invocation to complete.
        outer, inner = (
            ("-p nonexistent_plugin", "--no-cov")
            if attached
            else ("--no-cov", "-p nonexistent_plugin")
        )
        project.write("pyproject.toml", f'[tool.pytest.ini_options]\naddopts = "{outer}"\n')
        project.write("tests/pytest.ini", f"[pytest]\naddopts = {inner}\n")
        project.write(
            "tests/test_good.py",
            "from pathlib import Path\n"
            "def test_good(): Path('test-ran.txt').write_text('passed')\n",
        )
        project.write(".coveragerc", "[run]\n")
        arguments = [
            *plugin_request,
            "tests",
            *(["--cov-config=.coveragerc"] if attached else ["--cov-config", ".coveragerc"]),
            "--cov-report=",
            "-q",
        ]
        env = {"PYTEST_ADDOPTS": shlex.join(arguments) if source == "environment" else None}
        cli = arguments if source == "cli" else []
        # The native controller manages the adapter; explicit vfy coverage
        # requests remain supported and are still activated after discovery.
        result = (
            project.run_vfy("test", "--engine=pytest-native", "--workers=1", *cli, env=env)
            if native
            else project.run_pytest("-p", "vfy.pytest_plugin", *cli, env=env, load_xdist=False)
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (project.root / "test-ran.txt").read_text() == "passed"
        assert not (project.root / ".coverage").exists()
        assert not (project.root / ".vfy/replay.coverage").exists()
