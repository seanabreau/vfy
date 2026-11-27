"""Inactive installed plugins do not impose independent adapter requirements."""

from __future__ import annotations

import json
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("autoload", [False, True])
def test_inactive_asyncio_metadata_does_not_reject_synchronous_tests(
    tmp_path: Path, autoload: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "pytest_asyncio-0.0.dist-info/METADATA",
        "Metadata-Version: 2.1\nName: pytest-asyncio\nVersion: 0.0\n",
    )
    project.write(
        "pytest_asyncio-0.0.dist-info/entry_points.txt",
        "[pytest11]\nasyncio = inactive_asyncio\n",
    )
    project.write("inactive_asyncio.py", "raise AssertionError('inactive plugin imported')\n")
    project.write(
        "test_body.py",
        """import importlib.metadata, json
from pathlib import Path

def test_body(request):
    config = request.config
    Path('result.json').write_text(json.dumps([
        importlib.metadata.version('pytest-asyncio'),
        config.pluginmanager.hasplugin('asyncio'),
        config.pluginmanager.is_blocked('asyncio'),
    ]))
""",
    )
    plugins = (
        ["no:asyncio"]
        + [
            "no:" + entry.name
            for entry in entry_points(group="pytest11")
            if entry.name not in {"asyncio", "pytest_cov"}
        ]
        if autoload
        else []
    )
    arguments = [argument for plugin in plugins for argument in ("-p", plugin)]
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None} if autoload else None
    for route in ("stock", "independent"):
        result_path = tmp_path / "result.json"
        result_path.unlink(missing_ok=True)
        result = (
            project.run_python("-m", "pytest", "-q", *arguments, env=environment)
            if route == "stock"
            # The raw frontend follows pytest autoload policy; vfy test supplies built-ins.
            else project.run_python("-m", "vfy.independent.entry", *arguments, env=environment)
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result_path.read_text()) == ["0.0", False, autoload]


@pytest.mark.parametrize("plugin", [None, "no:asyncio", "pytest_asyncio.plugin", "conftest"])
def test_vfy_owned_asyncio_ignores_inactive_metadata_and_preserves_explicit_policy(
    tmp_path: Path, plugin: str | None
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nmarkers = asyncio: async test\n")
    project.write(
        "pytest_asyncio-0.0.dist-info/METADATA",
        "Metadata-Version: 2.1\nName: pytest-asyncio\nVersion: 0.0\n",
    )
    project.write(
        "pytest_asyncio-0.0.dist-info/entry_points.txt",
        "[pytest11]\nasyncio = unsupported_asyncio\n",
    )
    project.write(
        "unsupported_asyncio.py",
        "from pathlib import Path\nPath('upstream-imported').touch()\n"
        "raise AssertionError('upstream plugin imported')\n",
    )
    project.write(
        "test_body.py",
        """import asyncio
from pathlib import Path
import pytest

@pytest.mark.asyncio
async def test_body():
    assert asyncio.get_running_loop().is_running()
    Path('awaited').touch()
""",
    )
    if plugin == "conftest":
        project.write("conftest.py", "pytest_plugins = ['pytest_asyncio.plugin']\n")
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-q",
        *(["-p", plugin] if plugin not in (None, "conftest") else []),
    )
    output = result.stdout + result.stderr
    if plugin is None:
        assert result.returncode == 0, output
        assert (tmp_path / "awaited").is_file()
    elif plugin == "no:asyncio":
        assert result.returncode == 1, output
        assert "async def functions are not natively supported" in output
    else:
        assert result.returncode != 0, output
        assert "requires pytest-asyncio 1.4.0, 1.4.1, 1.4.1.post1, or 1.5.0; found 0.0" in output
    assert (tmp_path / "awaited").exists() is (plugin is None)
    assert not (tmp_path / "upstream-imported").exists()
