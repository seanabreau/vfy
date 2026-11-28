"""Verify the origin and contents of the Platformdirs fixture module."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from .plugin_support import installed_plugin_distribution

if TYPE_CHECKING:
    from importlib.metadata import Distribution

PLATFORMDIRS_PLUGIN_MODULE = "platformdirs.pytest_plugin"
PLATFORMDIRS_PLUGIN_NAMES = frozenset({"platformdirs", PLATFORMDIRS_PLUGIN_MODULE})
_PLUGIN_PATH = "platformdirs/pytest_plugin.py"
# Use the reviewed bytes when installation metadata supplies no fixture digest,
# as with an editable installation.
_REVIEWED_PLUGIN_SHA256 = "909672c61917b3284cc0c4369a3a8d284049782c3fb26ecbe058bb2a42c68e2c"


def platformdirs_plugin_verified() -> bool:
    """Return whether the installed Platformdirs fixture file can be verified."""
    installed = installed_plugin_distribution("platformdirs")
    return installed is not None and _verified_plugin_path(installed) is not None


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
        or sys.modules.get(PLATFORMDIRS_PLUGIN_MODULE) is not plugin
    ):
        return False
    origin = getattr(plugin, "__file__", None)
    spec_origin = getattr(getattr(plugin, "__spec__", None), "origin", None)
    installed = installed_plugin_distribution("platformdirs")
    if not isinstance(origin, str) or not isinstance(spec_origin, str) or installed is None:
        return False
    expected = _verified_plugin_path(installed)
    return (
        expected is not None and Path(origin).resolve() == Path(spec_origin).resolve() == expected
    )


def _plugin_digest(origin: Path) -> bytes | None:
    import hashlib  # noqa: PLC0415 - only needed when validating a plug-in

    try:
        content = origin.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(content).digest()


def _verified_plugin_path(installed: Distribution) -> Path | None:
    packaged = Path(str(installed.locate_file(_PLUGIN_PATH))).resolve()
    recorded = None
    if packaged.is_file():
        origin = packaged
        recorded = next(
            (file.hash for file in installed.files or () if str(file) == _PLUGIN_PATH), None
        )
    else:
        origin = _editable_plugin_path(installed)
    if origin is None or (digest := _plugin_digest(origin)) is None:
        return None
    if recorded is None:
        return origin if digest.hex() == _REVIEWED_PLUGIN_SHA256 else None

    from base64 import urlsafe_b64encode  # noqa: PLC0415 - only wheel RECORD uses base64

    encoded = urlsafe_b64encode(digest).rstrip(b"=").decode()
    return origin if recorded.mode == "sha256" and recorded.value == encoded else None


def _editable_plugin_path(installed: Distribution) -> Path | None:
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
    return (root / "src" / _PLUGIN_PATH).resolve()
