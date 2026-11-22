"""Identity checks for the supported upstream pytest-mock fixture adapter."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

from vfy.native import pytest_plugin_version_supported

from .plugin_support import installed_plugin_distribution

PYTEST_MOCK_PLUGIN_NAMES = frozenset({"pytest_mock", "pytest_mock.plugin"})
PYTEST_MOCK_VERSION_RANGE = ">=3.13,<4"


def _supported_version(version: str) -> bool:
    return pytest_plugin_version_supported("mock", version)


def pytest_mock_supported_version() -> bool:
    """Return whether the installed pytest-mock supports the native adapter."""
    installed = installed_plugin_distribution("pytest-mock")
    return installed is not None and _supported_version(installed.version)


def trusted_pytest_mock_plugin(plugin: ModuleType, plugin_name: str) -> bool:
    """Recognize the installed upstream module through either pytest load alias."""
    if plugin_name not in PYTEST_MOCK_PLUGIN_NAMES or plugin.__name__ != plugin_name:
        return False
    origin = getattr(plugin, "__file__", None)
    if not isinstance(origin, str):
        return False
    installed = installed_plugin_distribution("pytest-mock")
    if installed is None:
        return False
    relative = (
        "pytest_mock/__init__.py" if plugin_name == "pytest_mock" else "pytest_mock/plugin.py"
    )
    return (
        _supported_version(installed.version)
        and Path(origin).resolve() == Path(str(installed.locate_file(relative))).resolve()
    )
