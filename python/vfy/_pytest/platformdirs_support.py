"""Identity checks for the supported upstream Platformdirs fixture adapter."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from .plugin_support import installed_plugin_distribution

if TYPE_CHECKING:
    from importlib.metadata import Distribution

PLATFORMDIRS_PLUGIN_MODULE = "platformdirs.pytest_plugin"
PLATFORMDIRS_PLUGIN_NAMES = frozenset({"platformdirs", PLATFORMDIRS_PLUGIN_MODULE})
PLATFORMDIRS_REVIEWED_VERSIONS = "4.12.0-4.12.3"
# Platformdirs releases its fixture module unchanged across patch releases, so
# the reviewed bytes, not the distribution version, identify the adapter.
_REVIEWED_PLUGIN_SHA256 = "909672c61917b3284cc0c4369a3a8d284049782c3fb26ecbe058bb2a42c68e2c"


def platformdirs_plugin_reviewed() -> bool:
    """Return whether the installed Platformdirs ships the reviewed fixture module."""
    installed = installed_plugin_distribution("platformdirs")
    if installed is None:
        return False
    origin = _installed_plugin_path(installed)
    return origin is not None and _reviewed_bytes(origin)


def platformdirs_entrypoint_installed() -> bool:
    """Recognize the upstream fixture entry point in installation metadata."""
    installed = installed_plugin_distribution("platformdirs")
    return installed is not None and any(
        entry.group == "pytest11"
        and entry.name == "platformdirs"
        and entry.value == "platformdirs.pytest_plugin"
        for entry in installed.entry_points
    )


def trusted_platformdirs_plugin(plugin: ModuleType, plugin_name: str) -> bool:
    """Recognize the installed upstream fixture module through either load alias."""
    if (
        plugin_name not in PLATFORMDIRS_PLUGIN_NAMES
        or plugin.__name__ != PLATFORMDIRS_PLUGIN_MODULE
    ):
        return False
    origin = getattr(plugin, "__file__", None)
    installed = installed_plugin_distribution("platformdirs")
    if not isinstance(origin, str) or installed is None:
        return False
    expected = _installed_plugin_path(installed)
    return expected is not None and Path(origin).resolve() == expected and _reviewed_bytes(expected)


def _reviewed_bytes(origin: Path) -> bool:
    import hashlib  # noqa: PLC0415 - only needed when validating a plug-in

    try:
        content = origin.read_bytes()
    except OSError:
        return False
    return hashlib.sha256(content).hexdigest() == _REVIEWED_PLUGIN_SHA256


def _installed_plugin_path(installed: Distribution) -> Path | None:
    packaged = Path(str(installed.locate_file("platformdirs/pytest_plugin.py"))).resolve()
    if packaged.is_file():
        return packaged
    # Editable metadata in a trusted installation root identifies the upstream
    # src-layout checkout; project-local dist-info cannot authorize this path.
    metadata = installed.read_text("direct_url.json")
    if metadata is None:
        return None
    try:
        direct_url = json.loads(metadata)
    except json.JSONDecodeError:
        return None
    if not isinstance(direct_url, dict):
        return None
    directory = direct_url.get("dir_info")
    url = direct_url.get("url")
    if not isinstance(directory, dict) or directory.get("editable") is not True:
        return None
    if not isinstance(url, str):
        return None
    from urllib.parse import (  # noqa: PLC0415 - only editable metadata needs URL parsing
        unquote,
        urlsplit,
    )

    parsed = urlsplit(url)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    root = Path(unquote(parsed.path))
    if not root.is_absolute():
        return None
    return (root / "src/platformdirs/pytest_plugin.py").resolve()
