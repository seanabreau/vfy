"""Pinned pytest source identity and installed-interpreter checks."""

from __future__ import annotations

import subprocess
from pathlib import Path

UPSTREAM_COMMIT = "cf470ec0bf7eb89cd97dd56df4859eae5db46447"


def git_output(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def pytest_version(python: Path) -> str:
    result = subprocess.run(
        [str(python), "-c", "from importlib.metadata import version; print(version('pytest'))"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
