"""Registration and lookup for Coverage.py-compatible plug-ins."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import cast

from .plugin import CoveragePlugin


class Plugins:
    def __init__(self) -> None:
        self.order: list[CoveragePlugin] = []
        self.names: dict[str, CoveragePlugin] = {}
        self.file_tracers: list[CoveragePlugin] = []
        self.configurers: list[CoveragePlugin] = []
        self.context_switchers: list[CoveragePlugin] = []
        self.current_module: str | None = None

    def load_from_callables(self, initializers: Iterable[Callable[[Plugins], None]]) -> None:
        previous = self.current_module
        try:
            for initializer in initializers:
                self.current_module = initializer.__module__
                initializer(self)
        finally:
            self.current_module = previous

    def load_from_config(self, modules: Iterable[str], config: Mapping[str, object]) -> None:
        previous = self.current_module
        try:
            plugin_tables = cast("Mapping[str, object]", config.get("plugin", {}))
            for module_name in modules:
                module = importlib.import_module(module_name)
                initializer = getattr(module, "coverage_init", None)
                if initializer is None:
                    raise ImportError(
                        f"plug-in module {module_name!r} has no coverage_init function"
                    )
                options = config.get(f"plugin:{module_name}", plugin_tables.get(module_name, {}))
                self.current_module = module_name
                initializer(self, options)
        finally:
            self.current_module = previous

    def _add_plugin(self, plugin: CoveragePlugin, specialized: list[CoveragePlugin] | None) -> None:
        module = self.current_module or plugin.__class__.__module__
        name = f"{module}.{plugin.__class__.__name__}"
        plugin._coverage_plugin_name = name
        plugin._coverage_enabled = True
        self.order.append(plugin)
        self.names[name] = plugin
        if specialized is not None:
            specialized.append(plugin)

    def add_file_tracer(self, plugin: CoveragePlugin) -> None:
        self._add_plugin(plugin, self.file_tracers)

    def add_configurer(self, plugin: CoveragePlugin) -> None:
        self._add_plugin(plugin, self.configurers)

    def add_dynamic_context(self, plugin: CoveragePlugin) -> None:
        self._add_plugin(plugin, self.context_switchers)

    def add_noop(self, plugin: CoveragePlugin) -> None:
        self._add_plugin(plugin, None)

    def get(self, name: str) -> CoveragePlugin:
        return self.names[name]

    def __bool__(self) -> bool:
        return bool(self.order)

    def __iter__(self) -> Iterator[CoveragePlugin]:
        return iter(self.order)
