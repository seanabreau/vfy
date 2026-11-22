"""Bind the owned HTML provider to one independent configuration."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .protocol import Config


def install(config: Config) -> ModuleType:
    """Evaluate shared declarations against the facade and restore caller imports."""
    name = "vfy.pytest_reporting"
    attribute = "pytest_reporting"
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parents[1] / f"{attribute}.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the owned reporting provider {name!r}")
    plugin = importlib.util.module_from_spec(spec)
    package = vars(sys.modules["vfy"])
    previous_module = {name: sys.modules[name]} if name in sys.modules else {}
    previous_attribute = {attribute: package[attribute]} if attribute in package else {}

    def restore() -> None:
        if sys.modules.get(name) is plugin:
            if previous_module:
                sys.modules[name] = previous_module[name]
            else:
                sys.modules.pop(name, None)
        if package.get(attribute) is plugin:
            if previous_attribute:
                package[attribute] = previous_attribute[attribute]
            else:
                package.pop(attribute, None)

    try:
        sys.modules[name] = plugin
        package[attribute] = plugin
        config.add_cleanup(restore)
        spec.loader.exec_module(plugin)
    except BaseException:
        restore()
        raise
    return plugin
