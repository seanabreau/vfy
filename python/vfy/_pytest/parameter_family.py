"""Admit ordinary pytest 9.1 parameter families to the Rust collection operation."""

from __future__ import annotations

import builtins
import sys
from collections.abc import Callable
from functools import partial, update_wrapper
from types import FunctionType, MethodType, ModuleType

import pytest
from _pytest import python
from _pytest.mark import structures
from pluggy import HookCaller, PluginManager
from pluggy._callers import _multicall
from pluggy._hooks import HookRelay, HookSpec

from vfy._pytest import lazy_function
from vfy.native import _pytest

_original: Callable[..., None] = python.Metafunc.parametrize
_original_code = python.Metafunc.parametrize.__code__
_all = all
_getattr = getattr
_type = type
_vars = vars
_builtin_helpers = tuple(
    (name, getattr(builtins, name))
    for name in (
        "dict",
        "str",
        "bytes",
        "int",
        "float",
        "bool",
        "complex",
        "zip",
        "range",
        "enumerate",
        "len",
        "list",
        "set",
        "isinstance",
        "all",
        "any",
    )
)
_absent = object()
_callspec_fields = ("params", "indices", "_arg2scope", "_idlist", "marks")
_multicall_code = _multicall.__code__


def _raw_helper(owner: object, name: str) -> object:
    # Read descriptors without binding them or calling replacement __eq__.
    if _type(owner) is _type:
        for base in _type.__getattribute__(owner, "__mro__"):
            namespace = _type.__getattribute__(base, "__dict__")
            if name in namespace:
                return namespace[name]
    elif _type(owner) is ModuleType:
        return ModuleType.__getattribute__(owner, "__dict__").get(name, _absent)
    return _absent


if tuple(int(part) for part in pytest.version_tuple[:2]) == (9, 1):
    _helpers = (
        (python, "ParameterSet"),
        (structures, "ParameterSet"),
        (structures, "Collection"),
        (structures, "MarkDecorator"),
        (structures, "collections"),
        (structures.collections, "abc"),
        (structures.collections.abc, "Collection"),
        (python.ParameterSet, "__new__"),
        (python.ParameterSet, "__init__"),
        (python.ParameterSet, "__del__"),
        (python.ParameterSet, "__getattribute__"),
        (python.ParameterSet, "param"),
        (python.ParameterSet, "extract_from"),
        (python.ParameterSet, "_for_parametrize"),
        (python.ParameterSet, "_parse_parametrize_args"),
        (python.ParameterSet, "_parse_parametrize_parameters"),
        (python.Metafunc, "_resolve_parameter_set_ids"),
        (python.Metafunc, "_validate_if_using_arg_names"),
        (python, "_find_parametrized_scope"),
        (python, "Sequence"),
        (python, "_resolve_args_directness"),
        (python.ParameterSet, "values"),
        (python.ParameterSet, "marks"),
        (python.ParameterSet, "id"),
        (python, "IdMaker"),
        (python.IdMaker, "__new__"),
        (python.IdMaker, "__init__"),
        (python.IdMaker, "__del__"),
        (python.IdMaker, "__getattribute__"),
        (python.IdMaker, "argnames"),
        (python.IdMaker, "parametersets"),
        (python.IdMaker, "idfn"),
        (python.IdMaker, "ids"),
        (python.IdMaker, "config"),
        (python.IdMaker, "nodeid"),
        (python.IdMaker, "make_unique_parameterset_ids"),
        (python.IdMaker, "_resolve_ids"),
        (python.IdMaker, "_idval"),
        (python.IdMaker, "_idval_from_function"),
        (python.IdMaker, "_idval_from_hook"),
        (python.IdMaker, "_idval_from_value"),
        (python, "normalize_mark_list"),
        (python, "CallSpec2"),
        (python.CallSpec2, "__init__"),
        (python.CallSpec2, "__del__"),
        (python.CallSpec2, "__new__"),
        (python.CallSpec2, "__getattribute__"),
        (python.CallSpec2, "setmulti"),
        (python, "DirectParamFixtureDef"),
        (python.DirectParamFixtureDef, "__init__"),
        (pytest.Config, "__getattribute__"),
        (pytest.Config, "__bool__"),
        (pytest.Config, "__len__"),
        (pytest.Config, "pluginmanager"),
        (pytest.Config, "hook"),
        (pytest.PytestPluginManager, "__getattribute__"),
        (pytest.PytestPluginManager, "_hookexec"),
        (pytest.PytestPluginManager, "_inner_hookexec"),
        (PluginManager, "_hookexec"),
        (HookRelay, "__getattribute__"),
        (HookRelay, "pytest_make_parametrize_id"),
        (HookCaller, "__getattribute__"),
        (HookCaller, "__call__"),
        (HookCaller, "_hookexec"),
        (HookCaller, "_hookimpls"),
        (HookCaller, "_call_history"),
        (HookCaller, "name"),
        (HookCaller, "spec"),
        (HookCaller, "get_hookimpls"),
        (HookCaller, "is_historic"),
        (HookCaller, "_verify_all_args_are_provided"),
        (HookSpec, "__getattribute__"),
        (HookSpec, "__bool__"),
        (HookSpec, "__len__"),
        (HookSpec, "argnames"),
        (HookSpec, "opts"),
    )
    _snapshots = tuple((owner, name, _raw_helper(owner, name)) for owner, name in _helpers)
else:
    _snapshots = ()
_helper_codes = tuple(
    (helper, helper.__code__)
    for _owner, _name, helper in _snapshots
    if _type(helper) is FunctionType
)


class _Parametrize(partial[None]):
    """Bind native dispatch without retaining a Python invocation frame."""

    if sys.version_info < (3, 14):
        # Python 3.14+ partial objects already bind instances as methods.
        def __get__(
            self, instance: python.Metafunc | None, owner: type[python.Metafunc] | None = None
        ) -> Callable[..., None]:
            if instance is None:
                return self
            return MethodType(self, instance)


def _noop_restore() -> None:
    """Give fork admission a stable cleanup identity when no patch is installed."""


def install() -> Callable[[], None]:
    """Keep unsupported pytest versions and customized helpers canonical."""
    if (
        tuple(int(part) for part in pytest.version_tuple[:2]) != (9, 1)
        or _raw_helper(python.Metafunc, "parametrize") is not _original
        or python.Metafunc.parametrize.__code__ is not _original_code
    ):
        return _noop_restore
    # Check helpers per family before bypassing their per-row calls.

    def standard(config: pytest.Config | None = None) -> bool:
        if not (
            _all(
                _raw_helper(owner, name) is value
                or lazy_function.trusted_callspec_helper(owner, name, value)
                for owner, name, value in _snapshots
            )
            and _all(helper.__code__ is code for helper, code in _helper_codes)
        ):
            return False
        if config is not None:
            # An empty implementation list still passes through Pluggy tracing
            # and add_hookcall_monitoring. Such observers own every ID call.
            if _type(config) is not pytest.Config:
                return False
            manager = _vars(config).get("pluginmanager")
            relay = _vars(config).get("hook")
            if _type(manager) is not pytest.PytestPluginManager or _type(relay) is not HookRelay:
                return False
            hook = _vars(relay).get("pytest_make_parametrize_id")
            if (
                _vars(manager).get("_inner_hookexec") is not _multicall
                or "_hookexec" in _vars(manager)
                or _multicall.__code__ is not _multicall_code
                or _type(hook) is not HookCaller
            ):
                return False
            dispatch = hook._hookexec
            if (
                _type(dispatch) is not MethodType
                or dispatch.__self__ is not manager
                or dispatch.__func__ is not PluginManager._hookexec
                or _type(hook._hookimpls) is not list
                or hook._hookimpls
                or hook._call_history is not None
                or _type(hook.name) is not str
                or hook.name != "pytest_make_parametrize_id"
            ):
                return False
            spec = hook.spec
            if (
                _type(spec) is not HookSpec
                or _type(spec.argnames) is not tuple
                or not _all(_type(name) is str for name in spec.argnames)
                or spec.argnames != ("config", "val", "argname")
                or _type(spec.opts) is not dict
                or not _all(_type(name) is str for name in spec.opts)
                or spec.opts.get("firstresult") is not True
            ):
                return False
        return (
            _all(_vars(python.CallSpec2).get(name, _absent) is _absent for name in _callspec_fields)
            or lazy_function.trusted_callspec_fields()
        ) and _all(
            namespace.get(name, _getattr(builtins, name)) is value
            for namespace in (python.__dict__, structures.__dict__)
            for name, value in _builtin_helpers
        )

    def can_admit(metafunc: object) -> bool:
        if _type(metafunc) is not python.Metafunc:
            return False
        function = metafunc.function
        if _type(function) is MethodType:
            function = function.__func__
        return (
            _type(function) is FunctionType
            and "__signature__" not in _vars(function)
            and "__wrapped__" not in _vars(function)
            and standard()
        )

    parametrize = update_wrapper(
        _Parametrize(_pytest.parametrize_family, _original, can_admit, standard), _original
    )
    type.__setattr__(python.Metafunc, "parametrize", parametrize)

    def restore() -> None:
        if _raw_helper(python.Metafunc, "parametrize") is parametrize:
            type.__setattr__(python.Metafunc, "parametrize", _original)

    return restore
