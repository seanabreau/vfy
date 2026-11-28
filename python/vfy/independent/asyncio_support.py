"""Explicit asyncio declarations for the independent Rust frontend."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Literal, cast

import pytest

from vfy import native
from vfy.asyncio_runtime import _policy, _runner_lifetime

if TYPE_CHECKING:
    import asyncio

    from vfy._pytest_native import NativeIndependentRequest

    from .protocol import Config, Item, Parser, PluginManager


class _AsyncioHooks:
    @pytest.hookspec(firstresult=True)
    def pytest_asyncio_loop_factories(self, config: Config, item: Item) -> None:
        """Declare the pytest-asyncio hook so its use gets a diagnostic from the engine."""


def pytest_addhooks(pluginmanager: PluginManager) -> None:
    pluginmanager.add_hookspecs(_AsyncioHooks)


def pytest_collection_modifyitems(config: Config) -> None:
    """Reject the loop-factory hook; the marker argument is rejected during collection."""
    if config.hook.pytest_asyncio_loop_factories.get_hookimpls():
        raise native._pytest.IndependentUsageError(
            "pytest_asyncio_loop_factories is not supported by the independent engine; "
            "use --engine=pytest-native"
        )


def pytest_addoption(parser: Parser) -> None:
    group = parser.getgroup("asyncio")
    group.addoption("--asyncio-mode", default=None, help="async test discovery: strict or auto")
    group.addoption("--asyncio-debug", action="store_true", default=None)
    parser.addini("asyncio_mode", "async test discovery: strict or auto", default="strict")
    parser.addini("asyncio_debug", "debug asyncio event loops", type="bool", default=False)
    parser.addini("asyncio_default_fixture_loop_scope", "async fixture loop lifetime", default=None)
    parser.addini("asyncio_default_test_loop_scope", "async test loop lifetime", default="function")


def pytest_configure(config: Config) -> None:
    fixture_scope = cast("str | None", config.getini("asyncio_default_fixture_loop_scope") or None)
    if fixture_scope == "fixture-scope":
        manager = config.pluginmanager
        adapter = manager.get_plugin("asyncio")
        if any(
            plugin is adapter and distribution.version.startswith("1.4.")
            for plugin, distribution in manager._plugin_distinfo
        ):
            raise native._pytest.IndependentUsageError(
                "'fixture-scope' is not a valid asyncio_default_fixture_loop_scope. "
                "Valid scopes are: function, class, module, package, session."
            )
    try:
        native.validate_asyncio_config(
            "strict",
            fixture_scope,
            cast("str", config.getini("asyncio_default_test_loop_scope")),
        )
    except ValueError as error:
        raise native._pytest.IndependentUsageError(str(error)) from error
    config.addinivalue_line("markers", "asyncio(loop_scope): run a coroutine in a scoped loop")


def pytest_report_header(config: Config) -> str:
    mode = cast("str", config.getoption("asyncio_mode") or config.getini("asyncio_mode"))
    try:
        native.validate_asyncio_config(mode, None, "function")
    except ValueError as error:
        raise native._pytest.IndependentUsageError(str(error)) from error
    return f"asyncio: mode={mode}"


@pytest.fixture(scope="session")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    """Return the active policy, with the same override point as the pytest adapter."""
    return _policy()


def _runner_fixture(scope: Literal["function", "class", "module", "package", "session"]) -> object:
    def acquire(
        request: NativeIndependentRequest,
        event_loop_policy: asyncio.AbstractEventLoopPolicy,
    ) -> Iterator[asyncio.Runner]:
        config = cast("Config", request.config)
        option = config.getoption("asyncio_debug")
        debug = cast("bool", config.getini("asyncio_debug") if option is None else option)
        with _runner_lifetime(event_loop_policy, None, debug) as runner:
            yield runner

    return pytest.fixture(scope=scope, name=f"_{scope}_scoped_runner")(acquire)


_function_scoped_runner = _runner_fixture("function")
_class_scoped_runner = _runner_fixture("class")
_module_scoped_runner = _runner_fixture("module")
_package_scoped_runner = _runner_fixture("package")
_session_scoped_runner = _runner_fixture("session")
