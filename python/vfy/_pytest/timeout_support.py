"""Identity checks for the supported upstream pytest-timeout adapter."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Protocol

from vfy.native import pytest_plugin_version_supported

from .plugin_support import installed_plugin_distribution

# pytest registers the entry point as `timeout` and `-p pytest_timeout` by module name.
PYTEST_TIMEOUT_PLUGIN_NAMES = frozenset({"timeout", "pytest_timeout"})
PYTEST_TIMEOUT_VERSION_RANGE = ">=2.4,<2.5"


class TimeoutSettings(Protocol):
    """The upstream timer hook's selected method."""

    @property
    def method(self) -> str: ...


def _supported_version(version: str) -> bool:
    return pytest_plugin_version_supported("timeout", version)


def pytest_timeout_installed() -> bool:
    """Return whether pytest-timeout is installed in an active installation root."""
    return installed_plugin_distribution("pytest-timeout") is not None


def pytest_timeout_supported_version() -> bool:
    """Return whether the installed pytest-timeout supports the native adapter."""
    installed = installed_plugin_distribution("pytest-timeout")
    return installed is not None and _supported_version(installed.version)


def trusted_pytest_timeout_plugin(plugin: object, plugin_name: str) -> bool:
    """Recognize the installed upstream module through either pytest load name."""
    if (
        plugin_name not in PYTEST_TIMEOUT_PLUGIN_NAMES
        or not isinstance(plugin, ModuleType)
        or plugin.__name__ != "pytest_timeout"
    ):
        return False
    origin = getattr(plugin, "__file__", None)
    installed = installed_plugin_distribution("pytest-timeout")
    return (
        isinstance(origin, str)
        and installed is not None
        and _supported_version(installed.version)
        and Path(origin).resolve()
        == Path(str(installed.locate_file("pytest_timeout.py"))).resolve()
    )
