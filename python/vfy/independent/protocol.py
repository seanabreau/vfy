"""CPython protocol objects used by the independent Rust test frontend."""

from __future__ import annotations

import argparse
import inspect
import os
import sys
import warnings
from collections.abc import (
    Callable,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from copy import deepcopy
from functools import cached_property
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, Self, cast, override

from vfy import native

from .arguments import argument_parser, process_count
from .compat import NOTSET
from .facade import (
    ExitCode,
    Interrupted,
    MarkDecorator,
    PytestRemovedIn10Warning,
    hookimpl,
    skip as skip_test,
)
from .fixtures import FuncFixtureInfo
from .stash import Stash

type IniDeclaration = tuple[str, object, tuple[str, ...]]
_BUILTIN_INI: dict[str, IniDeclaration] = {
    "verbosity_assertions": ("string", "auto", ()),
    "junit_suite_name": ("string", "pytest", ()),
    "junit_logging": ("string", "no", ()),
    "junit_log_passing_tests": ("bool", True, ()),
    "junit_duration_report": ("string", "total", ()),
    "junit_family": ("string", "xunit2", ()),
    "log_level": ("string", None, ()),
    "log_format": ("string", "%(levelname)-8s %(name)s:%(filename)s:%(lineno)d %(message)s", ()),
    "log_date_format": ("string", "%H:%M:%S", ()),
    "log_auto_indent": ("string", None, ()),
    "log_cli": ("bool", False, ()),
    "log_cli_level": ("string", None, ()),
    "log_cli_format": ("string", None, ()),
    "log_cli_date_format": ("string", None, ()),
    "markers": ("linelist", [], ()),
    "filterwarnings": ("linelist", [], ()),
    "usefixtures": ("args", [], ()),
    "testpaths": ("args", [], ()),
    "pythonpath": ("paths", [], ()),
    "addopts": ("args", [], ()),
    "required_plugins": ("args", [], ()),
    "minversion": ("string", "", ()),
    "empty_parameter_set_mark": ("string", "", ()),
    "disable_test_id_escaping_and_forfeit_all_rights_to_community_support": ("bool", False, ()),
    "python_files": ("args", ["test_*.py", "*_test.py"], ()),
    "python_classes": ("args", ["Test"], ()),
    "python_functions": ("args", ["test"], ()),
    "norecursedirs": (
        "args",
        ["*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv", "{arch}"],
        (),
    ),
    "strict": ("bool", False, ()),
    "strict_config": ("bool", None, ()),
    "strict_markers": ("bool", None, ()),
    "strict_xfail": ("bool", None, ("xfail_strict",)),
    "tmp_path_retention_count": ("string", "3", ()),
    "tmp_path_retention_policy": ("string", "all", ()),
    # Accepted settings that change only diagnostics; test selection and
    # outcomes do not depend on them.
    "faulthandler_timeout": ("string", 0.0, ()),
    "faulthandler_exit_on_timeout": ("bool", False, ()),
    "assertion_text_diff_style": ("string", "ndiff", ()),
    "truncation_limit_chars": ("string", None, ()),
    "truncation_limit_lines": ("string", None, ()),
}

# Public marker declarations for the fixed pytest 9.1.1 protocol.
_BUILTIN_MARKERS: tuple[str, ...] = (
    (
        "filterwarnings(warning): add a warning filter to the given test. see "
        "https://docs.pytest.org/en/stable/how-to/capture-warnings.html#pytest-mark-filterwarnings"
        " "
    ),
    (
        "skip(reason=None): skip the given test function with an optional reason. "
        'Example: skip(reason="no way of currently testing this") skips the test.'
    ),
    (
        "skipif(condition, ..., *, reason=...): skip the given test function if any of "
        "the conditions evaluate to True. Example: skipif(sys.platform == 'win32') "
        "skips the test if we are on the win32 platform. See "
        "https://docs.pytest.org/en/stable/reference/reference.html#pytest-mark-skipif"
    ),
    (
        "xfail(condition, ..., *, reason=..., run=True, raises=None, "
        "strict=strict_xfail): mark the test function as an expected failure if any of "
        "the conditions evaluate to True. Optionally specify a reason for better "
        "reporting and run=False if you don't even want to execute the test function. "
        "If only specific exception(s) are expected, you can list them in raises, and "
        "if the test fails in other ways, it will be reported as a true failure. See "
        "https://docs.pytest.org/en/stable/reference/reference.html#pytest-mark-xfail"
    ),
    (
        "parametrize(argnames, argvalues): call a test function multiple times passing "
        "in different arguments in turn. argvalues generally needs to be a list of "
        "values if argnames specifies only one name or a list of tuples of values if "
        "argnames specifies multiple names. Example: @parametrize('arg1', [1,2]) would "
        "lead to two calls of the decorated test function, one with arg1=1 and another "
        "with arg1=2.see https://docs.pytest.org/en/stable/how-to/parametrize.html for "
        "more info and examples."
    ),
    (
        "usefixtures(fixturename1, fixturename2, ...): mark tests as needing all of the"
        " specified fixtures. see "
        "https://docs.pytest.org/en/stable/explanation/fixtures.html#usefixtures "
    ),
    (
        "tryfirst: mark a hook implementation function such that the plugin machinery "
        "will try to call it first/as early as possible. DEPRECATED, use "
        "@pytest.hookimpl(tryfirst=True) instead."
    ),
    (
        "trylast: mark a hook implementation function such that the plugin machinery "
        "will try to call it last/as late as possible. DEPRECATED, use "
        "@pytest.hookimpl(trylast=True) instead."
    ),
)

if TYPE_CHECKING:
    import importlib.metadata

    from vfy._pytest_native import (
        NativeCollectionArena,
        NativeHookRegistry,
        NativeIndependentCallInfo,
        NativeIndependentReport,
        NativeIndependentRequest,
        NativeSessionReports,
    )
    from vfy.native import NativePytestConfiguration

    from .cache import Cache
    from .capture import PhaseLogging
    from .legacy_path import LegacyPath
    from .terminal import TerminalReporter


class HookCaller:
    """Adapt a Python keyword call to the native hook operation."""

    def __init__(
        self, registry: NativeHookRegistry, name: str, remove_plugins: Sequence[object] = ()
    ) -> None:
        self._registry = registry
        self.name = name
        self._remove_plugins = remove_plugins

    @property
    def spec(self) -> object:
        return self._registry.get_spec(self.name)

    __call__ = native._pytest.NativeHookCall()

    def call_historic(
        self,
        result_callback: Callable[[object], None] | None = None,
        kwargs: dict[str, object] | None = None,
    ) -> None:
        self._registry.call_historic(self.name, kwargs, result_callback)

    def get_hookimpls(self) -> list[object]:
        return list(self._registry.get_subset_hookimpls(self.name, self._remove_plugins))

    def call_extra(
        self, methods: Sequence[Callable[..., object]], kwargs: dict[str, object]
    ) -> object:
        return self._registry.call_extra(self.name, methods, kwargs, self._remove_plugins)

    def has_spec(self) -> bool:
        return self._registry.has_spec(self.name)

    def is_historic(self) -> bool:
        return self._registry.is_historic(self.name)


class HookRelay:
    """Hook callers for one plugin scope, created on first access and cached on the relay."""

    def __init__(self, registry: NativeHookRegistry, remove_plugins: Sequence[object] = ()) -> None:
        self._registry = registry
        self._remove_plugins = remove_plugins

    def __getattr__(self, name: str) -> HookCaller:
        if not self._registry.has_hook(name):
            raise AttributeError(name)
        caller = HookCaller(self._registry, name, self._remove_plugins)
        setattr(self, name, caller)
        return caller

    @override
    def __dir__(self) -> list[str]:
        # Callers are created lazily, so list every known hook name, not only the cached ones.
        return sorted({*super().__dir__(), *self._registry.get_hook_names()})


class _DistFacade:
    """Expose a distribution's ``project_name`` as pluggy's ``DistFacade`` does."""

    def __init__(self, dist: importlib.metadata.Distribution) -> None:
        self._dist = dist

    @property
    def project_name(self) -> str:
        return self._dist.metadata["Name"]

    @property
    def version(self) -> str:
        return self._dist.version

    def __getattr__(self, attr: str) -> object:
        return getattr(self._dist, attr)


class PluginManager:
    """Expose pytest manager signatures over the native plugin registry."""

    def __init__(self, registry: NativeHookRegistry) -> None:
        self._native = registry
        self.hook = HookRelay(registry)
        self.trace = registry.trace
        self.project_name = registry.project_name
        self._plugin_distinfo: list[tuple[object, importlib.metadata.Distribution]] = []
        self._plugin_aliases: dict[str, str] = {}

    def list_plugin_distinfo(self) -> list[tuple[object, _DistFacade]]:
        # Native plugin loading records raw importlib.metadata distributions;
        # pytest reads ``project_name`` from the pluggy facade.
        return [(plugin, _DistFacade(dist)) for plugin, dist in self._plugin_distinfo]

    def get_canonical_name(self, plugin: object) -> str:
        return self._native.get_canonical_name(plugin)

    def add_hookcall_monitoring(
        self, before: Callable[..., object], after: Callable[..., object]
    ) -> Callable[[], None]:
        return self._native.add_hookcall_monitoring(before, after)

    def enable_tracing(self) -> Callable[[], None]:
        return self._native.enable_tracing()

    def register(self, plugin: object, name: str | None = None) -> str | None:
        registered = self._native.register_compatible(plugin, name)
        if registered is not None:
            self.hook.pytest_plugin_registered.call_historic(
                kwargs={"plugin": plugin, "plugin_name": registered, "manager": self}
            )
        return registered

    def unregister(self, plugin: object | None = None, name: str | None = None) -> object | None:
        resolved = None if name is None else self._plugin_aliases.get(name, name)
        return self._native.unregister_compatible(plugin, resolved)

    def get_plugins(self) -> set[object]:
        return set(self._native.get_plugins())

    def get_plugin(self, name: str) -> object | None:
        return self._native.get_plugin(self._plugin_aliases.get(name, name))

    getplugin = get_plugin

    def hasplugin(self, name: str) -> bool:
        return self._native.has_plugin(self._plugin_aliases.get(name, name))

    has_plugin = hasplugin

    def get_name(self, plugin: object) -> str | None:
        return self._native.get_name(plugin)

    def is_registered(self, plugin: object) -> bool:
        return self._native.is_registered(plugin)

    def list_name_plugin(self) -> list[tuple[str, object | None]]:
        return self._native.list_name_plugin()

    def add_hookspecs(self, module_or_class: object) -> None:
        self._native.add_hookspecs_compatible(module_or_class)

    def check_pending(self) -> None:
        self._native.check_pending()

    def set_blocked(self, name: str) -> None:
        self._native.set_blocked(self._plugin_aliases.get(name, name))

    def is_blocked(self, name: str) -> bool:
        return self._native.is_blocked(self._plugin_aliases.get(name, name))

    def unblock(self, name: str) -> bool:
        return self._native.unblock(self._plugin_aliases.get(name, name))

    def get_hookcallers(self, plugin: object) -> list[HookCaller] | None:
        names = self._native.get_hookcallers(plugin)
        return None if names is None else [getattr(self.hook, name) for name in names]

    def subset_hook_caller(self, name: str, remove_plugins: Sequence[object]) -> HookCaller:
        return HookCaller(self._native, name, remove_plugins)


class _InicfgView(Mapping[str, object]):
    """Read-only view of the settings as written in the configuration file."""

    def __init__(self, configuration: NativePytestConfiguration) -> None:
        self._configuration = configuration

    @override
    def __getitem__(self, key: str) -> object:
        value = self._configuration.ini_value(key)
        if value is None:
            raise KeyError(key)
        return value

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(self._configuration.ini_names())

    @override
    def __len__(self) -> int:
        return len(self._configuration.ini_names())


class Config:
    """Expose configuration values already resolved by the native frontend."""

    cache: Cache
    _parser: Parser

    def __init__(
        self,
        pluginmanager: PluginManager,
        root: str,
        options: Mapping[str, object],
        configuration: NativePytestConfiguration,
        ini: dict[str, IniDeclaration],
        inipath: str | None = None,
    ) -> None:
        self.pluginmanager = pluginmanager
        self.hook = pluginmanager.hook
        self.rootpath = Path(root)
        self._invocation_dir = Path.cwd()
        self.inipath = None if inipath is None else Path(inipath)
        options = dict(options)
        # Only worker processes publish xdist's worker attributes.
        workerinput = options.pop("_workerinput", None)
        if workerinput is not None:
            self.workerinput = workerinput
            self.workeroutput: dict[str, object] = {}
        self.option = SimpleNamespace(**options)
        self.known_args_namespace: argparse.Namespace = argparse.Namespace(**options)
        self._configuration = configuration
        self._ini = ini
        self._inicache: dict[str, object] = {}
        self._cleanup: list[Callable[[], object]] = []
        self._configured = False
        self.stash = Stash()

    @property
    def inicfg(self) -> Mapping[str, object]:
        warnings.warn(
            PytestRemovedIn10Warning(
                "config.inicfg is deprecated, use config.getini() to access configuration "
                "values instead.\nSee https://docs.pytest.org/en/stable/deprecations.html"
                "#config-inicfg"
            ),
            stacklevel=2,
        )
        return _InicfgView(self._configuration)

    @property
    def rootdir(self) -> LegacyPath:
        """The rootdir as a legacy ``py.path.local``-style object."""
        from .legacy_path import LegacyPath  # noqa: PLC0415 - legacy API only

        return LegacyPath(str(self.rootpath))

    @property
    def inifile(self) -> LegacyPath | None:
        """The configuration file as a legacy ``py.path.local``-style object."""
        from .legacy_path import LegacyPath  # noqa: PLC0415 - legacy API only

        return LegacyPath(str(self.inipath)) if self.inipath else None

    def cwd_relative_nodeid(self, nodeid: str) -> str:
        return self._configuration.cwd_relative_nodeid(self, nodeid)

    VERBOSITY_TEST_CASES = "test_cases"
    VERBOSITY_ASSERTIONS = "assertions"
    VERBOSITY_SUBTESTS = "subtests"

    def get_verbosity(self, verbosity_type: str | None = None) -> int:
        return native._pytest.get_independent_verbosity(self, verbosity_type)

    def _processopt(self, option: argparse.Action) -> None:
        if not hasattr(self.option, option.dest):
            setattr(self.option, option.dest, option.default)

    def add_cleanup(self, func: Callable[[], object]) -> None:
        self._cleanup.append(func)

    def getoption(self, name: str, default: object = NOTSET, skip: bool = False) -> object:
        name = self._parser._opt2dest.get(name, name)
        if name == "collectonly":
            name = "collect_only"
        value = getattr(self.option, name, default)
        if value is NOTSET or (value is None and skip):
            if skip:
                skip_test(f"no {name!r} option found")
            raise ValueError(f"no option named {name!r}")
        return value

    def getvalue(self, name: str, path: object | None = None) -> object:
        return self.getoption(name)

    def get_terminal_writer(self) -> object:
        reporter = self.pluginmanager.get_plugin("terminalreporter")
        assert reporter is not None
        return getattr(reporter, "_tw")  # noqa: B009 - arbitrary plugins own this CPython attribute.

    def addinivalue_line(self, name: str, line: str) -> None:
        value = self.getini(name)
        if not isinstance(value, list):
            raise ValueError(f"configuration value {name!r} is not a list")
        value.append(line)

    def getini(self, name: str) -> object:
        canonical = name
        if name not in self._ini:
            canonical = next(
                (key for key, (_, _, aliases) in self._ini.items() if name in aliases), name
            )
        if canonical not in self._ini:
            raise ValueError(f"unknown configuration value: {name!r}")
        if canonical not in self._inicache:
            kind, default, aliases = self._ini[canonical]
            self._inicache[canonical] = self._configuration.getini(
                canonical, kind, default, aliases
            )
        return self._inicache[canonical]


class Node:
    """A collection tree node with pytest's public construction protocol."""

    @property
    def ihook(self) -> HookRelay:
        return self.session.gethookproxy(self.path)

    def __init__(
        self,
        name: str,
        parent: Node | None = None,
        config: Config | None = None,
        session: Session | None = None,
        fspath: object = None,
        path: Path | str | None = None,
        nodeid: str | None = None,
    ) -> None:
        if parent is None and (config is None or session is None):
            raise TypeError("a node without a parent requires config and session")
        if path is None and fspath is not None:
            path = str(fspath)
        if path is None:
            path = cast("Node", parent).path
        if nodeid is None:
            owner = cast("Node", parent).nodeid
            nodeid = f"{owner}::{name}" if owner else name
        self._vfy_native_init(
            name,
            nodeid,
            path,
            config if config is not None else cast("Node", parent).config,
            session if session is not None else cast("Node", parent).session,
            parent,
        )

    def _vfy_native_init(
        self,
        name: str,
        nodeid: str,
        path: Path | str,
        config: Config,
        session: Session,
        parent: Node | None = None,
        marks: Sequence[MarkDecorator] = (),
    ) -> None:
        self.name = name
        self.nodeid = nodeid
        self.path = path if isinstance(path, Path) else Path(path)
        self.config = config
        self.session = session
        self.parent = parent
        self.own_markers = list(marks)
        self.keywords = NodeKeywords(self)
        self.extra_keyword_matches: set[str] = set()
        self.stash = Stash()

    @classmethod
    def from_parent(cls, parent: Node, **kwargs: Any) -> Self:  # noqa: ANN401 - subclasses declare their own keyword fields
        """Create a node below ``parent``, as plugin collectors do."""
        for name in ("config", "session"):
            if name in kwargs:
                raise TypeError(f"{name} is not a valid argument for from_parent")
        return cls(parent=parent, **kwargs)

    def addfinalizer(self, fin: Callable[[], object]) -> None:
        self.session._setupstate.addfinalizer(fin, self)

    def setup(self) -> None:
        """Prepare the node before its first item runs."""

    def teardown(self) -> None:
        """Release the node after its last item runs."""

    @property
    def _nodeid(self) -> str:
        return self.nodeid

    @_nodeid.setter
    def _nodeid(self, value: str) -> None:
        self.nodeid = value

    @override
    def __repr__(self) -> str:
        return "<{} {}>".format(self.__class__.__name__, getattr(self, "name", None))

    def warn(self, warning: Warning) -> None:
        """Issue a warning at this node's actual source location."""
        native._pytest.independent_node_warn(self, warning)

    def listchain(self) -> list[Node]:
        chain: list[Node] = []
        current: Node | None = self
        while current is not None:
            chain.append(current)
            current = current.parent
        chain.reverse()
        return chain

    def getparent[T: Node](self, cls: type[T]) -> T | None:
        current: Node | None = self
        while current is not None:
            if isinstance(current, cls):
                return current
            current = current.parent
        return None

    def _repr_failure_py(self, excinfo: object, style: object = None) -> object:
        return native._pytest.independent_item_repr_failure(self, excinfo, style)

    def listextrakeywords(self) -> set[str]:
        return {word for node in self.listchain() for word in node.extra_keyword_matches}

    def iter_markers(self, name: str | None = None) -> Iterator[MarkDecorator]:
        node: Node | None = self
        while node is not None:
            for mark in node.own_markers:
                if name is None or mark.name == name:
                    yield mark
            node = node.parent

    def get_closest_marker(
        self, name: str, default: MarkDecorator | None = None
    ) -> MarkDecorator | None:
        return next(self.iter_markers(name), default)

    def add_marker(self, marker: MarkDecorator | str, append: bool = True) -> None:
        if isinstance(marker, str):
            marker = MarkDecorator(marker)
        self.keywords[marker.name] = marker
        if append:
            self.own_markers.append(marker)
        else:
            self.own_markers.insert(0, marker)

    @property
    def fspath(self) -> LegacyPath:
        """The node's path as a legacy ``py.path.local``-style object."""
        from .legacy_path import LegacyPath  # noqa: PLC0415 - legacy API only

        return LegacyPath(self.path)

    @fspath.setter
    def fspath(self, value: object) -> None:
        self.path = Path(os.fspath(cast("os.PathLike[str]", value)))


class _KeywordSource(Protocol):
    def keys(self) -> Iterable[str]: ...

    def __getitem__(self, key: str, /) -> object: ...


class NodeKeywords(MutableMapping[str, object]):
    """Keywords of one node that fall back to its parent's keywords."""

    __slots__ = ("_markers", "node", "parent")

    def __init__(self, node: Node) -> None:
        self.node = node
        self.parent = node.parent
        self._markers: dict[str, object] = {node.name: True}

    @override
    def __getitem__(self, key: str) -> object:
        try:
            return self._markers[key]
        except KeyError:
            if self.parent is None:
                raise
            return self.parent.keywords[key]

    @override
    def __contains__(self, key: object) -> bool:
        return key in self._markers or (self.parent is not None and key in self.parent.keywords)

    @override
    def __setitem__(self, key: str, value: object) -> None:
        self._markers[key] = value

    @override
    # Pytest reserves the named source "other"; MutableMapping accepts it as a data key.
    def update(  # ty: ignore[invalid-method-override]
        self,
        other: _KeywordSource | Iterable[tuple[str, object]] = (),
        **kwargs: object,
    ) -> None:
        self._markers.update(other, **kwargs)

    @override
    def __delitem__(self, key: str) -> None:
        raise ValueError("cannot delete key in keywords dict")

    @override
    def __iter__(self) -> Iterator[str]:
        yield from self._markers
        if self.parent is not None:
            for keyword in self.parent.keywords:
                if keyword not in self._markers:
                    yield keyword

    @override
    def __len__(self) -> int:
        return sum(1 for _ in self)


class Failed(Exception):  # noqa: N818 - public pytest.Session.Failed exception name
    """Stop the remaining tests after a session failure."""


class CollectError(Exception):
    """An error while collecting a node."""


class Collector(Node):
    """A node whose ``collect()`` yields child collectors and items."""

    CollectError = CollectError

    def collect(self) -> Iterable[Item | Collector]:
        raise NotImplementedError("abstract")

    def repr_failure(self, excinfo: object) -> object:
        return native._pytest.independent_collector_repr_failure(self, excinfo)


class _NativeNode:
    """Builtin nodes that the native collector constructs positionally."""

    def __init__(
        self,
        name: str,
        nodeid: str,
        path: Path | str,
        config: Config,
        session: Session,
        parent: Node | None = None,
        marks: Sequence[MarkDecorator] = (),
    ) -> None:
        node = cast("Node", self)
        node._vfy_native_init(name, nodeid, path, config, session, parent, marks)
        node.keywords.update((mark.name, mark) for mark in node.own_markers)


class FSCollector(Collector):
    """A collector for a filesystem path, named relative to its parent."""

    def __init__(
        self,
        fspath: object = None,
        path_or_parent: Path | Node | None = None,
        path: Path | str | None = None,
        name: str | None = None,
        parent: Node | None = None,
        config: Config | None = None,
        session: Session | None = None,
        nodeid: str | None = None,
    ) -> None:
        if isinstance(path_or_parent, Node):
            parent = path_or_parent
        elif path_or_parent is not None:
            path = path_or_parent
        if path is None and fspath is not None:
            path = str(fspath)
        if path is None:
            raise TypeError("a filesystem collector requires a path")
        path = Path(path)
        if name is None:
            name = path.name
            if parent is not None and parent.path != path and path.is_relative_to(parent.path):
                name = path.relative_to(parent.path).as_posix()
        owner = session if session is not None else cast("Node", parent).session
        if nodeid is None:
            nodeid = owner._node_location_to_relpath(path)
            if nodeid == ".":
                nodeid = ""
        super().__init__(name, parent, config, session, path=path, nodeid=nodeid)


class File(FSCollector):
    """A collector for one file."""


class _CollectionOperation(Protocol):
    def collect(self) -> list[Item]: ...

    def clear_reports(self) -> None: ...


class _NodeFinalizerState(Protocol):
    def addfinalizer(self, finalizer: Callable[[], object], node: Node) -> None: ...


class Session(Collector):
    Interrupted = Interrupted
    Failed = Failed

    def __init__(self, config: Config) -> None:
        self._vfy_native_init("", "", config.rootpath, config, self)
        self._vfy_location_root = native._pytest.NativeRelativeRoot(config.rootpath)
        self.items: list[Item] = []
        self.testscollected = 0
        self.testsfailed = 0
        self.exitstatus: int | ExitCode = ExitCode.OK
        self.shouldstop: bool | str = False
        self.shouldfail: bool | str = False
        self._vfy_report: NativeSessionReports
        self._vfy_collection: NativeCollectionArena
        self._setupstate: _NodeFinalizerState
        self._vfy_perform_collect: _CollectionOperation | None = None

    @property
    def startpath(self) -> Path:
        return self.config._invocation_dir

    def isinitpath(self, path: str | Path, *, with_parents: bool = False) -> bool:
        """Return whether ``path`` was named on the command line (or contains such a path)."""
        return self._vfy_collection.is_initial_path(str(Path(path).absolute()), with_parents)

    def gethookproxy(self, fspath: os.PathLike[str] | str) -> HookRelay:
        path = os.fspath(fspath)
        config = self.config
        return cast(
            "HookRelay", config.pluginmanager._native.hook_proxy(path, config, self._vfy_collection)
        )

    def perform_collect(
        self, args: Sequence[str] | None = None, genitems: bool = True
    ) -> Sequence[Item | Collector]:
        if args is not None or not genitems:
            raise native._pytest.UnsupportedFeatureError(
                "independent Session.perform_collect requires args=None and genitems=True"
            )
        operation = self._vfy_perform_collect
        if operation is None:
            raise native._pytest.UnsupportedFeatureError(
                "independent Session.perform_collect is available during pytest_collection"
            )
        try:
            items = operation.collect()
        finally:
            operation.clear_reports()
            self.config.hook.pytest_collection_finish(session=self)
        self.testscollected = len(items)
        return items

    _node_location_to_relpath = native._pytest.NativeRelativePath()

    pytest_runtest_logreport = native._pytest.NativeSessionLogReportDescriptor()


class Item(Node):
    """A test item; plugins implement ``runtest`` and may override ``reportinfo``."""

    nextitem = None
    # Python test items carry the callable and its function-level attributes; other
    # item kinds leave these unset.
    obj: object
    function: object

    def __init__(
        self,
        name: str,
        parent: Node | None = None,
        config: Config | None = None,
        session: Session | None = None,
        nodeid: str | None = None,
        **kwargs: Any,  # noqa: ANN401 - Node keyword fields such as path
    ) -> None:
        super().__init__(name, parent, config, session, nodeid=nodeid, **kwargs)
        self.fixturenames: list[str] = []
        self._vfy_item_fields()

    def _vfy_item_fields(self) -> None:
        self.user_properties: list[tuple[str, object]] = []
        self._report_sections: list[tuple[str, str, str]] = []
        self._request: NativeIndependentRequest | bool = False

    def runtest(self) -> None:
        raise NotImplementedError("runtest must be implemented by Item subclass")

    def add_report_section(self, when: str, key: str, content: str) -> None:
        if content:
            self._report_sections.append((when, key, content))

    def reportinfo(self) -> tuple[Path | str, int | None, str]:
        return self.path, None, ""

    @cached_property
    def location(self) -> tuple[str, int | None, str]:
        path, lineno, domain = self.reportinfo()
        return self.session._node_location_to_relpath(Path(path)), lineno, str(domain)

    def repr_failure(self, excinfo: object) -> object:
        style = self.config.getoption("tbstyle", "auto")
        if style == "auto":
            style = "long"
        return self._repr_failure_py(excinfo, style=style)


class Function(Item):
    """The live item collected for a Python test function."""

    def __init__(
        self,
        nodeid: str,
        function: object,
        config: Config,
        session: Session,
        fixturenames: list[str],
        marks: list[MarkDecorator],
        own_mark_count: int | None = None,
        parent: Node | None = None,
    ) -> None:
        domain, separator, parameter = nodeid.partition("::")[2].partition("[")
        name = domain.rsplit("::", 1)[-1] + separator + parameter
        path = parent.path if parent is not None else config.rootpath / nodeid.split("::", 1)[0]
        self._vfy_native_init(name, nodeid, path, config, session, parent)
        self.originalname = self.name.split("[", 1)[0]
        self.obj = function
        self.function = function
        self.path = path
        self.fixturenames = fixturenames
        count = len(marks) if own_mark_count is None else own_mark_count
        self.own_markers = marks[:count]
        self.keywords.update((mark.name, mark) for mark in self.own_markers)
        self.keywords.update(getattr(function, "__dict__", {}))
        self._vfy_item_fields()

    reportinfo = native._pytest.NativeFunctionReportInfo()

    @override
    @cached_property
    def location(self) -> tuple[str, int | None, str]:
        return native._pytest.independent_item_location(self)

    @property
    def _fixtureinfo(self) -> FuncFixtureInfo:
        """The fixture names that plug-ins read through ``pytest_pyfunc_call``."""
        try:
            parameters = list(
                inspect.signature(cast("Callable[..., object]", self.obj)).parameters.values()
            )
        except (TypeError, ValueError):
            parameters = []
        kinds = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        argnames = tuple(
            parameter.name
            for parameter in parameters
            if parameter.kind in kinds
            and parameter.default is inspect.Parameter.empty
            and parameter.name in self.fixturenames
        )
        return FuncFixtureInfo(argnames, tuple(self.fixturenames), list(self.fixturenames), {})


class Argument:
    """The registered option that pytest exposes through ``OptionGroup.options``."""

    def __init__(self, action: argparse.Action, attrs: dict[str, Any]) -> None:
        self._action = action
        self._attrs = attrs
        self.dest = action.dest
        self.default = action.default
        self.type = action.type

    def names(self) -> list[str]:
        return list(self._action.option_strings)

    def attrs(self) -> dict[str, Any]:
        return {**self._attrs, "dest": self.dest}


class OptionGroup:
    """A named group of options; argparse registration is shared by all groups."""

    def __init__(self, parser: Parser, name: str = "", description: str = "") -> None:
        self._parser = parser
        self.name = name
        self.description = description
        self.options: list[Argument] = []

    def addoption(self, *names: str, **attrs: Any) -> None:  # noqa: ANN401 - custom argparse Actions accept arbitrary keyword arguments
        action = self._parser._add_argument(*names, **attrs)
        self.options.append(Argument(action, attrs))

    _addoption = addoption


class Parser:
    """Adapt plugin option registration to CPython's argument parser."""

    def __init__(self, parser: argparse.ArgumentParser | None = None) -> None:
        if parser is None:
            self._parser = argument_parser()
        else:
            # Isolate registrations while retaining caller-owned declaration values and callbacks.
            declarations = [
                value
                for action in parser._actions
                for value in (action.default, action.type, action.const, action.choices)
            ]
            declarations.extend(parser._defaults.values())
            declarations.extend(
                callback
                for registry in parser._registries.values()
                for callback in registry.values()
            )
            self._parser = deepcopy(parser, {id(value): value for value in declarations})
        # The bootstrap still recognizes native -n, including retained-session
        # validation. Defer only its owned fallback spelling until third-party
        # declarations have had their normal registration opportunity.
        for option in tuple(self._parser._actions):
            if (
                vars(option).get("_vfy_native_worker_alias") is True
                and "-n" in option.option_strings
            ):
                option.option_strings = [name for name in option.option_strings if name != "-n"]
                del self._parser._option_string_actions["-n"]
                if not option.option_strings:
                    self._parser._remove_action(option)
                    for group in self._parser._action_groups:
                        if option in group._group_actions:
                            group._group_actions.remove(option)
        self._opt2dest = {
            name: action.dest for action in self._parser._actions for name in action.option_strings
        }
        self._processopt: Callable[[argparse.Action], None] | None = None
        self._groups: dict[str, OptionGroup] = {}
        self._ini: dict[str, IniDeclaration] = {
            name: (kind, list(default) if isinstance(default, list) else default, aliases)
            for name, (kind, default, aliases) in _BUILTIN_INI.items()
        }

    def getgroup(self, name: str, description: str = "", after: str | None = None) -> OptionGroup:
        group = self._groups.get(name)
        if group is None:
            group = self._groups[name] = OptionGroup(self, name, description)
        return group

    def addoption(self, *names: str, **attrs: Any) -> None:  # noqa: ANN401 - custom argparse Actions accept arbitrary keyword arguments
        self._add_argument(*names, **attrs)

    def _add_argument(self, *names: str, **attrs: Any) -> argparse.Action:  # noqa: ANN401 - custom argparse Actions accept arbitrary keyword arguments
        option = self._parser.add_argument(*names, **attrs)
        for name in option.option_strings:
            self._opt2dest[name] = option.dest
        self.processoption(option)
        return option

    def processoption(self, option: argparse.Action) -> None:
        if self._processopt is not None and option.dest:
            self._processopt(option)

    def addini(
        self,
        name: str,
        help: str,  # noqa: A002 - pytest Parser.addini accepts this public keyword
        type: str | None = None,  # noqa: A002 - pytest Parser.addini accepts this public keyword
        default: object = NOTSET,
        *,
        aliases: Sequence[str] = (),
    ) -> None:
        if default is NOTSET:
            from vfy.native import (  # noqa: PLC0415 - native configuration policy
                NativePytestConfiguration,
            )

            default = NativePytestConfiguration.ini_default(type)
        self._ini[name] = ("string" if type is None else type, default, tuple(aliases))

    def parse(self, arguments: Sequence[str], final: bool) -> dict[str, object]:
        if final:
            namespace = self._parser.parse_intermixed_args(arguments)
        elif sys.version_info < (3, 12, 8) or (3, 13) <= sys.version_info < (3, 13, 1):
            # Match pytest's early-parser workaround for older argparse.
            namespace, unknown = self._parser.parse_known_args(arguments)
            namespace.paths.extend(argument for argument in unknown if not argument.startswith("-"))
        else:
            namespace, _unknown = self._parser.parse_known_intermixed_args(arguments)
        values: dict[str, object] = {}
        values.update(vars(namespace))
        return values

    def install_native_worker_alias(self) -> None:
        """Retain the native worker aliases when no plugin owns those spellings.

        ``--numprocesses`` is pytest-xdist's spelling of the same count.
        """
        if "-n" not in self._opt2dest:
            self.addoption("-n", dest="workers", type=int, default=argparse.SUPPRESS)
        if "--numprocesses" not in self._opt2dest:
            self.addoption(
                "--numprocesses", dest="workers", type=process_count, default=argparse.SUPPRESS
            )

    def defaults(self) -> dict[str, object]:
        return {
            action.dest: self._parser.get_default(action.dest)
            for action in self._parser._actions
            if action.dest != argparse.SUPPRESS
        }


class Metafunc:
    """Retain Python parameter values registered by generation hooks."""

    def __init__(self, definition: Item, cls: object | None, module: object) -> None:
        self.definition = definition
        self.config = definition.config
        self.function = definition.function
        self.fixturenames = definition.fixturenames
        self.cls = cls
        self.module = module
        self._marks: list[MarkDecorator] = []

    def parametrize(
        self,
        argnames: str | Sequence[str],
        argvalues: object,
        indirect: bool | Sequence[str] = False,
        ids: object = None,
        scope: str | None = None,
        *,
        _param_mark: object = None,
    ) -> None:
        native._pytest.independent_parameter_iterator_warning(self.definition.nodeid, argvalues)
        self._marks.append(
            MarkDecorator(
                "parametrize",
                (argnames, argvalues),
                {"indirect": indirect, "ids": ids, "scope": scope},
            )
        )


class BuiltinConfigureHooks:
    def __init__(self, configure: Callable[[], None]) -> None:
        self._configure = configure

    def pytest_configure(self) -> None:
        self._configure()


class BuiltinRunnerStatusHooks:
    """Forward the runner's normal-priority report classification."""

    def __init__(
        self,
        status: Callable[[NativeIndependentReport], tuple[str, str, str] | None],
        makereport: Callable[[Item, NativeIndependentCallInfo[None]], NativeIndependentReport],
    ) -> None:
        self.pytest_report_teststatus = status
        self.pytest_runtest_makereport = makereport


class BuiltinTerminalStatusHooks:
    """Forward the early terminal module's fallback report classification."""

    def __init__(self, status: Callable[[NativeIndependentReport], tuple[str, str, str]]) -> None:
        self.pytest_report_teststatus = status

    def pytest_addoption(self, parser: Parser) -> None:
        parser.addoption(
            "--color",
            metavar="color",
            action="store",
            dest="color",
            default="auto",
            choices=("yes", "no", "auto"),
            help="Color terminal output (yes/no/auto)",
        )
        parser.addini("console_output_style", "Terminal progress style", default="progress")
        parser.addini("verbosity_test_cases", "Test case execution verbosity", default="auto")


class BuiltinTerminalHooks:
    """Forward terminal hooks to their native lifecycle services."""

    def __init__(
        self,
        terminal_summary: Callable[[], None],
        warning_recorded: Callable[[object, str], None],
        report_header: Callable[[], None],
    ) -> None:
        self._terminal_summary = terminal_summary
        self.pytest_warning_recorded = warning_recorded
        self._report_header = report_header

    @hookimpl(trylast=True)
    def pytest_sessionstart(self) -> None:
        self._report_header()

    @hookimpl(wrapper=True)
    def pytest_sessionfinish(self) -> Iterator[object]:
        result = yield
        self._terminal_summary()
        return result


class BuiltinWarnings:
    """Enter warning scopes at pytest's real session and summary hook positions."""

    @hookimpl(wrapper=True)
    def pytest_sessionfinish(self, session: Session) -> Iterator[object]:
        with session._vfy_report.warning_scope(session.config):
            return (yield)

    @hookimpl(wrapper=True)
    def pytest_terminal_summary(self, terminalreporter: TerminalReporter) -> Iterator[object]:
        # The public reporter is supplied by the active terminal plugin.
        with terminalreporter._status_index.warning_scope(terminalreporter.config):
            return (yield)


def _runxfail_callable(exception: type[BaseException]) -> Callable[..., None]:
    def nop(*args: object, **kwargs: object) -> None:
        pass

    vars(nop)["Exception"] = exception
    return nop


class BuiltinSkippingHooks:
    """Forward skipping hooks at their builtin setup, call, and report priorities."""

    def __init__(
        self,
        setup: Callable[[Item], None],
        status: Callable[[NativeIndependentReport], tuple[str, str, str] | None],
        configure: Callable[[Config], None],
        call_wrapper: Callable[[Item], Iterator[None]],
        report_wrapper: Callable[..., Iterator[None]],
    ) -> None:
        self.pytest_runtest_call = call_wrapper
        self.pytest_runtest_makereport = report_wrapper
        self._setup = setup
        self.pytest_report_teststatus = status
        self.pytest_configure = configure

    def pytest_addoption(self, parser: Parser) -> None:
        parser.addoption("--runxfail", action="store_true", default=False)

    @hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item: Item) -> None:
        __tracebackhide__ = True
        self._setup(item)


class BuiltinPhaseHooks:
    """Enter native logging scopes at the builtin plugin's wrapper priority."""

    def __init__(self, capture: PhaseLogging) -> None:
        self._capture = capture

    @hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionstart(self, session: Session) -> Iterator[object]:
        with self._capture.protocol(session.config):
            return (yield)

    @hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection(self, session: Session) -> Iterator[object]:
        with self._capture.protocol(session.config):
            return (yield)

    @hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session: Session) -> Iterator[object]:
        with self._capture.protocol(session.config):
            return (yield)

    @hookimpl(wrapper=True)
    def pytest_runtestloop(self, session: Session) -> Iterator[object]:
        with self._capture.protocol(session.config):
            return (yield)

    @hookimpl(wrapper=True)
    def pytest_runtest_setup(self) -> Iterator[None]:
        with self._capture.phase("setup"):
            yield

    @hookimpl(wrapper=True)
    def pytest_runtest_call(self) -> Iterator[None]:
        with self._capture.phase("call"):
            yield

    @hookimpl(wrapper=True)
    def pytest_runtest_teardown(self) -> Iterator[None]:
        with self._capture.phase("teardown"):
            yield
