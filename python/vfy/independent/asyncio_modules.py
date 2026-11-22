"""Load public asyncio aliases on import and restore them with the configuration."""

from __future__ import annotations

import sys
import weakref
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType
from typing import TYPE_CHECKING, override

from vfy import asyncio_facade, native

from . import asyncio_support
from .fixtures import fixture
from .protocol import Item

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .protocol import Config

_NAMES = ("pytest_asyncio", "pytest_asyncio.plugin")
_TARGET = "1.4.0"


class AsyncioModules(MetaPathFinder, Loader):
    """Expose public aliases and collection claims."""

    def __init__(self, config: Config) -> None:
        self._config = weakref.ref(config)
        self._closed = False
        self._previous = {name: sys.modules[name] for name in _NAMES if name in sys.modules}
        self._loaded: set[str] = set()
        self._loading: set[str] = set()

        plugin = ModuleType("pytest_asyncio.plugin", asyncio_support.__doc__)
        vars(plugin).update(vars(asyncio_support))
        vars(plugin).update(
            __name__="pytest_asyncio.plugin",
            __package__="pytest_asyncio",
            __loader__=self,
            __spec__=ModuleSpec(
                "pytest_asyncio.plugin", loader=self, origin=asyncio_support.__file__
            ),
            fixture=asyncio_facade.fixture,
            is_async_test=self.is_async_test,
        )
        self.plugin = plugin
        self._installed: dict[str, ModuleType] = {"pytest_asyncio.plugin": plugin}

    def upstream_plugin(self) -> ModuleType:
        """Preserve upstream autouse policy when its compatibility alias activates first."""
        vars(self.plugin)["event_loop_policy"] = fixture(scope="session", autouse=True)(
            asyncio_support.event_loop_policy._get_wrapped_function()
        )
        return self.plugin

    def is_async_test(self, item: object) -> bool:
        """Return whether Rust claimed this item during collection."""
        return (
            not self._closed
            and isinstance(item, Item)
            and item.session._vfy_collection.is_async_test(item)
        )

    @override
    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname in _NAMES:
            package = fullname == "pytest_asyncio"
            origin = __file__ if package else asyncio_support.__file__
            return ModuleSpec(fullname, self, origin=origin, is_package=package)
        if fullname.startswith("pytest_asyncio."):
            raise native._pytest.UnsupportedFeatureError(
                f"the independent asyncio adapter does not expose {fullname!r}"
            )
        return None

    def _version(self) -> tuple[str, str]:
        """Release provider temporaries before checking alias ownership."""
        from importlib.metadata import (  # noqa: PLC0415 - public adapter import only
            PackageNotFoundError,
            distribution,
        )

        try:
            installed = distribution("pytest-asyncio")
        except PackageNotFoundError:
            if self._closed:
                raise
            version = None
        else:
            version = installed.version
        return version or _TARGET, (
            "installed-distribution" if version is not None else "declared-compatibility-target"
        )

    @override
    def create_module(self, spec: ModuleSpec) -> ModuleType:
        if spec.name == "pytest_asyncio.plugin":
            return self.plugin
        package = ModuleType("pytest_asyncio", "vfy-owned pytest-asyncio public declarations")
        vars(package).update(
            __file__=__file__,
            __loader__=self,
            __package__="pytest_asyncio",
            __path__=[],
            __spec__=spec,
            __all__=("fixture", "is_async_test"),
            fixture=asyncio_facade.fixture,
            is_async_test=self.is_async_test,
            plugin=self.plugin,
        )
        self._installed[spec.name] = package
        self._loading.add(spec.name)
        # Other importers must wait on the module lock during temporary publication.
        vars(spec)["_initializing"] = True
        sys.modules[spec.name] = package
        ready = False
        try:
            # Resolve while the package is visible to reentrant metadata callbacks.
            version, version_source = self._version()
            if self._closed or sys.modules.get(spec.name) is not package:
                raise RuntimeError("asyncio alias was released during import")
            vars(package).update(
                __version__=version,
                __vfy_version_source__=version_source,
            )
            # Replacing a callback's attributes can run their finalizers.
            if self._closed or sys.modules.get(spec.name) is not package:
                raise RuntimeError("asyncio alias was released during import")
            ready = True
        finally:
            if sys.modules.get(spec.name) is package:
                del sys.modules[spec.name]
            self._loading.remove(spec.name)
            if not ready:
                vars(spec)["_initializing"] = False
        return package

    @override
    def exec_module(self, module: ModuleType) -> None:
        if module.__name__ == "pytest_asyncio.plugin":
            self._loaded.add(module.__name__)
            return
        plugin = sys.modules.setdefault("pytest_asyncio.plugin", self.plugin)
        if plugin is self.plugin:
            self._loaded.add("pytest_asyncio.plugin")
        vars(module)["plugin"] = plugin
        self._loaded.add(module.__name__)

    def close(self) -> None:
        """Restore only aliases still owned by this configuration."""
        if self._closed:
            return
        for name in reversed(_NAMES):
            current = sys.modules.get(name)
            active = name in self._loaded or name in self._loading
            owned = active and current is self._installed.get(name)
            untouched = not active and name not in sys.modules
            if not owned and not untouched:
                continue
            if name in self._previous:
                sys.modules[name] = self._previous[name]
            elif owned:
                del sys.modules[name]
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        config = self._config()
        if config is not None:
            vars(config).pop("_vfy_asyncio_imports", None)
        self._closed = True


def install(config: Config) -> None:
    """Install the scoped alias loader and release it with the configuration."""
    handle = AsyncioModules(config)
    for name in _NAMES:
        sys.modules.pop(name, None)
    sys.meta_path.insert(0, handle)
    vars(config)["_vfy_asyncio_imports"] = handle
    try:
        config.add_cleanup(handle.close)
    except BaseException:
        handle.close()
        raise
