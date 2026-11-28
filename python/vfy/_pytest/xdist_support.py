"""Identity and lifecycle checks for the upstream xdist execution adapter."""

from __future__ import annotations

from pathlib import Path
from types import FunctionType, ModuleType
from typing import TYPE_CHECKING

from vfy.native import pytest_plugin_version_supported
from vfy.workers import (
    XDIST_OPTION_NAMES as XDIST_OPTION_NAMES,
    XDIST_PLUGIN_NAMES as XDIST_PLUGIN_NAMES,
    xdist_requested as xdist_requested,
)

from .plugin_support import installed_plugin_distribution

if TYPE_CHECKING:
    from _pytest.config import Config

XDIST_VERSION_RANGE = ">=3.6.1,<3.9"
XDIST_CONFTEST_HOOKS = frozenset(
    {
        "pytest_xdist_setupnodes",
        "pytest_xdist_newgateway",
        "pytest_xdist_rsyncstart",
        "pytest_xdist_rsyncfinish",
        "pytest_xdist_getremotemodule",
        "pytest_configure_node",
        "pytest_testnodeready",
        "pytest_testnodedown",
        "pytest_xdist_node_collection_finished",
        "pytest_xdist_make_scheduler",
        "pytest_xdist_auto_num_workers",
        "pytest_handlecrashitem",
        "pytest_runtest_logstart",
        "pytest_runtest_logreport",
        "pytest_runtest_logfinish",
        "pytest_warning_recorded",
    }
)


def xdist_supported_version() -> bool:
    """Return whether the installed xdist falls within the supported protocol range."""
    installed = installed_plugin_distribution("pytest-xdist")
    return installed is not None and pytest_plugin_version_supported("xdist", installed.version)


def trusted_xdist_plugin(plugin: object, plugin_name: str) -> bool:
    """Recognize the installed module and its controller or execnet worker."""
    # Registration guards inspect every external plugin. Establish an xdist
    # object shape before consulting its installed distribution identity.
    relative: str
    origin: str
    if isinstance(plugin, ModuleType):
        if plugin_name not in XDIST_PLUGIN_NAMES or plugin.__name__ != "xdist.plugin":
            return False
        module_origin = getattr(plugin, "__file__", None)
        if not isinstance(module_origin, str):
            return False
        relative, origin = "xdist/plugin.py", module_origin
    else:
        plugin_type = type(plugin)
        function: object
        if (
            plugin_type.__module__ == "xdist.workermanage"
            and plugin_type.__qualname__ == "WorkerController.RemoteHook"
        ):
            relative = "xdist/workermanage.py"
            function = plugin_type.__dict__.get("pytest_xdist_getremotemodule")
        elif plugin_type.__module__ == "xdist.dsession" and (
            (plugin_name == "dsession" and plugin_type.__name__ == "DSession")
            or (
                plugin_name == "terminaldistreporter"
                and plugin_type.__name__ == "TerminalDistReporter"
            )
        ):
            relative = "xdist/dsession.py"
            function = plugin_type.__init__
        elif (
            plugin_type.__name__ == "WorkerInteractor"
            and plugin_type.__module__ == "__channelexec__"
        ):
            # Execnet retains source filenames outside sys.modules.
            relative = "xdist/remote.py"
            function = plugin_type.__init__
        else:
            return False
        if not isinstance(function, FunctionType):
            return False
        origin = function.__code__.co_filename
    installed = installed_plugin_distribution("pytest-xdist")
    return (
        installed is not None
        and pytest_plugin_version_supported("xdist", installed.version)
        and Path(origin).resolve() == Path(str(installed.locate_file(relative))).resolve()
    )


def is_xdist_worker(config: Config) -> bool:
    """Return whether upstream xdist supplied this process's worker input."""
    return isinstance(getattr(config, "workerinput", None), dict)


def is_xdist_controller(config: Config) -> bool:
    """Return whether the real upstream distributed session is registered."""
    plugin = config.pluginmanager.getplugin("dsession")
    return plugin is not None and trusted_xdist_plugin(plugin, "dsession")


def is_native_xdist(config: Config) -> bool:
    """Recognize native execution with xdist, including its disabled -n0 mode."""
    manager = config.pluginmanager
    if not manager.hasplugin("vfy._pytest.native_pytest"):
        return False
    return any(
        plugin is not None and trusted_xdist_plugin(plugin, name)
        for name in XDIST_PLUGIN_NAMES
        if (plugin := manager.getplugin(name)) is not None
    )


def is_serial_xdist(config: Config) -> bool:
    """Return whether parsed xdist options retain execution in this process."""
    processes = config.getoption("numprocesses", default=None)
    return not is_xdist_worker(config) and (
        bool(processes == 0)
        or (processes is None and bool(config.getoption("dist", default="no") == "no"))
    )
