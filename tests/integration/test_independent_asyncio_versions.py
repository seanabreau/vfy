"""Installed asyncio versions retain admission and public metadata behavior."""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.version import Version

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest
_LEGACY_REFERENCE = Version(version("pytest-asyncio")) < Version("1.5")


def test_installed_asyncio_version_and_fixture_lifecycle_match_reference(tmp_path: Path) -> None:
    installed = version("pytest-asyncio")
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


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("fixture_scope", [None, "fixture-scope"], ids=["unset", "explicit"])
def test_default_fixture_scope_matches_caching_scope(
    tmp_path: Path, workers: int, fixture_scope: str | None
) -> None:
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        # Before 1.5, stock selects the fixture's caching scope by leaving the option unset.
        scope = None if route == "stock" and _LEGACY_REFERENCE else fixture_scope
        project.write(
            "pytest.ini",
            "[pytest]\n" + (f"asyncio_default_fixture_loop_scope = {scope}\n" if scope else ""),
        )
        for name in ("test_first", "test_second"):
            project.write(
                f"{name}.py",
                """import asyncio
import os
from pathlib import Path

import pytest
import pytest_asyncio

events = Path(__name__ + '.events')

@pytest_asyncio.fixture(scope='module')
async def module_loop():
    loop = asyncio.get_running_loop()
    events.with_suffix('.pid').write_text(str(os.getpid()))
    events.write_text('setup\\n')
    yield loop
    assert asyncio.get_running_loop() is loop
    with events.open('a') as output:
        output.write('teardown\\n')

@pytest_asyncio.fixture(scope='module', loop_scope='session')
async def explicit_loop():
    return asyncio.get_running_loop()

@pytest.mark.asyncio(loop_scope='module')
@pytest.mark.parametrize('case', ['a', 'b'])
async def test_loop(module_loop, explicit_loop, case):
    assert asyncio.get_running_loop() is module_loop
    assert explicit_loop is not module_loop
    with events.open('a') as output:
        output.write(case + '\\n')
""",
            )
        options = ("-q", "-p", "asyncio", "-W", "error")
        result = (
            project.run_python(
                "-m",
                "pytest",
                *options,
                "-W",
                'ignore:The configuration option "asyncio_default_fixture_loop_scope" is unset.'
                ":pytest.PytestDeprecationWarning",
            )
            if route == "stock"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--no-cov",
                f"--workers={workers}",
                "--native-scheduling=file",
                "--native-result-json=results.json",
                *options,
            )
        )
        result.expect(0)
        assert "4 passed" in result.stdout
        for name in ("test_first", "test_second"):
            assert (project.root / f"{name}.events").read_text() == "setup\na\nb\nteardown\n"
        if route == "independent" and workers == 2:
            worker_pids = {
                int((project.root / f"{name}.pid").read_text())
                for name in ("test_first", "test_second")
            }
            assert len(worker_pids) == 2
            topology = json.loads((project.root / "results.json").read_text())["execution_topology"]
            assert set(topology["worker_pids"]) == worker_pids


@pytest.mark.parametrize(
    ("installed", "entrypoint"),
    [("0.23.8", True), ("1.4+vendor.1", True), ("2.0a1", True), ("99.0", False)],
)
def test_native_asyncio_runs_independently_of_installed_version(
    tmp_path: Path, installed: str, entrypoint: bool
) -> None:
    project = VfyProject(tmp_path)
    environment = tmp_path / ".venv"
    project.run_python("-m", "venv", "--without-pip", str(environment)).expect(0)
    metadata = f"pytest_asyncio-{installed}.dist-info"
    project.write(
        f"{metadata}/METADATA",
        f"Metadata-Version: 2.1\nName: pytest-asyncio\nVersion: {installed}\n",
    )
    if entrypoint:
        project.write(
            f"{metadata}/entry_points.txt", "[pytest11]\nasyncio = pytest_asyncio.plugin\n"
        )
    project.write(
        "test_body.py",
        """from pathlib import Path
import pytest
import pytest_asyncio

Path("collected").touch()

@pytest.mark.asyncio
async def test_version():
    Path("ran").write_text(pytest_asyncio.__version__)
""",
    )
    result = project.run(
        str(environment / "bin" / "python"),
        "-m",
        "vfy",
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "asyncio",
        "-q",
        env={"PYTHONPATH": isolated_vfy_path()},
    )
    result.expect(0)
    assert (tmp_path / "ran").read_text() == installed


def test_asyncio_entry_point_must_belong_to_pytest_asyncio(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    environment = tmp_path / ".venv"
    project.run_python("-m", "venv", "--without-pip", str(environment)).expect(0)
    for name in ("pytest_asyncio", "other"):
        project.write(
            f"{name}-1.5.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.5\n",
        )
    project.write(
        "other-1.5.dist-info/entry_points.txt", "[pytest11]\nasyncio = pytest_asyncio.plugin\n"
    )
    project.write("test_body.py", "from pathlib import Path\nPath('collected').touch()\n")
    result = project.run(
        str(environment / "bin" / "python"),
        "-m",
        "vfy",
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "asyncio",
        env={"PYTHONPATH": isolated_vfy_path()},
    )
    result.expect(4)
    assert "expected one asyncio = pytest_asyncio.plugin from pytest-asyncio" in result.stderr
    assert not (tmp_path / "collected").exists()
