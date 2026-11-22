"""Expose the Cargo-sourced distribution version without importing the collector."""

from __future__ import annotations

import os
import sys


def _installed_version() -> str | None:
    """Read Maturin's adjacent distribution metadata without importing its parser."""
    for entry in sys.path:
        directory = entry or os.getcwd()
        try:
            candidates = os.scandir(directory)
        except OSError:
            continue
        with candidates:
            for candidate in candidates:
                normalized = candidate.name.lower().replace("_", "-")
                if not normalized.startswith("vfy-") or not normalized.endswith(".dist-info"):
                    continue
                try:
                    with open(
                        os.path.join(candidate.path, "METADATA"), encoding="utf-8"
                    ) as metadata:
                        name = None
                        version = None
                        for line in metadata:
                            if line.startswith("Name: "):
                                name = line.removeprefix("Name: ").strip()
                            elif line.startswith("Version: "):
                                version = line.removeprefix("Version: ").strip()
                            if name is not None and version is not None:
                                if name.lower().replace("_", "-") == "vfy":
                                    return version
                                break
                except (OSError, UnicodeError):
                    continue
    return None


__version__ = _installed_version()
if __version__ is None:
    # A source checkout can be used before an editable install exists. The
    # extension value remains Cargo's single source of truth.
    from .native import package_version

    __version__ = package_version()
