"""Connect pytest collection and fixture lifetimes to vfy's asyncio adapter.

Decorators remain importable without loading asyncio or the Rust engine.
Activate integration with vfy test or pytest -p vfy.asyncio_plugin.
"""

from __future__ import annotations

import contextvars
import functools
import importlib
import inspect
import socket
import warnings
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from types import FunctionType, MethodType
from typing import TYPE_CHECKING, Any, Literal, cast, override

import pluggy
import pytest
from _pytest.fixtures import SubRequest, resolve_fixture_function
from _pytest.python import FunctionDefinition

from .asyncio_adapter import run_async_fixture, run_async_test
from .asyncio_facade import (
    _ASYNC_TEST_ADAPTER,
    async_fixture as async_fixture,
    fixture as fixture,
    is_async_test as is_async_test,
)
from .asyncio_runtime import (
    _policy,
    _runner_lifetime,
    _sync_metadata,
    _wrap_sync_fixture,
    _wrap_syncgen_fixture,
)

if TYPE_CHECKING:
    import asyncio

_ScopeName = Literal["function", "class", "module", "package", "session"]
_CallableKind = Literal["coroutine", "asyncgen", "sync"]
_CollectedNodes = pytest.Item | pytest.Collector | Sequence[pytest.Item | pytest.Collector] | None
LoopFactory = Callable[[], "asyncio.AbstractEventLoop"]
_SCOPES: tuple[_ScopeName, ...] = ("function", "class", "module", "package", "session")
_SCOPE_PLANS = pytest.StashKey[dict[tuple[str | None, str | None, str], _ScopeName]]()
_NATIVE_PLAIN_SYNC_FUNCTION = pytest.StashKey[bool]()
_BUILTIN_ASYNCIO = pytest.StashKey[bool]()
_UPSTREAM_NAMES = frozenset(("asyncio", "pytest_asyncio", "pytest_asyncio.plugin"))


@dataclass(frozen=True)
class _Settings:
    mode: str
    debug: bool
    fixture_scope: str | None
    test_scope: str

    @classmethod
    def read(cls, config: pytest.Config) -> _Settings:
        mode = config.getoption("asyncio_mode")
        if mode is None:
            mode = config.getini("asyncio_mode")
        debug = config.getoption("asyncio_debug")
        if debug is None:
            debug = config.getini("asyncio_debug")
        return cls(
            mode,
            debug is True or debug == "true",
            config.getini("asyncio_default_fixture_loop_scope") or None,
            config.getini("asyncio_default_test_loop_scope"),
        )


class VfyAsyncioSpecs:
    @pluggy.HookspecMarker("pytest")(firstresult=True)
    def pytest_asyncio_loop_factories(
        self, config: pytest.Config, item: pytest.Item
    ) -> Mapping[str, LoopFactory] | None:
        """Return named event-loop constructors available to a collected test."""


def _asyncio_plugin_conflict(name: str) -> str:
    return (
        f"vfy cannot share asyncio integration with {name!r}; "
        "remove the explicit pytest-asyncio plug-in load and "
        "remove pytest-asyncio from the project's test dependencies"
    )


@pytest.hookimpl(tryfirst=True)
def pytest_plugin_registered(
    plugin: object, plugin_name: str, manager: pytest.PytestPluginManager
) -> None:
    for name in (plugin_name, getattr(plugin, "__name__", "")):
        if name and name.removeprefix("pytest-") in _UPSTREAM_NAMES:
            raise pytest.UsageError(_asyncio_plugin_conflict(name))


@pytest.hookimpl(trylast=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    """Install options after conftest loading so conflicts have a useful diagnostic."""
    manager = early_config.pluginmanager
    for name in _UPSTREAM_NAMES:
        if manager.hasplugin(name):
            raise pytest.UsageError(_asyncio_plugin_conflict(name))
    factories = getattr(manager.hook, "pytest_asyncio_loop_factories", None)
    if factories is None or not factories.has_spec():
        manager.add_hookspecs(VfyAsyncioSpecs)
    # pytest exposes its parser only privately at this bootstrap stage.
    parser = early_config._parser
    group = parser.getgroup("asyncio")
    group.addoption("--asyncio-mode", default=None, help="async test discovery: strict or auto")
    group.addoption(
        "--asyncio-debug", action="store_true", default=None, help="debug asyncio event loops"
    )
    for name, help_text, default in (
        ("asyncio_mode", "async test discovery: strict or auto", "strict"),
        ("asyncio_default_fixture_loop_scope", "loop lifetime for async fixtures", None),
        ("asyncio_default_test_loop_scope", "loop lifetime for async tests", "function"),
    ):
        parser.addini(name, help_text, type="string", default=default)
    parser.addini("asyncio_debug", "debug asyncio event loops", type="bool", default=False)


def pytest_configure(config: pytest.Config) -> None:
    settings = _Settings.read(config)
    from .native import validate_asyncio_config  # noqa: PLC0415 - decorators remain extension-free

    try:
        validate_asyncio_config(settings.mode, settings.fixture_scope, settings.test_scope)
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    config.addinivalue_line("markers", "asyncio(loop_scope, loop_factories): run with vfy asyncio")
    if settings.fixture_scope is None and not config.stash.get(_BUILTIN_ASYNCIO, False):
        warnings.warn(
            pytest.PytestDeprecationWarning(
                "Set asyncio_default_fixture_loop_scope explicitly; an unset value uses "
                "each fixture's caching scope."
            ),
            stacklevel=2,
        )


def pytest_report_header(config: pytest.Config) -> list[str]:
    settings = _Settings.read(config)
    return [
        f"asyncio: mode={settings.mode}, debug={settings.debug}, "
        f"asyncio_default_fixture_loop_scope={settings.fixture_scope}, "
        f"asyncio_default_test_loop_scope={settings.test_scope}"
    ]


def _plan_loop_scope(
    explicit: str | None,
    configured: str | None,
    fixture_scope: str,
    option_name: str,
    config: pytest.Config | None = None,
) -> _ScopeName:
    from .native import (  # noqa: PLC0415 - scope planning loads the extension lazily
        plan_asyncio_loop_scope,
    )

    inputs = (explicit, configured, fixture_scope)
    plans = config.stash.setdefault(_SCOPE_PLANS, {}) if config is not None else {}
    if inputs not in plans:
        try:
            plans[inputs] = plan_asyncio_loop_scope(*inputs)
        except ValueError as error:
            raise pytest.UsageError(f"Invalid {option_name}: {error}") from error
    return plans[inputs]


def _marker_options(marker: pytest.Mark) -> tuple[str | None, Sequence[str] | None]:
    options = marker.kwargs
    if marker.args or options.keys() - {"scope", "loop_scope", "loop_factories"}:
        raise ValueError("asyncio markers accept loop_scope and loop_factories keyword arguments")
    if "scope" in options:
        if "loop_scope" in options:
            raise pytest.UsageError("Use only loop_scope on an asyncio marker; scope is redundant")
        warnings.warn(
            pytest.PytestDeprecationWarning("Use loop_scope instead of scope on asyncio markers"),
            stacklevel=2,
        )
    scope = options.get("loop_scope", options.get("scope"))
    if scope is not None:
        scope = _plan_loop_scope(scope, None, "function", "loop_scope")
    names = options.get("loop_factories")
    if names is not None and (
        not isinstance(names, Sequence)
        or isinstance(names, str)
        or not names
        or not all(isinstance(name, str) and name for name in names)
    ):
        raise ValueError("loop_factories must contain one or more nonempty factory names")
    return scope, cast(Sequence[str] | None, names)


def _test_scope(item: pytest.Function) -> _ScopeName:
    marker = item.get_closest_marker("asyncio")
    assert marker is not None
    # Collection validates marker options once. Live setup can replace its scope.
    explicit = marker.kwargs.get("loop_scope") or marker.kwargs.get("scope")
    return _plan_loop_scope(
        explicit, _Settings.read(item.config).test_scope, "function", "loop_scope", item.config
    )


def _callable_kind(_config: pytest.Config, obj: object) -> _CallableKind:
    """Classify live callables, including code replacements and coroutine markers."""
    function = getattr(obj, "__func__", obj)
    if inspect.iscoroutinefunction(function):
        return "coroutine"
    return "asyncgen" if inspect.isasyncgenfunction(function) else "sync"


class _AsyncTestAdapter:
    """Dispatch extension point for custom pytest items and callable descriptors."""

    @staticmethod
    def _can_handle(item: pytest.Function) -> bool:
        return False

    @staticmethod
    def target(item: pytest.Function) -> tuple[object, str]:
        return item, "obj"

    @classmethod
    def adapter_for(cls, item: pytest.Function) -> type[_AsyncTestAdapter] | None:
        adapters = cls.__subclasses__()
        ordinary = adapters == list(_DEFAULT_ADAPTERS) and all(
            vars(adapter).get("_can_handle") is descriptor and predicate.__code__ is code
            for adapter, descriptor, predicate, code in _PREDICATES
        )
        function = item.obj
        plain = type(item) in (pytest.Function, FunctionDefinition) and type(function) in (
            FunctionType,
            MethodType,
        )
        if (not ordinary or not plain) and type(item) is FunctionDefinition:
            from ._pytest.compact_collection import (  # noqa: PLC0415 - custom predicates can read the session inventory
                _require_inventory,
            )

            _require_inventory(item.session)
        for adapter in adapters:
            if adapter._can_handle(item):
                return adapter
        candidate = function.__func__ if type(function) is MethodType else function
        compact = type(candidate) is FunctionType and not (
            vars(candidate).keys() & {"hypothesis", "is_hypothesis_test"}
        )
        if ordinary and plain and compact and type(item) is FunctionDefinition:
            item.stash[_NATIVE_PLAIN_SYNC_FUNCTION] = True
        return None


class _CoroutineAdapter(_AsyncTestAdapter):
    @staticmethod
    @override
    def _can_handle(item: pytest.Function) -> bool:
        return _callable_kind(item.config, item.obj) == "coroutine"


class _GeneratorTestAdapter(_AsyncTestAdapter):
    @staticmethod
    @override
    def _can_handle(item: pytest.Function) -> bool:
        return _callable_kind(item.config, item.obj) == "asyncgen"


class _PropertyTestAdapter(_AsyncTestAdapter):
    @staticmethod
    @override
    def _can_handle(item: pytest.Function) -> bool:
        test = item.obj
        handle = getattr(test, "hypothesis", None)
        return bool(
            getattr(test, "is_hypothesis_test", False)
            and handle is not None
            and _callable_kind(item.config, handle.inner_test) != "sync"
        )

    @staticmethod
    @override
    def target(item: pytest.Function) -> tuple[object, str]:
        return item.obj.hypothesis, "inner_test"


_DEFAULT_ADAPTERS = (_CoroutineAdapter, _GeneratorTestAdapter, _PropertyTestAdapter)
_PREDICATES = tuple(
    (adapter, vars(adapter)["_can_handle"], adapter._can_handle, adapter._can_handle.__code__)
    for adapter in _DEFAULT_ADAPTERS
)


def _managed_marker(item: pytest.Function) -> pytest.Mark | None:
    marker = item.get_closest_marker("asyncio")
    if marker is None and _Settings.read(item.config).mode == "auto":
        item.add_marker("asyncio")
        marker = item.get_closest_marker("asyncio")
    return marker


def _factory_catalog(item: pytest.Function) -> dict[str, LoopFactory] | None:
    hook = item.ihook.pytest_asyncio_loop_factories
    if not hook.get_hookimpls():
        return None
    catalog = hook(config=item.config, item=item)
    if (
        not isinstance(catalog, Mapping)
        or not catalog
        or any(
            not isinstance(name, str) or not name or not callable(factory)
            for name, factory in catalog.items()
        )
    ):
        raise pytest.UsageError(
            "pytest_asyncio_loop_factories must return named callable factories"
        )
    return dict(catalog)


@pytest.hookimpl(tryfirst=True)
def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    item = metafunc.definition
    if _AsyncTestAdapter.adapter_for(item) is None:
        return
    marker = _managed_marker(item)
    if marker is None:
        return
    scope, names = _marker_options(marker)
    catalog = _factory_catalog(item)
    if catalog is None:
        if names is not None:
            raise pytest.UsageError("loop_factories requires a pytest_asyncio_loop_factories hook")
        return
    selected = tuple(catalog) if names is None else tuple(names)
    parameters = []
    for name in selected:
        if name in catalog:
            parameters.append(catalog[name])
        else:
            parameters.append(
                pytest.param(
                    None,
                    marks=pytest.mark.skip(
                        reason=(
                            f"Loop factory {name!r} is unavailable; "
                            f"available factories: {', '.join(catalog)}"
                        )
                    ),
                )
            )
    metafunc.fixturenames.append("_asyncio_loop_factory")
    ids = (
        (pytest.HIDDEN_PARAM,)
        if len(selected) == 1 and hasattr(pytest, "HIDDEN_PARAM")
        else selected
    )
    metafunc.parametrize(
        "_asyncio_loop_factory",
        parameters,
        ids=ids,
        indirect=True,
        scope=_plan_loop_scope(
            scope, _Settings.read(item.config).test_scope, "function", "loop_scope"
        ),
    )


def _prepare_item(item: pytest.Function, adapter: type[_AsyncTestAdapter]) -> None:
    item.stash[_ASYNC_TEST_ADAPTER] = adapter
    if adapter is _GeneratorTestAdapter:
        reason = f"Async generator test {item.name} cannot be awaited; it will be ignored"
        item.warn(pytest.PytestCollectionWarning(reason))
        item.add_marker(pytest.mark.xfail(reason=reason, run=False))
        return
    runner_name = f"_{_test_scope(item)}_scoped_runner"
    if runner_name not in item.fixturenames:
        # Each parameter owns its loop requirement even when pytest shares names_closure.
        item.fixturenames = [*item.fixturenames, runner_name]
    if not item.config.hook.pytest_asyncio_loop_factories.get_hookimpls():
        return
    setup = item.setup

    def setup_with_factory() -> None:
        item._request.getfixturevalue("_asyncio_loop_factory")
        setup()

    setattr(item, "setup", setup_with_factory)  # noqa: B010 - pytest permits instance setup hooks


@pytest.hookimpl(specname="pytest_pycollect_makeitem", wrapper=True)
def pytest_pycollect_makeitem_attach_asyncio_adapter(
    collector: pytest.Module | pytest.Class, name: str, obj: object
) -> Generator[None, _CollectedNodes, _CollectedNodes]:
    nodes = yield
    if nodes is not None:
        for item in nodes if isinstance(nodes, Sequence) else (nodes,):
            if isinstance(item, pytest.Function):
                adapter = _AsyncTestAdapter.adapter_for(item)
                if adapter is not None and _managed_marker(item) is not None:
                    _prepare_item(item, adapter)
    return nodes


def _live_adapter(item: pytest.Function) -> type[_AsyncTestAdapter] | None:
    adapter = _AsyncTestAdapter.adapter_for(item)
    if adapter is not item.stash.get(_ASYNC_TEST_ADAPTER, None):
        if adapter is None:
            del item.stash[_ASYNC_TEST_ADAPTER]
        else:
            item.stash[_ASYNC_TEST_ADAPTER] = adapter
    return adapter


def warn_asyncio_test(pyfuncitem: pytest.Function) -> None:
    """Shared diagnostics for direct pytest and the native test-call boundary."""
    if pyfuncitem.get_closest_marker("asyncio") is None:
        return
    if _live_adapter(pyfuncitem) is None:
        pyfuncitem.warn(
            pytest.PytestWarning(
                f"{pyfuncitem.nodeid} has an asyncio mark but its callable is synchronous; "
                "remove the mark unless setup supplies a coroutine"
            )
        )
    elif _Settings.read(pyfuncitem.config).mode == "strict":
        for name, definitions in pyfuncitem._fixtureinfo.name2fixturedefs.items():
            if (
                definitions
                and _callable_kind(pyfuncitem.config, definitions[-1].func) != "sync"
                and not getattr(definitions[-1].func, "_force_asyncio_fixture", False)
            ):
                warnings.warn(
                    pytest.PytestDeprecationWarning(
                        f"Strict asyncio test {pyfuncitem.name} requests {name}, "
                        "an ordinary async fixture; "
                        "use @vfy.async_fixture or asyncio_mode=auto"
                    ),
                    stacklevel=1,
                )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> Generator[None, object, object]:
    warn_asyncio_test(pyfuncitem)
    if pyfuncitem.get_closest_marker("asyncio") is None:
        return (yield)
    adapter = _live_adapter(pyfuncitem)
    if adapter is None:
        return (yield)
    runner = pyfuncitem._request.getfixturevalue(f"_{_test_scope(pyfuncitem)}_scoped_runner")
    target, attribute = adapter.target(pyfuncitem)
    source = getattr(target, attribute)
    context = contextvars.copy_context()

    def invoke(*args: object, **kwargs: object) -> None:
        run_async_test(runner, source, args, kwargs, context=context)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(target, attribute, _sync_metadata(invoke, source))
        return (yield)


def _warn_event_loop_policy_override(fixture_name: str, fixture_function: object) -> None:
    if (
        fixture_name == "event_loop_policy"
        and getattr(fixture_function, "__module__", None) != __name__
    ):
        warnings.warn(
            pytest.PytestDeprecationWarning(
                "The event_loop_policy fixture override is deprecated; configure event loops "
                "through pytest_asyncio_loop_factories"
            ),
            stacklevel=1,
        )


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(
    fixturedef: pytest.FixtureDef[Any], request: SubRequest
) -> Generator[None, object, object]:
    try:
        _warn_event_loop_policy_override(fixturedef.argname, fixturedef.func)
    except pytest.PytestDeprecationWarning as error:
        cache = fixturedef.cache_key(request)
        # Warning-as-error must enter pytest's version-specific fixture exception cache.
        if cast(tuple[int, int], pytest.version_tuple[:2]) < (8, 3):
            # pytest 8.0-8.2 caches the exception without a traceback tuple.
            fixturedef.cached_result = (None, cache, error)  # ty: ignore[invalid-assignment]
        else:
            fixturedef.cached_result = (None, cache, (error, error.__traceback__))
        raise
    function = fixturedef.func
    explicit = bool(getattr(function, "_force_asyncio_fixture", False))
    settings = _Settings.read(request.config)
    kind = _callable_kind(request.config, function)
    if not explicit and (settings.mode == "strict" or kind == "sync"):
        return (yield)
    scope = _plan_loop_scope(
        getattr(function, "_loop_scope", None) or None,
        settings.fixture_scope,
        fixturedef.scope,
        "loop_scope",
        request.config,
    )
    name = f"_{scope}_scoped_runner"
    runner = request.getfixturevalue(name)
    # Runner invalidation finishes its dependent fixture before closing the loop.
    request._get_active_fixturedef(name).addfinalizer(
        functools.partial(fixturedef.finish, request=request)
    )
    # Runner dependencies can replace the callable or mutate its code in place.
    function = fixturedef.func
    kind = _callable_kind(request.config, function)
    bound = resolve_fixture_function(fixturedef, request)
    if kind == "sync":
        synchronized = (
            _wrap_syncgen_fixture(bound, runner)
            if inspect.isgeneratorfunction(function)
            else _wrap_sync_fixture(bound, runner)
        )
    else:

        def setup(*args: object, **kwargs: object) -> object:
            value, cleanup = run_async_fixture(
                runner,
                cast(
                    Callable[..., AsyncIterator[object]] | Callable[..., Awaitable[object]], bound
                ),
                args,
                kwargs,
            )
            if cleanup is not None:
                request.addfinalizer(cleanup)
            return value

        synchronized = _sync_metadata(setup, bound)
    vars(synchronized).update(_force_asyncio_fixture=True, _loop_scope=scope)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fixturedef, "func", synchronized)
        return (yield)


def _preload_fork_runtime() -> None:
    """Share imported asyncio modules without creating a policy or event loop."""
    importlib.import_module("asyncio")


def _runner_fixture(scope: _ScopeName) -> object:
    def acquire(
        request: pytest.FixtureRequest,
        event_loop_policy: asyncio.AbstractEventLoopPolicy,
        _asyncio_loop_factory: LoopFactory | None,
    ) -> Iterator[asyncio.Runner]:
        with _runner_lifetime(
            event_loop_policy, _asyncio_loop_factory, _Settings.read(request.config).debug
        ) as runner:
            yield runner

    return pytest.fixture(scope=scope, name=f"_{scope}_scoped_runner")(acquire)


@pytest.fixture(scope="session")
def _asyncio_loop_factory(request: pytest.FixtureRequest) -> LoopFactory | None:
    return cast("LoopFactory | None", getattr(request, "param", None))


@pytest.fixture(scope="session", autouse=True)
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    """Policy used by scoped runners, overridable for compatibility."""
    return _policy()


class _PortAllocator:
    def __init__(self, transport: int) -> None:
        self.transport = transport
        self.issued: set[int] = set()

    def __call__(self) -> int:
        while True:
            with socket.socket(socket.AF_INET, self.transport) as connection:
                connection.bind(("127.0.0.1", 0))
                port = int(connection.getsockname()[1])
            if port not in self.issued:
                self.issued.add(port)
                return port


@pytest.fixture(scope="session")
def unused_tcp_port_factory() -> Callable[[], int]:
    """Allocate distinct available TCP port numbers within this session."""
    return _PortAllocator(socket.SOCK_STREAM)


@pytest.fixture(scope="session")
def unused_udp_port_factory() -> Callable[[], int]:
    """Allocate distinct available UDP port numbers within this session."""
    return _PortAllocator(socket.SOCK_DGRAM)


@pytest.fixture
def unused_tcp_port() -> int:
    """Allocate an available TCP port number without reserving it."""
    return _PortAllocator(socket.SOCK_STREAM)()


@pytest.fixture
def unused_udp_port() -> int:
    """Allocate an available UDP port number without reserving it."""
    return _PortAllocator(socket.SOCK_DGRAM)()


for _scope in _SCOPES:
    globals()[f"_{_scope}_scoped_runner"] = _runner_fixture(_scope)
