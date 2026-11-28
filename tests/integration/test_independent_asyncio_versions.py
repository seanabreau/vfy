"""Installed asyncio versions retain admission and public metadata behavior."""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest
_LEGACY_REFERENCE = version("pytest-asyncio").startswith("1.4.")


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
@pytest.mark.parametrize(
    "fixture_scope",
    [
        None,
        pytest.param(
            "fixture-scope",
            marks=pytest.mark.skipif(_LEGACY_REFERENCE, reason="requires pytest-asyncio 1.5.0"),
        ),
    ],
    ids=["unset", "explicit"],
)
def test_default_fixture_scope_matches_caching_scope(
    tmp_path: Path, workers: int, fixture_scope: str | None
) -> None:
    configuration = "[pytest]\n"
    stock_warnings: tuple[str, ...] = ()
    if fixture_scope is not None:
        configuration += f"asyncio_default_fixture_loop_scope = {fixture_scope}\n"
    else:
        stock_warnings = (
            "-W",
            'ignore:The configuration option "asyncio_default_fixture_loop_scope" is unset.'
            ":pytest.PytestDeprecationWarning",
        )
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", configuration)
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
            project.run_python("-m", "pytest", *options, *stock_warnings)
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


@pytest.mark.skipif(not _LEGACY_REFERENCE, reason="requires pytest-asyncio 1.4.x")
def test_legacy_adapter_rejects_explicit_fixture_scope_before_collection(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope = fixture-scope\n")
    project.write("test_body.py", "from pathlib import Path\nPath('collected').touch()\n")
    options = ("-q", "-p", "asyncio")
    stock = project.run_python("-m", "pytest", *options)
    independent = project.run_vfy("test", "--engine=native", "--no-cov", *options)
    for result in (stock, independent):
        result.expect(4)
        assert "'fixture-scope' is not a valid asyncio_default_fixture_loop_scope." in result.stderr
        assert "Valid scopes are: function, class, module, package, session." in result.stderr
    assert not (project.root / "collected").exists()


@pytest.mark.parametrize("unsupported", ["1.4.0.post1", "1.4.1.post2", "1.4.2", "1.5.1"])
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
    assert f"1.4.0, 1.4.1, 1.4.1.post1, or 1.5.0; found {unsupported}" in (
        result.stdout + result.stderr
    )
    assert not (project.root / "collected").exists()
