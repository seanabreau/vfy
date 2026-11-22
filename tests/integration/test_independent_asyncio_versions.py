"""Installed asyncio versions retain admission and public metadata behavior."""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_installed_asyncio_version_and_fixture_lifecycle_match_reference(tmp_path: Path) -> None:
    installed = version("pytest-asyncio")
    assert installed in {"1.4.0", "1.4.1", "1.4.1.post1"}
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\nasyncio_mode = strict\n")
        project.write(
            "test_async.py",
            """import asyncio
import json
from importlib.metadata import version
from pathlib import Path
import pytest
import pytest_asyncio

@pytest_asyncio.fixture
async def resource():
    with Path('events').open('a') as output:
        output.write('setup\\n')
    yield 42
    with Path('events').open('a') as output:
        output.write('teardown\\n')

@pytest.mark.asyncio
async def test_async(resource, request):
    assert resource == 42
    assert asyncio.get_running_loop().is_running()
    entries = [(plugin, distribution) for plugin, distribution in
               request.config.pluginmanager.list_plugin_distinfo()
               if distribution.metadata['Name'].lower() == 'pytest-asyncio']
    assert len(entries) == 1
    assert entries[0][0] is request.config.pluginmanager.get_plugin('asyncio')
    Path('versions.json').write_text(json.dumps([
        version('pytest-asyncio'), pytest_asyncio.__version__, entries[0][1].version,
    ]))
    with Path('events').open('a') as output:
        output.write('test\\n')
""",
        )
        result = (
            project.run_python("-m", "pytest", "-q", "-p", "asyncio")
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", "-p", "asyncio")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observations.append(
            (
                json.loads((project.root / "versions.json").read_text()),
                (project.root / "events").read_text(),
            )
        )
    assert observations == [([installed] * 3, "setup\ntest\nteardown\n")] * 2


@pytest.mark.parametrize("unsupported", ["1.4.0.post1", "1.4.1.post2", "1.4.2"])
def test_unverified_asyncio_versions_are_rejected_before_collection(
    tmp_path: Path, unsupported: str
) -> None:
    project = VfyProject(tmp_path)
    metadata = f"pytest_asyncio-{unsupported}.dist-info"
    project.write(
        f"{metadata}/METADATA",
        f"Metadata-Version: 2.1\nName: pytest-asyncio\nVersion: {unsupported}\n",
    )
    project.write(f"{metadata}/entry_points.txt", "[pytest11]\nasyncio = pytest_asyncio.plugin\n")
    project.write("test_body.py", "from pathlib import Path\nPath('collected').touch()\n")
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q", "-p", "asyncio")
    assert result.returncode != 0, result.stdout + result.stderr
    assert f"requires pytest-asyncio 1.4.0, 1.4.1, or 1.4.1.post1; found {unsupported}" in (
        result.stdout + result.stderr
    )
    assert not (project.root / "collected").exists()
