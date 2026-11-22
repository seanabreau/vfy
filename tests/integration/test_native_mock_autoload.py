"""Installed pytest-mock fixtures retain pytest's loading and opt-out behavior."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from importlib.util import find_spec
from pathlib import Path

import pytest
from packaging.version import Version

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native workers require Unix")


@pytest.mark.parametrize(
    "arguments",
    [
        ("--workers=1",),
        ("--workers=2",),
        pytest.param(
            ("-n", "2"),
            marks=pytest.mark.skipif(find_spec("xdist") is None, reason="xdist is optional"),
        ),
    ],
)
def test_installed_mock_autoload_matches_stock(tmp_path: Path, arguments: tuple[str, ...]) -> None:
    observations = []
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("app.py", "value = 1\n")
        for name in ("first", "second"):
            project.write(
                f"test_{name}.py",
                "import app\n"
                "def test_patch(mocker):\n"
                "    mocker.patch.object(app, 'value', 42)\n"
                "    assert app.value == 42\n"
                "def test_cleanup():\n"
                "    assert app.value == 1\n",
            )
        common = ("--junitxml=result.xml", "-q")
        environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}
        result = (
            project.run_pytest(*common, load_xdist=False, env=environment)
            if route == "pytest"
            else project.run_vfy(
                "test", "--engine=pytest-native", *arguments, "--no-cov", *common, env=environment
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "4 passed" in result.stdout
        observations.append(
            sorted(
                (case.get("classname"), case.get("name"), [node.tag for node in case])
                for case in ET.parse(project.root / "result.xml").iter("testcase")
            )
        )
    assert observations[0] == observations[1]


@pytest.mark.parametrize(
    ("arguments", "autoload", "enabled"),
    [
        (("-p", "no:pytest_mock"), None, False),
        (("-p", "no:pytest_mock.plugin"), None, True),
        pytest.param(
            ("--disable-plugin-autoload",),
            None,
            False,
            marks=pytest.mark.skipif(
                Version(pytest.__version__) < Version("8.4"),
                reason="--disable-plugin-autoload requires pytest 8.4",
            ),
        ),
        ((), "1", False),
    ],
)
def test_mock_autoload_blocking_matches_stock(
    tmp_path: Path, arguments: tuple[str, ...], autoload: str | None, enabled: bool
) -> None:
    # Entry-point discovery honors the registration name, so blocking the
    # import-module alias leaves the canonical pytest_mock plugin available.
    for route in ("stock", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_loading.py",
            "import pytest\n"
            "def test_loading(request):\n"
            "    manager = request.config.pluginmanager\n"
            f"    assert manager.hasplugin('pytest_mock') is {enabled}\n"
            "    assert not manager.hasplugin('pytest_mock.plugin')\n"
            f"    if {enabled}:\n"
            "        assert request.getfixturevalue('mocker').Mock() is not None\n"
            "    else:\n"
            "        with pytest.raises(pytest.FixtureLookupError):\n"
            "            request.getfixturevalue('mocker')\n",
        )
        environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": autoload}
        result = (
            project.run_python("-m", "pytest", *arguments, "-q", env=environment)
            if route == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=1",
                "--no-cov",
                *arguments,
                "-q",
                env=environment,
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout


@pytest.mark.parametrize("installed", ["3.12.0", "3.16.0rc1", "4.0.0", "invalid"])
@pytest.mark.parametrize("explicit", [False, True])
def test_unsupported_mock_release_fails_before_collection(
    tmp_path: Path, installed: str, explicit: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", "from pathlib import Path\nPath('collected').touch()\n")
    project.write("test_mock.py", "def test_mock(mocker):\n    assert mocker.Mock() is not None\n")
    arguments = ["test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q"]
    if explicit:
        arguments.extend(("-p", "pytest_mock"))
    project.write(
        "run_mock.py",
        "from importlib.metadata import Distribution\n"
        "from vfy.cli import main\n"
        "original = Distribution.version.fget\n"
        "def installed_version(distribution):\n"
        "    if distribution.metadata['Name'].lower() == 'pytest-mock':\n"
        f"        return {installed!r}\n"
        "    return original(distribution)\n"
        "Distribution.version = property(installed_version)\n"
        f"raise SystemExit(main({arguments!r}))\n",
    )
    result = project.run_python("run_mock.py", env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None})
    result.expect(1)
    assert "native pytest engine requires pytest-mock>=3.13,<4" in result.stderr
    assert "fixture 'mocker' not found" not in result.stdout + result.stderr
    assert not (project.root / "collected").exists()
