"""Stage benchmark plug-ins without changing a specimen's import namespace."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

OUTCOME_PLUGIN = "vfy_benchmark_plugins.outcome_plugin"
NATIVE_REFERENCE_PLUGIN = "vfy_benchmark_plugins.native_reference_coverage"
RICH_CLOCK_PLUGIN = "vfy_benchmark_plugins.rich_clock_plugin"
BENCHMARK_PLUGIN = "vfy_benchmark_plugins.benchmark_plugins"
PLUGIN_RECEIPT = "vfy-benchmark-plugins.json"
_MODULES = (
    "outcome_plugin.py",
    "native_reference_coverage.py",
    "rich_clock_plugin.py",
    "benchmark_plugins.py",
    "datasette_diagnostic_plugin.py",
)


def _retain_exact(path: Path, content: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValueError(f"staged benchmark plug-in changed: {path}") from None


def stage_plugins(destination: Path) -> Path:
    """Copy canonical plug-in bytes once and verify every later use and receipt."""
    destination = destination.resolve()
    if (destination / "__init__.py").exists():
        raise ValueError(
            f"benchmark plug-in import root must not contain __init__.py: {destination}"
        )
    package = destination / "vfy_benchmark_plugins"
    source_root = Path(__file__).resolve().parent
    contents = {name: (source_root / name).read_bytes() for name in _MODULES}
    contents["__init__.py"] = b""
    files = {
        name: {
            "source": str(source_root / name) if name in _MODULES else None,
            "source_sha256": hashlib.sha256(content).hexdigest(),
            "derived": str(package / name),
            "derived_sha256": hashlib.sha256(content).hexdigest(),
        }
        for name, content in contents.items()
    }
    receipt = (
        json.dumps({"schema": 1, "package": "vfy_benchmark_plugins", "files": files}, indent=2)
        + "\n"
    ).encode()
    # Validate existing artifacts before creating any missing files.
    for path, content in [
        *((package / name, content) for name, content in contents.items()),
        (destination / PLUGIN_RECEIPT, receipt),
    ]:
        if path.exists() and path.read_bytes() != content:
            raise ValueError(f"staged benchmark plug-in changed: {path}")
    package.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        _retain_exact(package / name, content)
    _retain_exact(destination / PLUGIN_RECEIPT, receipt)
    return destination
