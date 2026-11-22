"""Recognition and validation of vfy's coverage and run-result adapters."""

from __future__ import annotations

import os
from types import ModuleType
from typing import TYPE_CHECKING

from _pytest.config import Config

from . import parallel
from .native_state import _NATIVE_PLUGIN_MODULE, _VFY_COVERAGE_BRIDGE, UnsupportedExtensionError
from .xdist_support import is_xdist_worker

if TYPE_CHECKING:
    from _pytest.config import PytestPluginManager
    from _pytest.main import Session


def _registered_vfy_capability(manager: PytestPluginManager) -> bool:
    return any(
        isinstance(plugin, ModuleType)
        and plugin.__name__ == _NATIVE_PLUGIN_MODULE
        and getattr(plugin, "_VFY_COVERAGE_BRIDGE", None) == _VFY_COVERAGE_BRIDGE
        for plugin in manager.get_plugins()
    )


def _vfy_bridge_plugin(plugin: object, plugin_name: str, manager: PytestPluginManager) -> bool:
    """Recognize only vfy's explicit coverage and run-result adapters."""
    if os.getenv("VFY_NATIVE_COVERAGE_BRIDGE") != "1" or not _registered_vfy_capability(manager):
        return False
    if isinstance(plugin, ModuleType):
        return (
            plugin_name in {"vfy.pytest_plugin", "vfy.pytest_reporting"}
            and plugin.__name__ == plugin_name
        )
    module = type(plugin).__module__
    if plugin_name == "_vfy_html_reporter":
        return module == "vfy.pytest_reporting" and type(plugin).__name__ == "_VfyHtmlReporter"
    if plugin_name == "_vfy_html_extras":
        return module == "vfy.pytest_reporting" and type(plugin).__name__ == "_VfyHtmlExtras"
    expected = {
        "_cov": "VfyCovPlugin",
        "_vfy_run_observer": "_VfyRunObserver",
        "_vfy_test_selection": "_VfyTestSelection",
        "_vfy_ci_collection": "_VfyCiCollection",
    }
    return bool(
        module == "vfy.pytest_plugin" and expected.get(plugin_name) == type(plugin).__name__
    )


def _native_bridge_adapters(session: Session) -> tuple[object | None, object]:
    pluginmanager = session.config.pluginmanager
    coverage = pluginmanager.getplugin("_cov")
    observer = pluginmanager.getplugin("_vfy_run_observer")
    no_cov = bool(session.config.getoption("no_cov", default=False))
    if (coverage is None and not no_cov) or (
        coverage is not None and not _vfy_bridge_plugin(coverage, "_cov", pluginmanager)
    ):
        raise UnsupportedExtensionError(
            "native pytest vfy coverage requires vfy's enabled coverage adapter"
        )
    if observer is None:
        raise UnsupportedExtensionError("native pytest requires vfy's result adapter")
    if not _vfy_bridge_plugin(observer, "_vfy_run_observer", pluginmanager):
        raise UnsupportedExtensionError("native pytest received an unsupported vfy result adapter")
    if getattr(session.config.option, "htmlpath", None) and not is_xdist_worker(session.config):
        html_reporter = pluginmanager.getplugin("_vfy_html_reporter")
        if html_reporter is None or not _vfy_bridge_plugin(
            html_reporter, "_vfy_html_reporter", pluginmanager
        ):
            raise UnsupportedExtensionError(
                "native pytest received an unsupported vfy HTML adapter"
            )
        if session.config.getini("generate_report_on_test") and parallel.is_parallel():
            raise UnsupportedExtensionError(
                "native vfy HTML generate_report_on_test requires --workers=1"
            )
    if no_cov and not getattr(coverage, "_selection_only", False):
        coverage = None
    return coverage, observer


def _accept_native_child_results(session: Session) -> None:
    """Accept parent bridge state after validated native worker execution."""
    manager = session.config.pluginmanager
    observer = manager.getplugin("_vfy_run_observer")
    if observer is None or not _vfy_bridge_plugin(observer, "_vfy_run_observer", manager):
        raise UnsupportedExtensionError("native pytest requires vfy's result adapter")
    coverage = manager.getplugin("_cov")
    if coverage is not None:
        if not _vfy_bridge_plugin(coverage, "_cov", manager):
            raise UnsupportedExtensionError(
                "native pytest vfy coverage requires vfy's enabled coverage adapter"
            )
        coverage._native_accept()
    observer._native_accept()


def _vfy_bridge_plugin_enabled(config: Config) -> bool:
    if os.getenv("VFY_NATIVE_COVERAGE_BRIDGE") != "1" or not _registered_vfy_capability(
        config.pluginmanager
    ):
        return False
    observer = config.pluginmanager.getplugin("_vfy_run_observer")
    coverage = config.pluginmanager.getplugin("_cov")
    selection = config.pluginmanager.getplugin("_vfy_test_selection")
    return (
        observer is not None
        and _vfy_bridge_plugin(observer, "_vfy_run_observer", config.pluginmanager)
        and (getattr(observer, "_native_bridge", False) is True)
        and (
            coverage is None
            or (
                _vfy_bridge_plugin(coverage, "_cov", config.pluginmanager)
                and (getattr(coverage, "_native_bridge", False) is True)
            )
        )
        and (
            selection is None
            or _vfy_bridge_plugin(selection, "_vfy_test_selection", config.pluginmanager)
        )
    )
