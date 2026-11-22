"""Installed distribution identities for supported third-party pytest adapters."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import site
import sys
import sysconfig
from collections.abc import MutableMapping
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, cast

from vfy.native import native_pytest_plugin_module_supported, pytest_plugin_version_supported

if TYPE_CHECKING:
    from importlib.metadata import Distribution

    from _pytest.config import Config as PytestConfig

    from vfy.independent.protocol import Config as IndependentConfig

# pytest's own pytester plug-ins, which native execution runs unchanged.
PYTESTER_PLUGIN_NAMES = frozenset({"pytester", "pytester_assertions"})
PYTEST_RANDOMLY_PLUGIN_NAMES = frozenset({"randomly", "pytest_randomly"})
PYTEST_RANDOMLY_PHASE_HOOKS = frozenset(
    {"pytest_runtest_setup", "pytest_runtest_call", "pytest_runtest_teardown"}
)
_WORKER_SEEDS_ENVIRONMENT = "VFY_NATIVE_WORKER_SEEDS"

# The PYTEST_DISABLE_PLUGIN_AUTOLOAD value of the environment vfy started in,
# as JSON (``null`` when unset), recorded when vfy disables autoloading.
_USER_AUTOLOAD_VARIABLE = "VFY_USER_PYTEST_DISABLE_PLUGIN_AUTOLOAD"


def worker_seed_options(config: PytestConfig | IndependentConfig) -> dict[str, object]:
    """Retain the configured seed values of the supported ordering plugins."""
    return {
        name: value
        for name in ("randomly_seed", "random_order_seed")
        if (value := config.getoption(name, default=None)) is not None
    }


def worker_seed_environment(
    config: PytestConfig | IndependentConfig, environment: MutableMapping[str, str]
) -> None:
    """Pass configured ordering plugin seeds to fresh vfy workers."""
    seeds = worker_seed_options(config)
    environment.pop(_WORKER_SEEDS_ENVIRONMENT, None)
    if seeds:
        environment[_WORKER_SEEDS_ENVIRONMENT] = json.dumps(seeds)


def worker_seeds() -> dict[str, object]:
    """Read the seed options provisioned by this worker's configured controller."""
    encoded = os.environ.get(_WORKER_SEEDS_ENVIRONMENT)
    return {} if encoded is None else cast("dict[str, object]", json.loads(encoded))


def disable_plugin_autoload(environment: MutableMapping[str, str]) -> None:
    """Disable pytest plug-in autoloading for a native run.

    Native runs load an explicit adapter set. The user's own setting is kept so
    the process that runs test bodies can restore it.
    """
    environment.setdefault(
        _USER_AUTOLOAD_VARIABLE, json.dumps(environment.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD"))
    )
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"


def restore_user_plugin_autoload() -> None:
    """Give test bodies the autoload setting of the environment vfy started in.

    A test that starts its own pytest process, as plug-in and tool test suites
    do, must not inherit vfy's disabled autoloading. Call this only in a process
    that runs tests and starts no further native workers.
    """
    recorded = os.environ.pop(_USER_AUTOLOAD_VARIABLE, None)
    if recorded is None:
        return
    value = json.loads(recorded)
    if value is None:
        os.environ.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    else:
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = value


def _installed_distribution_roots() -> list[str]:
    configured = {
        Path(path).resolve()
        for path in (
            sysconfig.get_path("purelib"),
            sysconfig.get_path("platlib"),
            *site.getsitepackages(),
        )
        if path is not None
    }
    if site.ENABLE_USER_SITE:
        configured.add(Path(site.getusersitepackages()).resolve())
    return list(
        dict.fromkeys(
            str(root)
            for entry in sys.path
            if entry and (root := Path(entry).resolve()) in configured
        )
    )


def installed_plugin_distribution(name: str) -> Distribution | None:
    """Find an adapter distribution in the interpreter's active installation roots.

    Project and PYTHONPATH metadata cannot establish an upstream plugin identity.
    Installation roots follow the interpreter's configured scheme and enabled sites.
    """
    # Only installed plug-in inspection needs distribution metadata.
    from importlib.metadata import Distribution  # noqa: PLC0415

    return next(iter(Distribution.discover(name=name, path=_installed_distribution_roots())), None)


def installed_pytest_entry_points() -> list[tuple[str, str, str, str]]:
    """Inspect installed pytest entry points without importing third-party code."""
    # Only installed plug-in inspection needs distribution metadata.
    from importlib.metadata import Distribution  # noqa: PLC0415

    return sorted(
        {
            (entry.name, entry.value, distribution.metadata["Name"], distribution.version)
            for distribution in Distribution.discover(path=_installed_distribution_roots())
            for entry in distribution.entry_points
            if entry.group == "pytest11"
        }
    )


# Entry points handled by native adapters, reporting, or coverage conflict policy.
_NATIVE_POLICY_ENTRY_POINTS = frozenset(
    {
        "asyncio",
        "html",
        "html_fixtures",
        "metadata",
        "platformdirs",
        "pytest_cov",
        "pytest_mock",
        "slipcover",
        "timeout",
        "xdist",
        "xdist.looponfail",
    }
)


def _module_source(name: str) -> str | None:
    """Locate a module's source file without executing its packages.

    Editable installs record no package files, so the import system locates
    them. Only the top-level lookup uses ``find_spec``, which imports nothing
    for an undotted name; submodules resolve along the parent's search path.
    """
    top, _, rest = name.partition(".")
    try:
        spec = importlib.util.find_spec(top)
        for part in rest.split(".") if rest else ():
            if spec is None or spec.submodule_search_locations is None:
                return None
            spec = importlib.machinery.PathFinder.find_spec(
                f"{spec.name}.{part}", list(spec.submodule_search_locations)
            )
    except (ImportError, ValueError):
        # A broken finder or module entry leaves the plug-in unadmitted.
        return None
    if spec is None or not spec.has_location or spec.origin is None:
        return None
    return spec.origin if spec.origin.endswith(".py") else None


def _reviewed_randomly_distribution(distribution: Distribution) -> bool:
    return (
        distribution.metadata["Name"].lower() == "pytest-randomly"
        and pytest_plugin_version_supported("randomly", distribution.version)
        and [
            (entry.name, entry.value)
            for entry in distribution.entry_points
            if entry.group == "pytest11"
        ]
        == [("randomly", "pytest_randomly")]
    )


def trusted_native_randomly_plugin(plugin: object, plugin_name: str) -> bool:
    """Recognize the reviewed upstream module before its phase hooks register."""
    if (
        plugin_name not in PYTEST_RANDOMLY_PLUGIN_NAMES
        or not isinstance(plugin, ModuleType)
        or plugin.__name__ != "pytest_randomly"
        or sys.modules.get("pytest_randomly") is not plugin
    ):
        return False
    origin = getattr(plugin, "__file__", None)
    installed = installed_plugin_distribution("pytest-randomly")
    return (
        isinstance(origin, str)
        and installed is not None
        and _reviewed_randomly_distribution(installed)
        and Path(origin).resolve()
        == Path(str(installed.locate_file("pytest_randomly/__init__.py"))).resolve()
    )


def trusted_native_seed_xdist_plugin(plugin: object) -> bool:
    """Recognize the installed ordering plugins' worker-input publishers."""
    plugin_type = type(plugin)
    if plugin_type.__name__ != "XdistHooks":
        return False
    module = sys.modules.get(plugin_type.__module__)
    if not isinstance(module, ModuleType) or vars(module).get("XdistHooks") is not plugin_type:
        return False
    if plugin_type.__module__ == "pytest_randomly":
        return trusted_native_randomly_plugin(module, "pytest_randomly")
    if plugin_type.__module__ != "random_order.xdist":
        return False
    origin = getattr(module, "__file__", None)
    installed = installed_plugin_distribution("pytest-random-order")
    return (
        isinstance(origin, str)
        and installed is not None
        and installed.version == "1.2.0"
        and Path(origin).resolve()
        == Path(str(installed.locate_file("random_order/xdist.py"))).resolve()
    )


def native_compatible_entry_points() -> frozenset[tuple[str, str]]:
    """Return installed ``(entry point, module)`` plug-ins that native execution loads.

    Rust inspects ordinary modules without importing them. Reviewed adapters
    also verify their installed distribution identities. Every admitted hook
    runs unchanged under native execution.

    Each call scans the installed distributions, so a long-lived watch process
    sees plug-ins installed or removed between runs. Callers scan once per
    operation and pass the result to the helpers below.
    """
    # Only installed plug-in inspection needs distribution metadata.
    from importlib.metadata import Distribution  # noqa: PLC0415

    admitted = set()
    for distribution in Distribution.discover(path=_installed_distribution_roots()):
        entries = [
            entry
            for entry in distribution.entry_points
            if entry.group == "pytest11"
            and entry.name not in _NATIVE_POLICY_ENTRY_POINTS
            and ":" not in entry.value
        ]
        if not entries:
            continue
        files = {str(path).replace("\\", "/"): path for path in distribution.files or ()}
        for entry in entries:
            relative = entry.value.replace(".", "/")
            recorded = files.get(f"{relative}.py") or files.get(f"{relative}/__init__.py")
            source = (
                str(distribution.locate_file(recorded))
                if recorded is not None
                else _module_source(entry.value)
            )
            if source is not None and (
                native_pytest_plugin_module_supported(source)
                or (
                    entry.name == "randomly"
                    and entry.value == "pytest_randomly"
                    and _reviewed_randomly_distribution(distribution)
                )
            ):
                admitted.add((entry.name, entry.value))
    return frozenset(admitted)


def trusted_native_compatible_plugin(plugin: object, admitted: frozenset[tuple[str, str]]) -> bool:
    """Recognize an admitted installed plug-in module after pytest loads it."""
    if isinstance(plugin, ModuleType) and plugin.__name__ == "pytest_randomly":
        return trusted_native_randomly_plugin(plugin, plugin.__name__)
    return isinstance(plugin, ModuleType) and plugin.__name__ in {module for _, module in admitted}


def native_compatible_plugin_names(admitted: frozenset[tuple[str, str]]) -> frozenset[str]:
    """Return the entry-point and module names of admitted installed plug-ins."""
    return frozenset(name for entry in admitted for name in entry)
