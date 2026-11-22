"""Installed plug-ins omitted by native execution remain visible to users."""

from __future__ import annotations

import json
import sys
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Unix")
def test_omitted_plugin_is_disclosed_before_native_execution(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_property.py",
        "from hypothesis import given, strategies\n"
        "@given(strategies.integers())\n"
        "def test_property(value):\n    assert isinstance(value, int)\n",
    )
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}

    diagnostic = project.run_vfy(
        "diagnostics", "--engine=pytest-native", "--json", "--collect", env=environment
    )
    diagnostic.expect(0)
    payload = json.loads(diagnostic.stdout)
    hypothesis = next(
        row for row in payload["pytest_plugin_inventory"] if row["name"] == "hypothesispytest"
    )
    assert hypothesis["distribution"] == "hypothesis"
    assert hypothesis["native_status"] == "omitted"
    assert hypothesis["differs_from_pytest"] is True
    assert payload["native_readiness"]["collection"]["status"] == "collected"

    native = project.run_vfy(
        "--quiet", "test", "--engine=pytest-native", "--all", "--no-cov", "-q", env=environment
    )
    native.expect(0)
    assert "hypothesis (hypothesispytest)" in native.stderr
    assert "native execution omits installed pytest plug-ins" in native.stderr
    assert "pytest -p no:pytest_cov -p vfy.pytest_plugin --cov" in native.stderr

    explicit = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        "--no-cov",
        "-p",
        "_hypothesis_pytestplugin",
        env=environment,
    )
    assert explicit.returncode != 0
    assert "does not support explicitly requested third-party plug-ins" in explicit.stderr


@pytest.mark.parametrize("disable_autoload", [False, True])
def test_diagnostics_distinguishes_adapter_replacement_and_explicit_omission(
    tmp_path: Path,
    disable_autoload: bool,
) -> None:
    project = VfyProject(tmp_path)
    result = project.run_vfy(
        "diagnostics",
        "--engine=pytest-native",
        "--json",
        "--",
        "-p",
        "no:timeout",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1" if disable_autoload else None},
    )
    result.expect(0)
    inventory = {row["name"]: row for row in json.loads(result.stdout)["pytest_plugin_inventory"]}
    assert inventory["timeout"]["native_status"] == "omitted"
    assert inventory["timeout"]["differs_from_pytest"] is False
    assert inventory["pytest_mock"]["native_status"] == (
        "omitted" if disable_autoload else "loaded"
    )
    if entry_points(group="pytest11", name="asyncio"):
        assert inventory["asyncio"]["native_status"] == "replaced"
        assert "vfy provides its own asyncio adapter" in inventory["asyncio"]["reason"]
    else:
        assert "asyncio" not in inventory


@pytest.mark.parametrize("plugin", ["anyio", "asyncio", "slipcover"])
def test_explicit_unsupported_plugin_remains_blocked_without_autoload(
    tmp_path: Path, plugin: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_ok.py", "def test_ok():\n    pass\n")
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    diagnostic = project.run_vfy(
        "diagnostics", "--engine=pytest-native", "--json", "--", "-p", plugin, env=environment
    )
    diagnostic.expect(0)
    payload = json.loads(diagnostic.stdout)
    entry = next((row for row in payload["pytest_plugin_inventory"] if row["name"] == plugin), None)
    installed = any(point.name == plugin for point in entry_points(group="pytest11"))
    if installed:
        assert entry is not None
        assert entry["native_status"] == "omitted"
        assert "native execution rejects this plug-in" in entry["reason"]
    else:
        assert entry is None
    assert payload["native_readiness"]["status"] == "blocked"
    run = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "--no-cov", "-p", plugin, env=environment
    )
    assert run.returncode != 0
    expected = (
        "remove the explicit pytest-asyncio plug-in load"
        if plugin == "asyncio"
        else "native pytest cannot run this workflow"
    )
    assert expected in run.stderr
