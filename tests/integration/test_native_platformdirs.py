"""The real Platformdirs fixture plugin restores process-local directory state."""

from __future__ import annotations

import json
import shutil
import sys
import venv
from importlib.metadata import distribution, entry_points, version
from importlib.util import find_spec
from pathlib import Path

import platformdirs
import pytest
from packaging.version import Version
from validation.pytest_runner.check_native_xdist_adoption import junit_records

import vfy
from tests.support import VfyProject

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="collected fork requires Linux")


@pytest.mark.parametrize(
    "route",
    [
        "serial",
        "workers",
        pytest.param(
            "xdist",
            marks=pytest.mark.skipif(find_spec("xdist") is None, reason="xdist is optional"),
        ),
    ],
)
def test_platformdirs_entrypoint_fixtures_match_stock(tmp_path: Path, route: str) -> None:
    """Installed fixture entry points survive vfy's controlled plugin loading."""
    blocked = [
        argument
        for plugin in entry_points(group="pytest11")
        if plugin.name not in {"platformdirs", "xdist", "xdist.looponfail"}
        for argument in ("-p", f"no:{plugin.name}")
    ]
    for engine in ("stock", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """
import platformdirs
import pytest

import vfy

@pytest.fixture
def restored():
    saved = dict(vars(platformdirs.PlatformDirs))
    baseline = platformdirs.user_data_path('sentinel')
    yield
    assert dict(vars(platformdirs.PlatformDirs)) == saved
    assert platformdirs.user_data_path('sentinel') == baseline

@pytest.fixture
def platformdirs_isolated(restored, platformdirs_isolated):
    return platformdirs_isolated
""",
        )
        for index in range(2):
            project.write(
                f"test_paths_{index}.py",
                """
import platformdirs
import platformdirs.pytest_plugin as plugin

def test_isolated(platformdirs_isolated, tmp_path, request):
    assert request.config.pluginmanager.getplugin('platformdirs') is plugin
    assert platformdirs_isolated == tmp_path
    assert platformdirs.user_data_path('sentinel') == tmp_path / 'user_data' / 'sentinel'
""",
            )
        arguments = [*blocked, "-q"]
        if route == "xdist":
            arguments.extend(("--numprocesses", "2", "--dist=load"))
        environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}
        completed = (
            project.run_python("-m", "pytest", *arguments, env=environment)
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                *(["--workers=2"] if route == "workers" else []),
                "--no-cov",
                *arguments,
                env=environment,
            )
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "2 passed" in completed.stdout


@pytest.mark.parametrize(
    "disabled",
    [
        "environment",
        pytest.param(
            "option",
            marks=pytest.mark.skipif(
                Version(pytest.__version__) < Version("8.4"),
                reason="--disable-plugin-autoload requires pytest 8.4",
            ),
        ),
        "platformdirs",
        "platformdirs.pytest_plugin",
    ],
)
def test_platformdirs_automatic_loading_respects_opt_out(tmp_path: Path, disabled: str) -> None:
    blocked = [
        argument
        for plugin in entry_points(group="pytest11")
        if plugin.name != "platformdirs"
        for argument in ("-p", f"no:{plugin.name}")
    ]
    # Pytest blocks entry-point discovery by its registration name. Blocking
    # the import-module alias alone leaves the canonical entry point enabled.
    enabled = disabled == "platformdirs.pytest_plugin"
    for engine in ("stock", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_paths.py",
            f"""
import pytest

import vfy

def test_loading(request, tmp_path):
    manager = request.config.pluginmanager
    assert manager.hasplugin('platformdirs') is {enabled}
    assert not manager.hasplugin('platformdirs.pytest_plugin')
    if {enabled}:
        assert request.getfixturevalue('platformdirs_isolated') == tmp_path
    else:
        with pytest.raises(pytest.FixtureLookupError):
            request.getfixturevalue('platformdirs_isolated')
""",
        )
        arguments = [*blocked, "-q"]
        if disabled == "option":
            arguments.append("--disable-plugin-autoload")
        elif disabled != "environment":
            arguments.extend(("-p", f"no:{disabled}"))
        environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1" if disabled == "environment" else None}
        completed = (
            project.run_python("-m", "pytest", *arguments, env=environment)
            if engine == "stock"
            else project.run_vfy(
                "test", "--engine=pytest-native", "--no-cov", *arguments, env=environment
            )
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "1 passed" in completed.stdout


@pytest.mark.parametrize("plugin_name", ["platformdirs", "platformdirs.pytest_plugin"])
def test_explicit_platformdirs_loading_with_autoload_disabled(
    tmp_path: Path, plugin_name: str
) -> None:
    for engine in ("stock", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_paths.py",
            "def test_paths(platformdirs_isolated, tmp_path):\n"
            "    assert platformdirs_isolated == tmp_path\n",
        )
        arguments = ("-p", plugin_name, "-q")
        completed = (
            project.run_python("-m", "pytest", *arguments)
            if engine == "stock"
            else project.run_vfy("test", "--engine=pytest-native", "--no-cov", *arguments)
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "1 passed" in completed.stdout


@pytest.mark.parametrize(
    "declaration", ["['platformdirs.pytest_plugin']", "'platformdirs.pytest_plugin'"]
)
def test_platformdirs_conftest_declaration_matches_stock(tmp_path: Path, declaration: str) -> None:
    for engine in ("stock", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", f"pytest_plugins = {declaration}\n")
        project.write(
            "test_paths.py",
            "import platformdirs.pytest_plugin as plugin\n"
            "def test_paths(platformdirs_isolated, tmp_path, request):\n"
            "    manager = request.config.pluginmanager\n"
            "    assert manager.getplugin('platformdirs.pytest_plugin') is plugin\n"
            "    assert platformdirs_isolated == tmp_path\n",
        )
        completed = (
            project.run_python("-m", "pytest", "-q")
            if engine == "stock"
            else project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "1 passed" in completed.stdout


@pytest.mark.parametrize(
    ("plugin_name", "declaration"),
    [
        ("platformdirs", False),
        ("platformdirs.pytest_plugin", False),
        ("platformdirs.pytest_plugin", True),
    ],
    ids=["entrypoint", "explicit-module", "conftest-module"],
)
def test_platformdirs_shadow_plugin_is_rejected_before_collection(
    tmp_path: Path, plugin_name: str, *, declaration: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("platformdirs/__init__.py", "")
    project.write("platformdirs/pytest_plugin.py", "")
    project.write("platformdirs-4.12.0.dist-info/METADATA", "Name: platformdirs\nVersion: 4.12.0\n")
    project.write(
        "platformdirs-4.12.0.dist-info/entry_points.txt",
        "[pytest11]\nplatformdirs = platformdirs.pytest_plugin\n",
    )
    project.write(
        "test_body.py",
        "from pathlib import Path\nPath('collected').touch()\ndef test_body():\n    pass\n",
    )
    if declaration:
        project.write("conftest.py", f"pytest_plugins = [{plugin_name!r}]\n")
    options = [] if declaration else ["-p", plugin_name]
    completed = project.run_vfy("test", "--engine=pytest-native", "--no-cov", *options, "-q")
    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert "requires the installed upstream Platformdirs plugin from 4.12.0-4.12.3" in (
        completed.stdout + completed.stderr
    )
    assert not (project.root / "collected").exists()


@pytest.mark.parametrize(
    ("installed_version", "plugin_reviewed"),
    [("4.12.0", True), ("4.12.3", True), ("4.12.0", False)],
    ids=["reviewed", "later-release-same-plugin", "changed-plugin"],
)
def test_platformdirs_editable_install_identity_is_checked_before_collection(
    tmp_path: Path, installed_version: str, *, plugin_reviewed: bool
) -> None:
    project = VfyProject(tmp_path)
    source = project.root / "src/platformdirs"
    shutil.copytree(
        Path(platformdirs.__file__).parent, source, ignore=shutil.ignore_patterns("__pycache__")
    )
    if not plugin_reviewed:
        with (source / "pytest_plugin.py").open("a") as plugin:
            plugin.write("# changed after review\n")
    environment = project.root / "environment"
    venv.EnvBuilder(with_pip=False).create(environment)
    site = (
        environment / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    metadata = site / f"platformdirs-{installed_version}.dist-info"
    metadata.mkdir()
    installed = distribution("platformdirs")
    upstream_metadata = installed.read_text("METADATA")
    upstream_entrypoints = installed.read_text("entry_points.txt")
    assert upstream_metadata is not None
    assert upstream_entrypoints is not None
    (metadata / "METADATA").write_text(
        upstream_metadata.replace("Version: 4.12.0", f"Version: {installed_version}")
    )
    (metadata / "entry_points.txt").write_text(upstream_entrypoints)
    (metadata / "direct_url.json").write_text(
        json.dumps({"url": project.root.as_uri(), "dir_info": {"editable": True}})
    )
    # Reuse the installed test dependencies without installing into either
    # environment. Only the private venv's metadata authorizes editable source.
    # An editable vfy checkout is outside site-packages, so add its import root.
    (site / "dependencies.pth").write_text(
        f"{project.root / 'src'}\n{Path(pytest.__file__).parents[1]}\n"
        f"{Path(vfy.__file__).parents[1]}\n"
    )
    project.write(
        "run_editable.py",
        "import subprocess, sys\n"
        f"sys.exit(subprocess.call([{str(environment / 'bin/python')!r}, *sys.argv[1:]]))\n",
    )
    project.write(
        "test_paths.py",
        "from pathlib import Path\nPath('collected').touch()\n"
        "import platformdirs.pytest_plugin as plugin\n"
        "def test_paths(platformdirs_isolated, tmp_path, request):\n"
        f"    assert str(Path(plugin.__file__).resolve()) == {str(source / 'pytest_plugin.py')!r}\n"
        "    assert request.config.pluginmanager.getplugin('platformdirs') is plugin\n"
        "    assert platformdirs_isolated == tmp_path\n",
    )
    completed = project.run_python(
        "run_editable.py",
        "-m",
        "vfy",
        "test",
        "--engine=pytest-native",
        "--no-cov",
        f"--basetemp={project.root / 'base'}",
        "-q",
        "test_paths.py",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None},
    )
    if plugin_reviewed:
        completed.expect(0)
        assert "1 passed" in completed.stdout
    else:
        assert completed.returncode != 0, completed.stdout + completed.stderr
        assert "requires the Platformdirs pytest plugin from 4.12.0-4.12.3" in (
            completed.stdout + completed.stderr
        )
        assert "-p no:platformdirs" in completed.stdout + completed.stderr
        assert not (project.root / "collected").exists()


@pytest.mark.skipif(find_spec("xdist") is None, reason="xdist is optional")
def test_platformdirs_fixture_isolation_and_cleanup_match_stock_workers(tmp_path: Path) -> None:
    assert version("platformdirs") == "4.12.0"
    records = {}
    for route in ("stock", "exec", "fork"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            """
import json, os
from pathlib import Path
import platformdirs
import pytest

import vfy

@pytest.fixture
def cleanup_probe(request):
    saved = dict(vars(platformdirs.PlatformDirs))
    baseline = platformdirs.user_data_dir('sentinel')
    yield
    assert dict(vars(platformdirs.PlatformDirs)) == saved
    assert platformdirs.user_data_dir('sentinel') == baseline
    case = request.node.callspec.params['case']
    Path(f'cleanup.{case}.json').write_text(json.dumps({'pid': os.getpid()}))
    print(f'cleanup {case}')

@pytest.fixture
def platformdirs_isolated(cleanup_probe, platformdirs_isolated):
    return platformdirs_isolated
""",
        )
        project.write(
            "test_paths.py",
            """
import json, os
from pathlib import Path
import platformdirs
import platformdirs.pytest_plugin as plugin
import pytest

import vfy

collection_pid = os.getpid()

@pytest.mark.parametrize('case', range(8))
def test_isolated(case, platformdirs_isolated, tmp_path, request):
    assert request.config.pluginmanager.getplugin('platformdirs.pytest_plugin') is plugin
    assert platformdirs_isolated == tmp_path
    dirs = platformdirs.PlatformDirs('sentinel', version='1')
    data = dirs.user_data_path
    config = dirs.site_config_path
    assert data == tmp_path / 'user_data' / 'sentinel' / '1'
    assert config == tmp_path / 'site_config' / 'sentinel' / '1'
    assert platformdirs.user_data_path('sentinel', version='1') == data
    data.mkdir(parents=True)
    (data / 'owned.txt').write_text(str(case))
    Path(f'case.{case}.json').write_text(json.dumps({
        'pid': os.getpid(), 'collection_pid': collection_pid, 'root': str(tmp_path),
    }))
    print(f'isolated {case}')
    if case == 7:
        pytest.fail('platformdirs failure sentinel', pytrace=False)
""",
        )
        arguments = (
            "-p",
            "platformdirs.pytest_plugin",
            "--capture=sys",
            "--tb=auto",
            "--basetemp",
            str(project.root / "base"),
            "--junitxml=results.xml",
            "-o",
            "junit_logging=all",
            "-o",
            "junit_log_passing_tests=true",
            "-q",
        )
        if route == "stock":
            completed = project.run_python(
                "-m", "pytest", "-p", "xdist.plugin", "-n", "2", "--dist=load", *arguments
            )
        else:
            completed = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--history=history.json",
                "--no-replay",
                "--no-cov",
                *arguments,
                env={
                    "VFY_WORKER_START": route,
                    "VFY_NATIVE_SCHEDULING": "cases",
                    "VFY_WORKER_COLLECTION": "0",
                },
            )
        completed.expect(1)
        assert "platformdirs failure sentinel" in completed.stdout + completed.stderr
        cases = [json.loads((project.root / f"case.{case}.json").read_text()) for case in range(8)]
        cleanup = [
            json.loads((project.root / f"cleanup.{case}.json").read_text()) for case in range(8)
        ]
        assert len({case["pid"] for case in cases}) == 2
        assert [case["pid"] for case in cases] == [case["pid"] for case in cleanup]
        assert len({case["root"] for case in cases}) == 8
        assert all(
            Path(case["root"], "user_data", "sentinel", "1", "owned.txt").read_text() == str(index)
            for index, case in enumerate(cases)
        )
        assert all((case["pid"] != case["collection_pid"]) == (route == "fork") for case in cases)
        records[route] = junit_records(
            project.root / "results.xml", project.root, project.root / "base"
        )
        assert len(records[route]) == 8
    assert records["exec"] == records["stock"]
    assert records["fork"] == records["stock"]


@pytest.mark.parametrize("hook_name", ["pytest_runtest_call", "pytest_addoption"])
def test_platformdirs_plugin_still_rejects_unreviewed_hooks(tmp_path: Path, hook_name: str) -> None:
    assert version("platformdirs") == "4.12.0"
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"""
import platformdirs.pytest_plugin as plugin
import pytest

import vfy

def unreviewed_call(item):
    raise AssertionError('unreviewed plugin hook ran')

@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    config.pluginmanager.unregister(plugin)
    plugin.{hook_name} = unreviewed_call
    config.pluginmanager.register(plugin, 'platformdirs.pytest_plugin')
""",
    )
    project.write(
        "test_body.py",
        "from pathlib import Path\nPath('collected').touch()\n"
        "def test_body():\n    Path('body-ran').touch()\n",
    )
    completed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-cov",
        "-p",
        "platformdirs.pytest_plugin",
        "-q",
    )
    assert completed.returncode != 0, completed.stdout + completed.stderr
    diagnostic = completed.stdout + completed.stderr
    assert "platformdirs.pytest_plugin" in diagnostic
    assert hook_name in diagnostic
    assert "native pytest does not execute hooks from plugin" in diagnostic
    assert not (project.root / "body-ran").exists()
    assert not (project.root / "collected").exists()
