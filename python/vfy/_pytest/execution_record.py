"""Trusted execution-record environment and result publication."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType

from _pytest.config import Config

from . import parallel
from .native_state import (
    _EXECUTION_PROBE_MODULE,
    _EXECUTION_RECORDER_NAME,
    NativeRunResult,
    _session_warnings_key,
)


def _native_record_environment() -> dict[str, str] | None:
    """Return the complete trusted native-record environment, when requested."""
    if os.environ.get("VFY_PYTEST_RECORD_MODE") != "native":
        return None
    values = {
        "session_token": os.environ.get("VFY_PYTEST_RECORD_SESSION", ""),
        "probe": os.environ.get("VFY_PYTEST_RECORD_PROBE", ""),
        "rootdir": os.environ.get("VFY_PYTEST_RECORD_ROOT", ""),
        "temporary": os.environ.get("VFY_PYTEST_RECORD_TEMP", ""),
        "prefix": os.environ.get("VFY_PYTEST_RECORD_PREFIX", ""),
        "observer": os.environ.get("VFY_PYTEST_OBSERVER_RESULT", ""),
        "raw": os.environ.get("VFY_PYTEST_RECORD_RAW", ""),
        "result": os.environ.get("PYTEST_NATIVE_RESULT", ""),
    }
    token = values["session_token"]
    if len(token) != 64 or any(character not in "0123456789abcdef" for character in token):
        return None
    paths = ("probe", "rootdir", "temporary", "observer", "raw", "result")
    if any(not values[name] or not Path(values[name]).is_absolute() for name in paths):
        return None
    if not values["prefix"] or not Path(values["prefix"]).is_absolute():
        return None
    return values


def _trusted_execution_probe(plugin: object, plugin_name: str) -> bool:
    environment = _native_record_environment()
    if (
        environment is None
        or not isinstance(plugin, ModuleType)
        or plugin_name != _EXECUTION_PROBE_MODULE
        or plugin.__name__ != _EXECUTION_PROBE_MODULE
    ):
        return False
    origin = getattr(plugin, "__file__", None)
    return (
        isinstance(origin, str) and Path(origin).resolve() == Path(environment["probe"]).resolve()
    )


def _trusted_execution_recorder(plugin: object, plugin_name: str) -> bool:
    environment = _native_record_environment()
    if environment is None or plugin_name != _EXECUTION_RECORDER_NAME:
        return False
    module_name = type(plugin).__module__
    module = sys.modules.get(module_name)
    recorder_type = getattr(module, "_ExecutionRecorder", None)
    origin = getattr(module, "__file__", None)
    if (
        module_name != _EXECUTION_PROBE_MODULE
        or module is None
        or type(plugin) is not recorder_type
        or not isinstance(origin, str)
        or Path(origin).resolve() != Path(environment["probe"]).resolve()
    ):
        return False
    return bool(
        getattr(plugin, "mode", None) == "native"
        and getattr(plugin, "session_token", None) == environment["session_token"]
        and getattr(plugin, "observer_path", None) == environment["observer"]
        and getattr(plugin, "raw_path", None) == environment["raw"]
    )


def _execution_record_identity(config: Config) -> dict[str, str] | None:
    environment = _native_record_environment()
    if environment is None:
        return None
    manager = config.pluginmanager
    observer = manager.get_plugin(_EXECUTION_RECORDER_NAME)
    if not _trusted_execution_recorder(observer, _EXECUTION_RECORDER_NAME):
        return None
    if getattr(observer, "outer_config", None) is not config:
        return None
    if Path(config.rootpath).resolve() != Path(environment["rootdir"]).resolve():
        return None
    nonce = getattr(observer, "observer_nonce", None)
    if (
        not isinstance(nonce, str)
        or len(nonce) != 64
        or any(character not in "0123456789abcdef" for character in nonce)
    ):
        return None
    return {
        "session_token": environment["session_token"],
        "observer_nonce": nonce,
        "rootdir": str(Path(config.rootpath).resolve()),
    }


def _publish_or_retain_execution_record(
    config: Config,
    result: NativeRunResult,
    identity: dict[str, str] | None,
    *,
    worker_warnings: list[tuple[str, str, int, str]] | None = None,
) -> None:
    """Publish ordinary JSON immediately; finalize corpus records after unconfigure."""
    if identity is not None:
        recorder = config.pluginmanager.get_plugin(_EXECUTION_RECORDER_NAME)
        if recorder is None:
            raise RuntimeError("trusted native execution recorder disappeared")
        if worker_warnings is None:
            result.bind_execution_record_identity(identity)
            recorder._native_execution_result(result)
            config.stash[_session_warnings_key].clear()
        else:
            recorder._native_execution_warning_records(worker_warnings)
        return
    if not parallel.is_worker():
        upstream_warnings = config.stash.get(_session_warnings_key, [])
        if upstream_warnings:
            result.prepend_warning_records(upstream_warnings)
            upstream_warnings.clear()
    destination = os.environ.get("PYTEST_NATIVE_RESULT")
    if destination and _native_record_environment() is None:
        result.write_json(destination)
