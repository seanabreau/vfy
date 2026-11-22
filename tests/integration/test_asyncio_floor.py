"""Installed-wheel asyncio contract with the pinned pytest runtime."""

from __future__ import annotations

from pathlib import Path

from tests.support.vfy_project import VfyProject


def test_pinned_pytest_runs_strict_and_auto_async_fixtures(tmp_path: Path) -> None:
    for directory, fixture, mode in (
        ("strict", "@vfy.async_fixture", "strict"),
        ("auto", "@pytest.fixture", "auto"),
    ):
        project = VfyProject(tmp_path / directory)
        source = project.write(
            "test_request.py",
            "import asyncio\n"
            "import vfy\n"
            "import pytest\n\n"
            f"{fixture}\n"
            "async def client():\n"
            "    await asyncio.sleep(0)\n"
            "    return object()\n\n"
            "@pytest.mark.asyncio\n"
            "async def test_request(client):\n"
            "    assert client is not None\n",
        )
        result = project.run_python(
            "-m",
            "pytest",
            "-p",
            "vfy.asyncio_plugin",
            "-p",
            "vfy.pytest_plugin",
            "--asyncio-mode",
            mode,
            "-o",
            "asyncio_default_fixture_loop_scope=function",
            "--cov=.",
            "--cov-branch",
            "--cov-context=test",
            "--cov-report=",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        data = project.data()
        assert data.lines(str(source))
        assert data.arcs(str(source))
        assert any(context.endswith("test_request|run") for context in data.measured_contexts())


def test_public_api_reflection_does_not_activate_a_pytest_plugin(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    result = project.run_python(
        "-c",
        "import importlib.abc, sys\n"
        "attempts = []\n"
        "class BlockRunner(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname.partition('.')[0] in {'pytest', '_pytest', 'pluggy', 'packaging'}:\n"
        "            attempts.append(fullname)\n"
        "            raise ModuleNotFoundError(fullname)\n"
        "sys.meta_path.insert(0, BlockRunner())\n"
        "import vfy\n"
        "for name in dir(vfy):\n"
        "    getattr(vfy, name)\n"
        "assert vfy.asyncio.fixture is vfy.async_fixture\n"
        "assert 'vfy.asyncio_plugin' not in sys.modules\n"
        "assert not attempts, attempts\n",
    )
    result.expect(0)
