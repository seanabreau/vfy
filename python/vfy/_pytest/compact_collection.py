"""Collect parametrized functions as compact native case families.

Pytest imports modules, discovers fixtures, runs ``pytest_generate_tests``, and
assigns parameter IDs. Each family stores CallSpec2 values, avoiding a Function
and TopRequest allocation per case.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import FunctionType, MethodType, ModuleType
from typing import TYPE_CHECKING, Literal, Protocol, cast

import pytest
from _pytest.config import Config
from _pytest.fixtures import FuncFixtureInfo
from _pytest.python import (
    Class,
    Function,
    FunctionDefinition,
    Metafunc,
    Module,
    PyCollector,
)
from _pytest.scope import Scope

from vfy.native import _pytest

from .compatibility import pytest_callspec_type
from .native_state import _NATIVE_ADAPTER_MODULES

if TYPE_CHECKING:
    from _pytest.python import CallSpec2
else:
    CallSpec2 = pytest_callspec_type()


class _NativeMutationController(Protocol):
    def run_native_mutations(self, session: pytest.Session) -> None: ...


_genfunctions = PyCollector._genfunctions
_pycollector_collect = PyCollector.collect
_module_collect = Module.collect
_class_collect = Class.collect
_function_init = Function.__init__
_function_from_parent = Function.from_parent.__func__
_supports_hidden_parameter_ids = hasattr(pytest, "HIDDEN_PARAM")
_native_inventory_required_key = pytest.StashKey[bool]()
_native_family_count_key = pytest.StashKey[int]()
_native_expanded_length_key = pytest.StashKey[int]()
_TRUSTED_HOOK_MODULES = {
    *_NATIVE_ADAPTER_MODULES,
    "vfy.pytest_plugin",
    "vfy.pytest_reporting",
    "vfy.asyncio_plugin",
    "vfy._pytest.asyncio_builtin",
}
_SESSION_OBSERVER_HOOKS = (
    "pytest_configure",
    "pytest_unconfigure",
    "pytest_sessionstart",
    "pytest_sessionfinish",
    "pytest_report_header",
    "pytest_generate_tests",
    "pytest_collection_finish",
    "pytest_runtest_setup",
    "pytest_runtest_teardown",
    "pytest_make_parametrize_id",
    "pytest_deselected",
    "pytest_asyncio_loop_factories",
)


def _has_external_hooks(config: Config, names: Iterable[str]) -> bool:
    for name in names:
        hook = getattr(config.pluginmanager.hook, name, None)
        if hook is None:
            continue
        for implementation in hook.get_hookimpls():
            module = implementation.function.__module__
            if not module.startswith("_pytest.") and module not in _TRUSTED_HOOK_MODULES:
                return True
    return False


def _require_inventory(session: pytest.Session) -> None:
    session.stash[_native_inventory_required_key] = True
    if session.stash.get(_native_expanded_length_key, -1) != len(session.items):
        expand_inventory(session.items)
        session.stash[_native_expanded_length_key] = len(session.items)


def expand_inventory(
    items: list[pytest.Item], *, phase: Literal["collection", "execution"] = "execution"
) -> None:
    """Expose real parameter items when any test can observe the Session."""
    if not items:
        return
    session = items[0].session
    family_count = session.stash.get(_native_family_count_key, 0)
    if not family_count:
        return
    mark_expression = session.config.getoption("markexpr")
    selection_callback = phase == "collection" and (
        type(mark_expression) is not str or "=" in mark_expression
    )
    if (
        not session.stash.get(_native_inventory_required_key, False)
        and not selection_callback
        and not (
            _has_external_hooks(session.config, _SESSION_OBSERVER_HOOKS)
            or (
                phase == "execution"
                and any(
                    "request" in fixtureinfo.names_closure
                    for item in items
                    if (fixtureinfo := getattr(item, "_fixtureinfo", None)) is not None
                )
            )
        )
    ):
        return
    session.stash[_native_inventory_required_key] = True
    expanded: list[pytest.Item] = []
    expanded_families = 0
    for item in items:
        callspecs = vars(item).get("_native_callspecs")
        if callspecs is None:
            expanded.append(item)
            continue
        template = cast(Function, item)
        expanded_families += 1
        metadata = vars(template)["_native_function_metadata"]

        def constructor_snapshot() -> None:
            pass

        # Later generators can replace function marks after earlier items bind.
        vars(constructor_snapshot).update(metadata)
        if "pytestmark" in metadata:
            vars(constructor_snapshot)["pytestmark"] = list(
                vars(template)["_native_initial_markers"]
            )
        cases = _expanded_functions(
            cast(PyCollector, template.parent),
            template.originalname,
            template._fixtureinfo,
            callspecs,
            callobj=constructor_snapshot,
            function_metadata=metadata,
        )
        for case in cases:
            case.obj = template.obj
            expanded.append(case)
    if expanded_families:
        items[:] = expanded
        # Module.collect can still hold families outside session.items.
        session.stash[_native_family_count_key] = family_count - expanded_families


def _plain_function_metadata(funcobj: object) -> bool:
    if type(funcobj) is not FunctionType:
        return False
    if sys.version_info >= (3, 14) and funcobj.__annotate__ is not None:
        return False
    metadata = vars(funcobj)
    if any(type(name) is not str or name != "pytestmark" for name in metadata):
        return False
    marks = metadata.get("pytestmark", [])
    if type(marks) is not list:
        marks = [marks]
    for decorator in marks:
        mark = decorator.mark if type(decorator) is pytest.MarkDecorator else decorator
        if (
            type(mark) is not pytest.Mark
            or type(mark.name) is not str
            or type(mark.kwargs) is not dict
        ):
            return False
        if callable(mark.kwargs.get("ids")):
            return False
    return True


def native_mutation_controller(config: Config) -> _NativeMutationController | None:
    """Return the one verified mutation controller, or None when none is registered.

    Raises ``pytest.UsageError`` for a lookalike or a second controller.
    """
    candidates = [
        plugin
        for plugin in config.pluginmanager.get_plugins()
        if type(plugin).__module__ == "vfy.mutation_runner"
        and type(plugin).__name__ == "NativeMutationController"
    ]
    if not candidates:
        return None
    if len(candidates) != 1 or not is_native_mutation_controller(candidates[0]):
        raise pytest.UsageError(
            "native pytest received an unsupported mutation controller; "
            "only vfy's collected mutation runner is supported"
        )
    return cast(_NativeMutationController, candidates[0])


def is_native_mutation_controller(plugin: object) -> bool:
    """Trust only the packaged controller class, not hook-compatible lookalikes."""
    if (
        type(plugin).__module__ != "vfy.mutation_runner"
        or type(plugin).__name__ != "NativeMutationController"
    ):
        return False
    from vfy.mutation_runner import (  # noqa: PLC0415 - import only after exact-name match
        NativeMutationController,
    )

    return type(plugin) is NativeMutationController


def _has_conftest_collection_modifyitems(config: Config) -> bool:
    implementations = config.pluginmanager.hook.pytest_collection_modifyitems.get_hookimpls()
    return any(
        isinstance(origin, str) and Path(origin).name == "conftest.py"
        for implementation in implementations
        if (origin := getattr(implementation.plugin, "__file__", None)) is not None
    )


def _uses_item_selection(collector: PyCollector) -> bool:
    config = collector.config
    if native_mutation_controller(config) is not None:
        return True
    if any(
        config.pluginmanager.get_plugin(name) is not None
        for name in ("_vfy_test_selection", "_vfy_ci_collection")
    ):
        return True
    if _has_conftest_collection_modifyitems(config):
        return True
    if config.option.collectonly:
        return True
    if config.getoption("keyword"):
        return True
    if config.getoption("deselect"):
        return True
    if config.getoption("keepduplicates", default=False):
        return True
    return any(
        config.getoption(option, default=False)
        for option in (
            "lf",
            "failedfirst",
            "newfirst",
            "stepwise",
            "stepwise_reset",
            "stepwise_skip",
        )
    )


def _has_explicit_node_selection(collector: PyCollector) -> bool:
    # Pytest 8.0 stores initial parts as (path, names) tuples; nonempty names
    # can be explicit node IDs whose selection needs one item per case.
    initial_parts = getattr(collector.session, "_initial_parts", ())
    return any(
        getattr(part, "parametrization", None) is not None
        or bool(getattr(part, "parts", ()))
        or (isinstance(part, tuple) and len(part) > 1 and bool(part[1]))
        for part in initial_parts
    )


def _uses_standard_collection_methods(collector: PyCollector) -> bool:
    # Custom collectors and collection observers can depend on every generated item.
    if type(collector) not in (Module, Class):
        return False
    # pytest_configure can install observers without registering collection hooks.
    original_collect = _module_collect if type(collector) is Module else _class_collect
    return (
        PyCollector.collect is _pycollector_collect
        and getattr(collector.collect, "__func__", None) is original_collect
        and Function.__init__ is _function_init
        and getattr(Function.from_parent, "__func__", None) is _function_from_parent
    )


def _can_elide_parameter_items(collector: PyCollector) -> bool:
    if not _uses_standard_collection_methods(collector):
        return False
    if collector.session.stash.get(_native_inventory_required_key, False):
        return False
    return not _has_external_hooks(
        collector.config,
        (
            "pytest_collect_file",
            "pytest_pycollect_makemodule",
            "pytest_collection",
            "pytest_pycollect_makeitem",
            "pytest_make_collect_report",
            "pytest_collectreport",
            "pytest_collection_modifyitems",
            "pytest_itemcollected",
            *_SESSION_OBSERVER_HOOKS,
        ),
    )


def _can_select_parameters(collector: PyCollector) -> bool:
    # Explicit selectors still produce ordinary items with their complete marks;
    # pytest applies -m to those selected items after node matching.
    if not _can_elide_parameter_items(collector):
        return False
    # A previously failed collector expands into all its children in lastfailed.
    # Preserve that cache transition even without an explicit --lf selector.
    cache = getattr(collector.config, "cache", None)
    return cache is None or not cache.get("cache/lastfailed", {})


def _record_marker_actions(template: Function) -> None:
    original = template.add_marker
    actions: list[tuple[pytest.Mark, bool]] = []

    def add_marker(marker: str | pytest.MarkDecorator, append: bool = True) -> None:
        original(marker, append=append)
        actions.append((template.own_markers[-1 if append else 0], append))

    # Record insertion direction so Rust can replay additions around parameter
    # marks while pytest retains validation and keyword updates on the template.
    vars(template).update(
        _native_initial_markers=tuple(template.own_markers),
        _native_marker_actions=actions,
        add_marker=add_marker,
    )


def _expanded_functions(
    collector: PyCollector,
    name: str,
    fixtureinfo: FuncFixtureInfo,
    callspecs: Iterable[CallSpec2],
    *,
    callobj: object | None = None,
    function_metadata: dict[str, object] | None = None,
) -> Iterator[Function]:
    arguments = {"callobj": callobj} if callobj is not None else {}
    for callspec in callspecs:
        # Before pytest 8.4, even an empty ID list receives the bracket suffix.
        subname = (
            f"{name}[{callspec.id}]"
            if not _supports_hidden_parameter_ids or callspec._idlist
            else name
        )
        keywords = {callspec.id: True}
        if function_metadata is not None:
            keywords = {**function_metadata, **keywords}
        yield Function.from_parent(
            collector,
            name=subname,
            callspec=callspec,
            fixtureinfo=fixtureinfo,
            keywords=keywords,
            originalname=name,
            **arguments,
        )


def _compact_genfunctions(self: PyCollector, name: str, funcobj: object) -> Iterator[Function]:
    if (
        type(self) is Class
        or not _uses_standard_collection_methods(self)
        or not _plain_function_metadata(funcobj)
    ):
        _require_inventory(self.session)
        yield from _genfunctions(self, name, funcobj)
        return

    modulecol = self.getparent(Module)
    assert modulecol is not None
    module = modulecol.obj
    if type(module) is not ModuleType or "__getattr__" in vars(module):
        _require_inventory(self.session)
        yield from _genfunctions(self, name, funcobj)
        return

    definition = FunctionDefinition.from_parent(self, name=name, callobj=funcobj)
    fixtureinfo = definition._fixtureinfo
    metafunc = Metafunc(
        definition=definition,
        fixtureinfo=fixtureinfo,
        config=self.config,
        cls=None,
        module=module,
        _ispytest=True,
    )
    methods = []
    if hasattr(module, "pytest_generate_tests"):
        methods.append(module.pytest_generate_tests)
    if methods or any(
        callable(fixture.ids)
        for definitions in fixtureinfo.name2fixturedefs.values()
        for fixture in definitions
    ):
        _require_inventory(self.session)
    self.ihook.pytest_generate_tests.call_extra(methods, {"metafunc": metafunc})

    from vfy.asyncio_plugin import (  # noqa: PLC0415 - classification is available after generation
        _NATIVE_PLAIN_SYNC_FUNCTION,
    )

    if not definition.stash.get(_NATIVE_PLAIN_SYNC_FUNCTION, False):
        _require_inventory(self.session)

    if not metafunc._calls:
        yield Function.from_parent(self, name=name, fixtureinfo=fixtureinfo)
        return

    # Pytest 8.0 updates this fixture information through prune_dependency_tree
    # alone; the explicit recomputation was added to newer Metafunc versions.
    recompute_direct_params = getattr(metafunc, "_recompute_direct_params_indices", None)
    if callable(recompute_direct_params):
        recompute_direct_params()
    fixtureinfo.prune_dependency_tree()

    # Generation can install observers or change selection options. Ordinary
    # functions need no elision checks; parameter families use the live state.
    if _uses_item_selection(self) or not _can_elide_parameter_items(self):
        yield from _expanded_functions(self, name, fixtureinfo, metafunc._calls)
        return

    # Custom mappings retain their live lookup behavior on ordinary pytest
    # items; compact cases reconstruct only the built-in callspec containers.
    custom_callspecs = any(
        type(callspec) is not CallSpec2
        or type(callspec.params) is not dict
        or type(callspec.indices) is not dict
        or type(callspec._arg2scope) is not dict
        for callspec in metafunc._calls
    )
    # Pytest's sorter needs individual items to order high-scoped parameters.
    explicit_selection = _has_explicit_node_selection(self)
    high_scoped = not custom_callspecs and any(
        scope is not Scope.Function
        for callspec in metafunc._calls
        for scope in callspec._arg2scope.values()
    )
    if (
        explicit_selection
        or custom_callspecs
        or (
            self.config.getoption("markexpr")
            and any(callspec.marks for callspec in metafunc._calls)
        )
        or high_scoped
    ):
        indices = (
            _pytest.selected_parameter_indices(self, name, metafunc._calls)
            if explicit_selection
            and not custom_callspecs
            and not high_scoped
            and _can_select_parameters(self)
            else None
        )
        callspecs = (
            metafunc._calls if indices is None else (metafunc._calls[index] for index in indices)
        )
        yield from _expanded_functions(self, name, fixtureinfo, callspecs)
        return

    template = Function.from_parent(self, name=name, fixtureinfo=fixtureinfo, originalname=name)
    vars(template)["_native_callspecs"] = tuple(metafunc._calls)
    vars(template)["_native_function_metadata"] = dict(vars(funcobj))
    _record_marker_actions(template)
    self.session.stash[_native_family_count_key] = (
        self.session.stash.get(_native_family_count_key, 0) + 1
    )
    yield template


def bind(collector: PyCollector) -> None:
    """Bind compact collection to one collector in the owning native session."""
    # Pytest 9.2 owns new CallSpec and NodeId layouts. Keep its ordinary items
    # until compact reconstruction has a differential proof for those layouts.
    if tuple(int(part) for part in pytest.version_tuple[:2]) >= (9, 2):
        return
    state = vars(collector)
    if state.get("_vfy_compact_collection_bound", False):
        return
    modulecol = collector.getparent(Module)
    if (
        type(collector) is Class
        or (
            modulecol is not None
            and (
                type(modulecol.obj) is not ModuleType
                or "__getattr__" in vars(modulecol.obj)
                or "pytest_generate_tests" in vars(modulecol.obj)
            )
        )
        or _has_external_hooks(collector.config, _SESSION_OBSERVER_HOOKS)
    ):
        _require_inventory(collector.session)
    # Custom collectors own function generation, including callable selection.
    if getattr(collector._genfunctions, "__func__", None) is not _genfunctions:
        return
    # The instance attribute limits this replacement to the session's collectors.
    state["_genfunctions"] = MethodType(_compact_genfunctions, collector)
    state["_vfy_compact_collection_bound"] = True
