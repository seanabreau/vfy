"""Public pytest-asyncio declarations use one owned native execution service."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.independent_private_aliases import PRIVATE_ALIASES
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest


def _run(project: VfyProject, route: str, *plugins: str, autoload: bool = False) -> CommandResult:
    arguments = [argument for plugin in plugins for argument in ("-p", plugin)]
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None} if autoload else None
    return project.run_engine(
        route, *arguments, stock=("-q",), independent=("--no-header",), env=environment
    )


@requires_native_pytest
@pytest.mark.parametrize("disabled", [False, True])
@pytest.mark.parametrize("kind", ["coroutine", "returned", "fixture", "asyncgen"])
def test_disabled_asyncio_does_not_execute_unhandled_bodies(
    tmp_path: Path, disabled: bool, kind: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    sources = {
        "coroutine": "async def test_body():\n    open('body.txt', 'w').write('ran')\n",
        "returned": "async def body():\n    open('body.txt', 'w').write('ran')\n"
        "def test_body():\n    return body()\n",
        "fixture": "import pytest\n@pytest.fixture\nasync def resource():\n"
        "    open('setup.txt', 'w').write('ran')\n    return 1\n"
        "def test_body(resource):\n    open('body.txt', 'w').write('ran')\n",
        "asyncgen": "async def test_body():\n    open('body.txt', 'w').write('ran')\n    yield 1\n",
    }
    project.write("test_body.py", sources[kind])
    for route in ("stock", "independent"):
        result = _run(project, route, *(["no:asyncio"] if disabled else []))
        result.expect(1)
        assert (
            "with no plugin or hook" in result.stdout + result.stderr
            if kind == "fixture"
            else ("async def functions are not natively supported" in result.stdout + result.stderr)
        )
        assert not (tmp_path / "body.txt").exists()
        assert not (tmp_path / "setup.txt").exists()


@requires_native_pytest
@pytest.mark.parametrize("kind", ["sync", "coroutine", "asyncgen"])
def test_disabled_asyncio_rechecks_fixture_replaced_test_code(tmp_path: Path, kind: str) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_body.py",
            """from pathlib import Path
import pytest

@pytest.fixture
def replace(request, number):
    def sync(replace, number):
        Path(f'body-{number}.txt').write_text('sync')
    async def coroutine(replace, number):
        Path(f'body-{number}.txt').write_text('coroutine')
    async def asyncgen(replace, number):
        Path(f'body-{number}.txt').write_text('asyncgen')
        yield
    request.function.__code__ = locals()[KIND].__code__
    yield
    Path(f'finalized-{number}.txt').touch()

@pytest.mark.parametrize('number', [1, 2])
def test_body(replace, number):
    raise AssertionError('stale collected callable executed')
""".replace("KIND", repr(kind)),
        )
        result = (
            project.run_engine("stock", "-q", "-p", "no:asyncio", "--junitxml=junit.xml")
            if route == "stock"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--no-cov",
                "-q",
                "-p",
                "no:asyncio",
                "--junitxml=junit.xml",
            )
        )
        transcript = result.stdout + result.stderr
        assert result.returncode == (0 if kind == "sync" else 1), transcript
        if kind != "sync":
            assert "async def functions are not natively supported" in transcript
        cases = [
            (
                case.attrib["name"],
                [child.tag for child in case if child.tag in {"failure", "error"}],
            )
            for case in ET.parse(project.root / "junit.xml").iter("testcase")
        ]
        observations.append((cases, sorted(path.name for path in project.root.glob("*.txt"))))
    assert observations[1] == observations[0]
    assert observations[0][1] == (
        ["body-1.txt", "body-2.txt", "finalized-1.txt", "finalized-2.txt"]
        if kind == "sync"
        else ["finalized-1.txt", "finalized-2.txt"]
    )


@requires_native_pytest
@pytest.mark.parametrize("enabled", [False, True])
def test_importing_public_fixture_does_not_activate_asyncio(tmp_path: Path, enabled: bool) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_body.py",
        """import json, pytest_asyncio
from pathlib import Path
@pytest_asyncio.fixture
async def unused():
    raise AssertionError('unused fixture executed')
def test_services(request):
    config=request.config
    Path('services.json').write_text(json.dumps({
        'active':config.pluginmanager.hasplugin('asyncio'),
        'blocked':config.pluginmanager.is_blocked('asyncio'),
        'fixtures':request.fixturenames,
        'options':hasattr(config.option, 'asyncio_mode'),
        'version':pytest_asyncio.__version__,
    }))
""",
    )
    expected = None
    for route in ("stock", "independent"):
        result = _run(project, route, "asyncio" if enabled else "no:asyncio")
        result.expect(0)
        actual = json.loads((tmp_path / "services.json").read_text())
        if route == "stock":
            expected = actual
        else:
            assert actual == expected


@requires_native_pytest
@pytest.mark.parametrize(
    "plugins",
    [
        ("no:asyncio", "asyncio"),
        ("asyncio", "no:asyncio"),
        ("asyncio", "no:asyncio", "asyncio"),
    ],
)
def test_asyncio_activation_order_matches_stock(tmp_path: Path, plugins: tuple[str, ...]) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_body.py",
        """import json, pytest
from pathlib import Path
def test_services(request):
    config = request.config
    Path('services.json').write_text(json.dumps([
        config.pluginmanager.hasplugin('asyncio'),
        config.pluginmanager.is_blocked('asyncio'),
        hasattr(config.option, 'asyncio_mode'),
        request.fixturenames,
    ]))
@pytest.mark.asyncio
async def test_body():
    Path('body.txt').write_text('ran')
""",
    )
    expected = None
    for route in ("stock", "independent"):
        for name in ("services.json", "body.txt"):
            (tmp_path / name).unlink(missing_ok=True)
        result = _run(project, route, *plugins)
        services = tmp_path / "services.json"
        actual = (
            result.returncode,
            json.loads(services.read_text()) if services.exists() else None,
            (tmp_path / "body.txt").exists(),
        )
        if route == "stock":
            expected = actual
        else:
            assert actual == expected, result.stdout + result.stderr


@requires_native_pytest
def test_upstream_pytest11_activation_loads_owned_asyncio(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_body.py",
        "import asyncio, pytest, pytest_asyncio\n"
        "from importlib.metadata import version\n@pytest.mark.asyncio\n"
        "async def test_body(request):\n"
        "    assert request.config.pluginmanager.hasplugin('asyncio')\n"
        "    entries = [(plugin, distribution) for plugin, distribution in\n"
        "               request.config.pluginmanager.list_plugin_distinfo()\n"
        "               if distribution.metadata['Name'].lower() == 'pytest-asyncio']\n"
        "    assert len(entries) == 1\n"
        "    assert entries[0][0] is request.config.pluginmanager.get_plugin('asyncio')\n"
        "    assert entries[0][1].version == version('pytest-asyncio')\n"
        "    await asyncio.sleep(0)\n",
    )
    blockers = [
        "no:" + entry.name
        for entry in entry_points(group="pytest11")
        if entry.name not in {"asyncio", "pytest_cov"}
    ]
    for route in ("stock", "independent"):
        result = _run(project, route, "pytest_cov", *blockers, autoload=True)
        result.expect(0)


@requires_native_pytest
@pytest.mark.parametrize("activate_upstream", [False, True])
def test_cached_owned_asyncio_records_only_one_upstream_distribution(
    tmp_path: Path, activate_upstream: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_distribution.py",
        "from importlib.metadata import version\n"
        "def test_distribution(request):\n"
        "    manager = request.config.pluginmanager\n"
        "    entries = [(plugin, distribution) for plugin, distribution in\n"
        "               manager.list_plugin_distinfo()\n"
        "               if distribution.metadata['Name'].lower() == 'pytest-asyncio']\n"
        f"    assert len(entries) == {int(activate_upstream)}\n"
        "    for plugin, distribution in entries:\n"
        "        assert plugin is manager.get_plugin('asyncio')\n"
        "        assert distribution.version == version('pytest-asyncio')\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-q",
        *(("-p", "asyncio", "-p", "asyncio") if activate_upstream else ()),
    )
    result.expect(0)


@requires_native_pytest
@pytest.mark.parametrize(
    ("mode", "marked", "status"),
    [("strict", False, 1), ("strict", True, 0), ("auto", False, 0)],
)
def test_async_generator_discovery_matches_stock(
    tmp_path: Path, mode: str, marked: bool, status: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", f"[pytest]\nasyncio_mode={mode}\n")
    project.write(
        "test_body.py",
        "import pytest\nfrom pathlib import Path\n"
        + ("@pytest.mark.asyncio\n" if marked else "")
        + "async def test_body():\n    Path('body.txt').write_text('ran')\n    yield 1\n",
    )
    for route in ("stock", "independent"):
        result = _run(project, route, "asyncio")
        assert result.returncode == status, result.stdout + result.stderr
        assert not (tmp_path / "body.txt").exists()
        if status == 0:
            assert "1 xfailed" in result.stdout + result.stderr


@requires_native_pytest
def test_async_generator_preserves_user_xfail_precedence(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        "from pathlib import Path\ndef pytest_runtest_logreport(report):\n"
        "    if hasattr(report, 'wasxfail'):\n"
        "        Path('xfail.txt').write_text(report.when + ':' + report.wasxfail)\n",
    )
    project.write(
        "test_body.py",
        "import pytest\n@pytest.mark.asyncio\n"
        "@pytest.mark.xfail(reason='user reason', run=True)\n"
        "async def test_body():\n    yield 1\n",
    )
    for route in ("stock", "independent"):
        (tmp_path / "xfail.txt").unlink(missing_ok=True)
        result = _run(project, route, "asyncio")
        result.expect(0)
        assert (tmp_path / "xfail.txt").read_text() == "call:user reason"


def test_collection_warning_public_identity_matches_pytest(tmp_path: Path) -> None:
    observed = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_probe.py",
            """import json
from pathlib import Path
import pytest

def test_probe():
    category = pytest.PytestCollectionWarning
    Path('observed.json').write_text(json.dumps([
        category.__name__, category.__module__,
        [base.__name__ for base in category.__bases__],
        issubclass(category, pytest.PytestWarning), issubclass(category, UserWarning),
        str(category('collection message')),
    ]))
""",
        )
        result = project.run_engine(engine, "-q")
        result.expect(0)
        observed.append(json.loads((project.root / "observed.json").read_text()))
    assert observed[1] == observed[0]


@requires_native_pytest
@pytest.mark.parametrize("failure", [False, True])
def test_public_asyncio_fixture_runs_without_upstream_distributions(
    tmp_path: Path, failure: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("private_aliases.py", PRIVATE_ALIASES)
    environment = tmp_path / ".independent-python"
    created = project.run_python("-m", "venv", "--without-pip", str(environment))
    created.expect(0)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_body.py",
        f"""import asyncio, importlib.metadata, pytest, pytest_asyncio, sys
from pathlib import Path
from private_aliases import assert_private_aliases
@pytest_asyncio.fixture
async def resource():
    yield asyncio.get_running_loop()
    Path('closed.txt').write_text('closed')
@pytest.mark.asyncio
async def test_body(resource, request):
    assert resource is asyncio.get_running_loop()
    names = {{item.metadata['Name'].lower() for item in importlib.metadata.distributions()}}
    assert not names.intersection({{'pytest', 'pluggy', 'packaging', 'pytest-asyncio'}})
    assert_private_aliases()
    Path('body.txt').write_text('awaited')
    assert not {failure!r}, 'deliberate async failure'
""",
    )
    result = project.run(
        str(environment / "bin" / "python"),
        "-m",
        "vfy",
        "test",
        "--engine=native",
        "--no-cov",
        "--no-header",
        env={"PYTHONPATH": isolated_vfy_path()},
    )
    assert result.returncode == int(failure), result.stdout + result.stderr
    assert (tmp_path / "body.txt").read_text() == "awaited"
    assert (tmp_path / "closed.txt").read_text() == "closed"


@requires_native_pytest
@pytest.mark.parametrize("invalid", ["entrypoint", "duplicate"])
def test_asyncio_metadata_rejection_precedes_test_import_and_leaves_later_runs_working(
    tmp_path: Path, invalid: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_body.py",
        "from pathlib import Path\nPath('imported.txt').write_text('imported')\n"
        "def test_body(): pass\n",
    )
    project.write(
        "sitecustomize.py",
        """import importlib.metadata as metadata
import os
from types import SimpleNamespace

invalid = os.environ.get('FAKE_ASYNCIO_METADATA')
if invalid:
    value = 'wrong.plugin' if invalid == 'entrypoint' else 'pytest_asyncio.plugin'
    entry = metadata.EntryPoint(name='asyncio', value=value, group='pytest11')
    distribution = SimpleNamespace(
        metadata={'Name': 'pytest-asyncio'}, version='1.4.0', entry_points=[entry],
        locate_file=lambda path: path,
    )
    metadata.distribution = lambda name: distribution
    metadata.distributions = lambda: [distribution] * (2 if invalid == 'duplicate' else 1)
""",
    )
    arguments = ("test", "--engine=native", "--no-cov", "-q", "-p", "asyncio")
    rejected = project.run_vfy(
        *arguments, env={"PYTHONPATH": str(tmp_path), "FAKE_ASYNCIO_METADATA": invalid}
    )
    assert rejected.returncode != 0, rejected.stdout + rejected.stderr
    assert "vfy independent usage error" in rejected.stdout + rejected.stderr
    assert not (tmp_path / "imported.txt").exists()

    accepted = project.run_vfy(*arguments)
    accepted.expect(0)
    assert (tmp_path / "imported.txt").is_file()
