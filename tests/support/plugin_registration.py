"""Documented registration methods that a Coverage.py plug-in initializer receives."""

from __future__ import annotations

from typing import Protocol

from vfy.plugin import CoveragePlugin


class PluginRegistration(Protocol):
    """The object passed to each ``Coverage(plugins=[...])`` initializer."""

    def add_file_tracer(self, plugin: CoveragePlugin) -> None: ...
    def add_configurer(self, plugin: CoveragePlugin) -> None: ...
    def add_dynamic_context(self, plugin: CoveragePlugin) -> None: ...
    def add_noop(self, plugin: CoveragePlugin) -> None: ...
