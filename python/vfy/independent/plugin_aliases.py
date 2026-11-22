"""Session-scoped import aliases for independent replacement plug-ins."""

from __future__ import annotations

import importlib
import sys
from importlib import _bootstrap
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .protocol import Config


class _PluginAlias(MetaPathFinder, Loader):
    def __init__(self, alias: str, target: str) -> None:
        self.alias = alias
        self.target = target
        self.closed = False
        self.module: ModuleType | None = None
        self.target_spec: ModuleSpec | None = None

    @override
    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if not self.closed and fullname == self.alias:
            return ModuleSpec(fullname, self)
        return None

    @override
    def create_module(self, spec: ModuleSpec) -> ModuleType:
        try:
            module = importlib.import_module(self.target)
        except BaseException:
            if self.module is not None and sys.modules.get(self.alias) is self.module:
                del sys.modules[self.alias]
            raise
        target_spec = module.__spec__
        if self.closed or (self.alias in sys.modules and sys.modules[self.alias] is not module):
            raise RuntimeError(f"plug-in alias {self.alias!r} was released during import")
        self.module = module
        self.target_spec = target_spec
        return module

    @override
    def exec_module(self, module: ModuleType) -> None:
        # Import machinery temporarily replaces the target's spec with the alias spec.
        module.__spec__ = self.target_spec

    def close(self) -> None:
        self.closed = True
        # Importlib may republish the alias after exec_module returns.
        lock = _bootstrap._get_module_lock(self.alias)  # ty: ignore[unresolved-attribute]
        lock.acquire()
        try:
            if self.module is not None and sys.modules.get(self.alias) is self.module:
                del sys.modules[self.alias]
            if self in sys.meta_path:
                sys.meta_path.remove(self)
        finally:
            lock.release()


def install_plugin_alias(config: Config, alias: str, target: str) -> None:
    """Expose an owned replacement on demand until configuration cleanup."""
    if alias in sys.modules:
        return
    handle = _PluginAlias(alias, target)
    sys.meta_path.insert(0, handle)
    try:
        config.add_cleanup(handle.close)
    except BaseException:
        handle.close()
        raise
