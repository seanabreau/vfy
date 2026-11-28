"""Registration-time validation of third-party plug-ins and conftest hooks."""

from __future__ import annotations

import inspect
import os
import sys
import weakref
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest
from _pytest.config import Config

from vfy.native import native_pytest_plugin_hook_supported

from . import compact_collection, parallel
from .benchmark_support import (
    PYTEST_BENCHMARK_HOOKS,
    benchmark_plugin_loaded,
    trusted_pytest_benchmark_plugin,
)
from .execution_record import _trusted_execution_probe, _trusted_execution_recorder
from .mock_support import (
    PYTEST_MOCK_PLUGIN_NAMES,
    PYTEST_MOCK_VERSION_RANGE,
    pytest_mock_supported_version as _pytest_mock_supported_version,
    trusted_pytest_mock_plugin,
)
from .native_state import (
    _NATIVE_ADAPTER_MODULES,
    _VFY_ASYNCIO_PLUGIN_MODULE,
    SUPPORTED_CONFTEST_HOOKS,
    UnsupportedExtensionError,
)
from .platformdirs_support import (
    PLATFORMDIRS_PLUGIN_MODULE,
    PLATFORMDIRS_PLUGIN_NAMES,
    PLATFORMDIRS_REVIEWED_VERSIONS,
    trusted_platformdirs_plugin,
)
from .plugin_support import (
    PYTEST_RANDOMLY_PHASE_HOOKS,
    PYTEST_RANDOMLY_PLUGIN_NAMES,
    PYTESTER_PLUGIN_NAMES,
    trusted_native_randomly_plugin,
    trusted_native_seed_xdist_plugin,
)
from .timeout_support import (
    PYTEST_TIMEOUT_PLUGIN_NAMES,
    PYTEST_TIMEOUT_VERSION_RANGE,
    pytest_timeout_supported_version as _pytest_timeout_supported_version,
    trusted_pytest_timeout_plugin,
)
from .vfy_bridge import _vfy_bridge_plugin
from .worker_completion import _NativeWorkerCompletionRecorder
from .xdist_cache import XdistCacheReporter
from .xdist_support import (
    XDIST_CONFTEST_HOOKS,
    XDIST_PLUGIN_NAMES,
    XDIST_VERSION_RANGE,
    is_native_xdist,
    is_serial_xdist,
    trusted_xdist_plugin,
    xdist_supported_version,
)

if TYPE_CHECKING:
    from _pytest.config import PytestPluginManager


def _validate_timeout_configuration(config: Config, items: Sequence[pytest.Item] = ()) -> None:
    """Reject timeout modes whose diagnostics or session stop state cannot reach the caller."""
    manager = config.pluginmanager
    plugin = next(
        (
            manager.get_plugin(name)
            for name in PYTEST_TIMEOUT_PLUGIN_NAMES
            if manager.get_plugin(name)
        ),
        None,
    )
    if plugin is None:
        return
    if parallel.is_parallel() and (
        config.getoption("session_timeout") is not None or config.getini("session_timeout")
    ):
        raise UnsupportedExtensionError(
            "native pytest requires --workers=1 with pytest-timeout session timeouts; "
            "use --workers=1 or remove --session-timeout and session_timeout"
        )
    configured = plugin.get_env_settings(config).method == "thread"
    marked = next(
        (
            item.nodeid
            for item in items
            if (marker := item.get_closest_marker("timeout")) is not None
            and (marker.kwargs.get("method") or (marker.args[1:2] or [None])[0]) == "thread"
        ),
        None,
    )
    if configured or marked is not None:
        location = "" if marked is None else f" ({marked})"
        raise UnsupportedExtensionError(
            "native pytest supports pytest-timeout's signal method, not the thread "
            f"method{location}; use method='signal' or run this workflow with pytest"
        )


def _hook_location(value: object) -> str:
    if not (inspect.isfunction(value) or inspect.ismethod(value)):
        return "an unknown location"
    try:
        path = inspect.getsourcefile(value) or inspect.getfile(value)
        line = inspect.getsourcelines(value)[1]
    except (OSError, TypeError):
        return "an unknown location"
    return f"{path}:{line}"


def _validate_protocol_execution_scope(
    hook: str, *, native_xdist: bool, origin: str, execution_started: bool = False
) -> None:
    if hook != "pytest_runtest_protocol":
        return
    if execution_started:
        raise UnsupportedExtensionError(
            "native pytest requires protocol hooks to register before execution: "
            f"{hook!r} from {origin}"
        )
    if native_xdist:
        raise UnsupportedExtensionError(
            f"native pytest-xdist does not execute {origin} pytest_runtest_protocol hooks; "
            "remove pytest-xdist options such as -n from the command line and addopts and "
            "use vfy's --workers, or run this workflow with pytest"
        )


def _validate_conftest(
    module: ModuleType,
    manager: PytestPluginManager,
    *,
    declarations_pending: bool = False,
    execution_started: bool = False,
) -> None:
    plugin_declarations = getattr(module, "pytest_plugins", ())
    declarations = (
        plugin_declarations.split(",")
        if isinstance(plugin_declarations, str)
        else plugin_declarations
    )
    supported_declarations = isinstance(declarations, (list, tuple)) and all(
        isinstance(name, str)
        and name
        in {
            *PYTEST_MOCK_PLUGIN_NAMES,
            *XDIST_PLUGIN_NAMES,
            *PYTESTER_PLUGIN_NAMES,
            PLATFORMDIRS_PLUGIN_MODULE,
        }
        for name in declarations
    )
    if plugin_declarations and not supported_declarations:
        path = getattr(module, "__file__", repr(module))
        raise UnsupportedExtensionError(
            f"native pytest does not load pytest_plugins declared by {path}; "
            "remove the declaration or run this workflow with pytest"
        )
    config = manager.getplugin("pytestconfig")
    xdist_enabled = isinstance(config, Config) and is_native_xdist(config)
    if declarations_pending and supported_declarations:
        # Pytest registers a conftest before importing its pytest_plugins. The
        # registration guard validates those imports before their hooks run.
        xdist_enabled |= any(
            name in XDIST_PLUGIN_NAMES and not manager.is_blocked(name) for name in declarations
        )
    for name, value in vars(module).items():
        # Match pluggy's hook discovery: module globals such as Werkzeug's
        # context-local proxies are callable but raise on attribute access.
        if not inspect.isroutine(value):
            continue
        try:
            options = getattr(value, "pytest_impl", None)
        except Exception:  # noqa: BLE001 - pluggy also tolerates any attribute error here
            options = None
        if not name.startswith("pytest_") and not isinstance(options, dict):
            continue
        hook_name = (options.get("specname") if isinstance(options, dict) else None) or name
        _validate_protocol_execution_scope(
            hook_name,
            native_xdist=xdist_enabled
            and not (isinstance(config, Config) and is_serial_xdist(config)),
            origin="project",
        )
        if execution_started and hook_name in {
            "pytest_runtest_protocol",
            "pytest_runtest_setup",
            "pytest_runtest_teardown",
        }:
            raise UnsupportedExtensionError(
                "native pytest requires conftest phase hooks to register before execution: "
                f"{hook_name!r}"
            )
        if (
            hook_name in {"pytest_runtest_setup", "pytest_runtest_teardown"}
            and isinstance(options, dict)
            and (options.get("wrapper") or options.get("hookwrapper"))
        ):
            raise UnsupportedExtensionError(
                f"native pytest does not execute conftest phase hook wrappers: {hook_name!r} at "
                f"{_hook_location(value)}"
            )
        # Collection uses pytest; the native lifecycle owns supported plain phase hooks.
        if hook_name in SUPPORTED_CONFTEST_HOOKS:
            continue
        if xdist_enabled and hook_name in XDIST_CONFTEST_HOOKS:
            continue
        if hook_name in PYTEST_BENCHMARK_HOOKS and benchmark_plugin_loaded(manager):
            # pytest-benchmark calls these while saving and reporting results.
            continue
        raise UnsupportedExtensionError(
            f"native pytest does not execute conftest hook {hook_name!r} at "
            f"{_hook_location(value)}; remove the hook or run this workflow with pytest"
        )


def _validated_pytest_mock_plugin(plugin: object, plugin_name: str) -> bool:
    if plugin_name not in PYTEST_MOCK_PLUGIN_NAMES:
        return False
    if isinstance(plugin, ModuleType) and trusted_pytest_mock_plugin(plugin, plugin_name):
        return True
    if not _pytest_mock_supported_version():
        raise UnsupportedExtensionError(
            f"native pytest requires pytest-mock{PYTEST_MOCK_VERSION_RANGE}; "
            "install a supported version or run this workflow with pytest"
        )
    raise UnsupportedExtensionError(
        f"native pytest requires the installed upstream pytest-mock plugin: {plugin_name!r}"
    )


def _trusted_worker_completion_recorder(plugin: object) -> bool:
    if type(plugin) is not _NativeWorkerCompletionRecorder:
        return False
    module = sys.modules.get(_NativeWorkerCompletionRecorder.__module__)
    origin = getattr(module, "__file__", None)
    expected = Path(__file__).with_name("worker_completion.py")
    return (
        parallel.is_worker()
        and os.environ.get("VFY_WORKER_COLLECTION") == "1"
        and isinstance(origin, str)
        and Path(origin).resolve() == expected.resolve()
    )


def _validated_platformdirs_plugin(
    plugin: object, plugin_name: str, manager: PytestPluginManager
) -> bool:
    if plugin_name not in PLATFORMDIRS_PLUGIN_NAMES:
        return False
    if not (isinstance(plugin, ModuleType) and trusted_platformdirs_plugin(plugin, plugin_name)):
        raise UnsupportedExtensionError(
            "native pytest requires the installed upstream Platformdirs plugin from "
            f"{PLATFORMDIRS_REVIEWED_VERSIONS}: {plugin_name!r}"
        )
    for attribute_name in dir(plugin):
        options = manager.parse_hookimpl_opts(plugin, attribute_name)
        if options is not None:
            hook_name = options.get("specname") or attribute_name
            raise UnsupportedExtensionError(
                f"native pytest does not execute hooks from plugin {plugin_name!r}: {hook_name}"
            )
    return True


def _validated_randomly_phase_hook(
    plugin: object,
    plugin_name: str,
    hook_name: str,
    options: Mapping[str, object],
    manager: PytestPluginManager,
) -> bool:
    if plugin_name not in PYTEST_RANDOMLY_PLUGIN_NAMES:
        return False
    if not trusted_native_randomly_plugin(plugin, plugin_name):
        raise UnsupportedExtensionError(
            "native pytest requires the installed upstream pytest-randomly plugin "
            "from a final 5.x release"
        )
    if hook_name not in PYTEST_RANDOMLY_PHASE_HOOKS:
        return False
    if (
        options.get("wrapper")
        or options.get("hookwrapper")
        or getattr(manager, "_native_execution_started", False)
    ):
        raise UnsupportedExtensionError(
            "native pytest requires plain pytest-randomly phase hooks registered before execution"
        )
    return True


def _validate_plugin_before_registration(
    plugin: object, plugin_name: str, manager: PytestPluginManager
) -> None:
    """Check every external hook before Pluggy can install or replay one."""
    module = plugin.__name__ if isinstance(plugin, ModuleType) else type(plugin).__module__
    if module == "_pytest.cacheprovider":
        raise UnsupportedExtensionError(
            "native pytest uses its Rust cache service; the stock cacheprovider cannot be enabled"
        )
    if module.startswith("_pytest.") or module in _NATIVE_ADAPTER_MODULES:
        return
    if any(
        (
            _trusted_worker_completion_recorder(plugin),
            _trusted_vfy_mutation_plugin(plugin, plugin_name),
        )
    ):
        return
    if isinstance(plugin, ModuleType) and _trusted_vfy_asyncio_plugin(plugin, plugin_name):
        return
    if _trusted_execution_probe(plugin, plugin_name):
        return
    if _trusted_execution_recorder(plugin, plugin_name):
        return
    if _vfy_bridge_plugin(plugin, plugin_name, manager) or (
        type(plugin) is XdistCacheReporter and plugin_name == "_vfy_native_xdist_cache_reporter"
    ):
        return
    # The native lifecycle drives pytest-timeout's protocol and call wrappers;
    # pytest runs its options, configuration, report header, and timer hooks.
    if (
        _validated_xdist_plugin(plugin, plugin_name)
        or _validated_platformdirs_plugin(plugin, plugin_name, manager)
        or _validated_pytest_timeout_plugin(plugin, plugin_name)
        or trusted_pytest_benchmark_plugin(plugin, plugin_name)
    ):
        return
    if isinstance(plugin, ModuleType) and (getattr(plugin, "__file__", "") or "").endswith(
        "conftest.py"
    ):
        _validate_conftest(
            plugin,
            manager,
            declarations_pending=True,
            execution_started=bool(getattr(manager, "_native_execution_started", False)),
        )
        return

    mock_plugin = _validated_pytest_mock_plugin(plugin, plugin_name)
    config = manager.get_plugin("pytestconfig")
    native_xdist = (
        isinstance(config, Config) and is_native_xdist(config) and not is_serial_xdist(config)
    )

    for attribute_name in dir(plugin):
        options = manager.parse_hookimpl_opts(plugin, attribute_name)
        if options is None:
            continue
        hook_name = options.get("specname") or attribute_name
        _validate_protocol_execution_scope(
            hook_name,
            native_xdist=native_xdist,
            origin=f"third-party plugin {plugin_name!r}",
            execution_started=bool(getattr(manager, "_native_execution_started", False)),
        )
        if (
            _validated_randomly_phase_hook(plugin, plugin_name, hook_name, options, manager)
            or (mock_plugin and hook_name == "pytest_configure")
            or (
                hook_name == "pytest_configure_node"
                and isinstance(config, Config)
                and is_native_xdist(config)
                and not (options.get("wrapper") or options.get("hookwrapper"))
                and trusted_native_seed_xdist_plugin(plugin)
            )
        ):
            # Reviewed hooks run unchanged; mock installs its own config cleanup.
            continue
        if hook_name in {
            "pytest_addoption",
            "pytest_load_initial_conftests",
            "pytest_plugin_registered",
        } or native_pytest_plugin_hook_supported(hook_name):
            # Validate all hooks before Pluggy replays any pytest_plugin_registered calls.
            # pytest runs supported configuration and collection hooks unchanged.
            continue
        if hook_name == "pytest_runtestloop" and (
            options.get("wrapper") or options.get("hookwrapper")
        ):
            # Harness wrappers observe the native loop; replacing it is unsupported.
            continue
        raise UnsupportedExtensionError(
            f"native pytest does not execute hooks from plugin {plugin_name!r}: {hook_name}"
        )


def _validated_pytest_timeout_plugin(plugin: object, plugin_name: str) -> bool:
    if trusted_pytest_timeout_plugin(plugin, plugin_name):
        return True
    if plugin_name not in PYTEST_TIMEOUT_PLUGIN_NAMES:
        return False
    if not _pytest_timeout_supported_version():
        raise UnsupportedExtensionError(
            f"native pytest requires pytest-timeout{PYTEST_TIMEOUT_VERSION_RANGE}; "
            "install a supported version or pass -p no:timeout"
        )
    raise UnsupportedExtensionError(
        f"native pytest requires the installed upstream pytest-timeout plugin: {plugin_name!r}"
    )


def _validated_xdist_plugin(plugin: object, plugin_name: str) -> bool:
    if trusted_xdist_plugin(plugin, plugin_name):
        return True
    if plugin_name not in XDIST_PLUGIN_NAMES:
        return False
    if not xdist_supported_version():
        raise UnsupportedExtensionError(
            f"native pytest supports stable pytest-xdist{XDIST_VERSION_RANGE}. "
            "Install a supported version or run this workflow with --engine=pytest"
        )
    raise UnsupportedExtensionError(
        f"native pytest requires the installed upstream xdist plugin: {plugin_name!r}"
    )


def _trusted_vfy_asyncio_plugin(plugin: ModuleType, plugin_name: str) -> bool:
    origin = getattr(plugin, "__file__", None)
    expected = Path(__file__).resolve().parents[1] / "asyncio_plugin.py"
    return (
        plugin_name == _VFY_ASYNCIO_PLUGIN_MODULE
        and plugin.__name__ == _VFY_ASYNCIO_PLUGIN_MODULE
        and isinstance(origin, str)
        and Path(origin).resolve() == expected
    )


def _trusted_vfy_mutation_plugin(plugin: object, plugin_name: str) -> bool:
    if compact_collection.is_native_mutation_controller(plugin):
        return True
    if not isinstance(plugin, ModuleType) or plugin.__name__ != plugin_name:
        return False
    origin = getattr(plugin, "__file__", None)
    if not isinstance(origin, str):
        return False
    if plugin_name == "vfy.mutation":
        expected = Path(__file__).resolve().parents[1] / "mutation.py"
        return Path(origin).resolve() == expected
    if plugin_name == "vfy.mutation_parallel":
        expected = Path(__file__).resolve().parents[1] / "mutation_parallel.py"
        if Path(origin).resolve() != expected:
            return False
        if os.environ.get("VFY_MUTATION_TEST_ISOLATION") != "file":
            raise UnsupportedExtensionError(
                "native pytest loads vfy.mutation_parallel only for file-isolated mutations"
            )
        return True
    return False


def _guard_plugin_registration(manager: PytestPluginManager) -> None:
    """Prevalidate external plugins before pytest delegates registration to Pluggy."""
    if getattr(manager, "_native_registration_guard_installed", False):
        return

    original_register = type(manager).register
    manager_ref = weakref.ref(manager)

    def register(plugin: object, name: str | None = None) -> str | None:
        current_manager = manager_ref()
        if current_manager is None:
            raise RuntimeError("pytest plugin manager was released during registration")
        plugin_name = name or current_manager.get_canonical_name(plugin)
        if (
            current_manager.get_plugin(plugin_name) is not None
            or current_manager.is_blocked(plugin_name)
            or current_manager.is_registered(plugin)
        ):
            # Let Pluggy preserve its existing duplicate and blocked-name behavior.
            return original_register(current_manager, plugin, name)
        _validate_plugin_before_registration(plugin, plugin_name, current_manager)
        return original_register(current_manager, plugin, name)

    manager.__dict__["_native_registration_guard_installed"] = True
    manager.__dict__["register"] = register
