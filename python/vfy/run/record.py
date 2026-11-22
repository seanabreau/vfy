"""Identity shared by successful test runs and their later consumers."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from vfy._version import __version__
from vfy.dependency_fingerprint import installed_dependency_fingerprint
from vfy.native import portable_test_fingerprint, replay_fingerprint

from .engine import _resolve_pytest_configuration

if TYPE_CHECKING:
    from vfy.config_types import Config
    from vfy.native_types import PytestConfigRecord

GREEN_FINGERPRINT_KEY = "vfy_green_test_fingerprint"
GREEN_HISTORY_DIGEST_KEY = "vfy_green_test_history_digest"
GREEN_SOURCE_IDENTITY_KEY = "vfy_green_test_source_identity"
GREEN_SOURCE_ROOTS_KEY = "vfy_green_test_source_roots"
GREEN_IGNORED_PATHS_KEY = "vfy_green_test_ignored_paths"


def green_test_run_path(history_path: str) -> Path:
    """Return the successful-run manifest path beside test history."""

    return Path(history_path).resolve().parent / "test-run.json"


def native_execution_fingerprint(
    fingerprint: str,
    scheduling: str | None,
    *,
    worker_collection: bool = False,
    worker_start: str | None = None,
    startup_grace: str | None = None,
) -> str:
    """Bind a successful-run record to the requested native execution policy."""

    if scheduling is not None:
        fingerprint = replay_fingerprint([fingerprint, f"native-scheduling={scheduling}"])
    if worker_collection:
        fingerprint = replay_fingerprint([fingerprint, "native-collection=workers"])
    if worker_start not in {None, "exec"}:
        fingerprint = replay_fingerprint([fingerprint, f"native-worker-start={worker_start}"])
    if startup_grace not in {None, "0"}:
        fingerprint = replay_fingerprint([fingerprint, f"native-startup-grace-ms={startup_grace}"])
    return fingerprint


def _execution_config(config: Config) -> dict[str, object]:
    identity: dict[str, object] = {
        name: config[name] for name in ("run", "paths", "plugin") if name in config
    }
    vfy_run = config.get("vfy", {}).get("run")
    if vfy_run is not None:
        identity["vfy_run"] = vfy_run
    return identity


def execution_fingerprint(
    config: Config,
    source_roots: Sequence[str],
    pytest_args: Sequence[str],
    *,
    dependency_fingerprint: str | None = None,
    identity: Literal["local", "portable"] = "local",
    engine: Literal["pytest-native", "pytest", "native"] = "pytest-native",
    effective_pytest_args: Sequence[str] | None = None,
    pytest_configuration: PytestConfigRecord | None = None,
) -> str:
    """Identify inputs that can change the meaning of a pytest run."""

    if pytest_configuration is None:
        pytest_configuration = _resolve_pytest_configuration(list(pytest_args)).projection
    if effective_pytest_args is None:
        effective_pytest_args = pytest_configuration["effective_args"]
    environment = {
        name: os.environ.get(name, "")
        for name in (
            "PYTHONPATH",
            "PYTEST_ADDOPTS",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            "PYTEST_PLUGINS",
            "COVERAGE_PROCESS_START",
            "COVERAGE_RCFILE",
        )
    }
    dependencies = dependency_fingerprint
    if not dependencies:
        dependencies = installed_dependency_fingerprint()
    values = [
        __version__,
        sys.implementation.name,
        ".".join(map(str, sys.version_info[:3])),
        str(sys.flags.optimize),
        json.dumps(_execution_config(config), sort_keys=True, separators=(",", ":")),
        json.dumps(list(source_roots), separators=(",", ":")),
        json.dumps(list(pytest_args), separators=(",", ":")),
        json.dumps(list(effective_pytest_args), separators=(",", ":")),
        json.dumps(environment, sort_keys=True, separators=(",", ":")),
        dependencies,
        engine,
        pytest_configuration["config_file"] or "",
        pytest_configuration["rootdir"],
        pytest_configuration["config_contents_fingerprint"],
    ]
    if identity == "portable":
        import sysconfig  # noqa: PLC0415 - platform configuration is part of portable identity only

        return portable_test_fingerprint(
            [
                *values,
                os.getenv("COVERAGE_CORE", ""),
                sysconfig.get_platform(),
                str(sysconfig.get_config_var("Py_GIL_DISABLED") or 0),
            ]
        )
    return replay_fingerprint(
        [*values[:3], os.path.realpath(sys.executable), os.path.realpath(sys.prefix), *values[3:]]
    )
