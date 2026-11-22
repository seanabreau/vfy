"""Deferred storage for pytest items and parameter values.

Public attribute access materializes ordinary pytest containers. Installed
descriptors persist after unconfigure so surviving items can still access them.
"""

from __future__ import annotations

import gc
import sys
import sysconfig
from collections.abc import Iterator, Sequence
from inspect import Parameter, Signature
from types import (
    CellType,
    FunctionType,
    GetSetDescriptorType,
    MethodDescriptorType,
    ModuleType,
    WrapperDescriptorType,
)
from typing import TYPE_CHECKING, Any, cast

import pytest
from _pytest import fixtures, nodes
from _pytest.mark import structures
from _pytest.mark.structures import NodeKeywords, get_unpacked_marks
from _pytest.nodes import Item, Node
from _pytest.python import (
    Class,
    Function,
    FunctionDefinition,
    Metafunc,
    Module,
    PyCollector,
    PyobjMixin,
)
from _pytest.scope import Scope
from _pytest.stash import Stash

from vfy.native import _pytest

from .compatibility import pytest_callspec_type

if TYPE_CHECKING:
    from _pytest.python import CallSpec2
else:
    CallSpec2 = pytest_callspec_type()

_enabled_key = pytest.StashKey[bool]()
_node_dict = Node.__dict__["__dict__"]
_callspec_dict = CallSpec2.__dict__["__dict__"]
_callspec_raw_get = (
    _callspec_dict.__get__
    if type(_callspec_dict) is GetSetDescriptorType
    and (
        _callspec_dict.__objclass__ is CallSpec2
        # CPython 3.15 shares its stock instance dictionary descriptor.
        or (_callspec_dict.__objclass__ is object and _callspec_dict is _node_dict)
    )
    and _callspec_dict.__name__ == "__dict__"
    else None
)
_callspec_id = CallSpec2.__dict__["id"]
_callspec_id_code = _callspec_id.fget.__code__
_callspec_new = CallSpec2.__new__
_callspec_init = CallSpec2.__init__
_callspec_init_code = _callspec_init.__code__
_callspec_allocator = object.__new__
_callspec_getstate = CallSpec2.__getstate__
_callspec_reduce = CallSpec2.__reduce__
_callspec_reduce_ex = CallSpec2.__reduce_ex__
_anchor_key = "_vfy_dense_row"
_canonical_scopes = tuple(Scope)
_metafunc_dict = Metafunc.__dict__["__dict__"]
_metafunc_getattribute = Metafunc.__getattribute__
_config_dict = pytest.Config.__dict__["__dict__"]
_config_getattribute = pytest.Config.__getattribute__
_stash_storage = Stash.__dict__["_storage"]
_stash_getattribute = Stash.__getattribute__
_stash_get = Stash.get
_stash_get_code = _stash_get.__code__
_stash_getitem = Stash.__getitem__
_stash_getitem_code = _stash_getitem.__code__
# Pytest before 8.4 has no recomputation helper. Its native execution does not
# install this deferred adapter, which requires the pinned pytest shape.
_recompute_indices = getattr(Metafunc, "_recompute_direct_params_indices", None)
_recompute_indices_code = _recompute_indices.__code__ if _recompute_indices is not None else None
_function_init = Function.__init__
_genfunctions = PyCollector._genfunctions
_function_setattr = Function.__setattr__
_function_delattr = Function.__delattr__
_function_getattribute = Function.__getattribute__
_callspec_original_getattribute = CallSpec2.__getattribute__
_node_init = Node.__init__
_top_request = fixtures.TopRequest
_fixture_request_init = fixtures.FixtureRequest.__init__
_function_initrequest = Function._initrequest
_callspec_fields = ("params", "indices", "_arg2scope", "_idlist", "marks")
# Installation guards all native inventory use; the initial sentinel stays None.
_inventory = cast("_pytest.NativeDeferredFunctionInventory", None)
_installed = False
_callspec_descriptors: dict[str, object] = {}


def enabled(config: pytest.Config) -> bool:
    return config.stash.get(_enabled_key, False)


def trusted_callspec_fields() -> bool:
    if (
        not _installed
        or not _reader_slots_standard
        or _CallSpecField.__mro__ is not _reader_field_mro
        or any(
            name in _CallSpecField.__dict__ for name in ("__getattribute__", "__getattr__", "name")
        )
        or _CallSpecField.__dict__.get("__dict__") is not _reader_field_dict
    ):
        return False
    for name, value in _callspec_descriptors.items():
        if (
            CallSpec2.__dict__.get(name) is not value
            or type(value) is not _CallSpecField
            or not _reader_field_state(value, _callspec_dict)
        ):
            return False
        state = _reader_field_dict.__get__(value)
        if (
            type(state) is not dict
            or any(type(key) is not str for key in state)
            or state.get("name") is not name
        ):
            return False
    return True


def trusted_callspec_helper(owner: object, name: str, original: object) -> bool:
    """Accept the one packaged getter while retaining customization rejection."""
    return (
        owner is CallSpec2
        and name == "__getattribute__"
        and original is _callspec_original_getattribute
        and _state_helpers_standard()
        and _callspec_getattribute_standard()
        and trusted_callspec_fields()
    )


def _row(raw: dict[str, object]) -> _pytest.NativeDenseCallSpecRow | None:
    value = dict.get(raw, _anchor_key)
    if type(value) is not _pytest.NativeDenseCallSpecRow:
        return None
    return value if value.owns(raw) else None


_CallSpecField = _pytest.NativeCallSpecField


def _callspec_dict_get(callspec: CallSpec2) -> dict[str, object]:
    raw = cast("dict[str, object]", _callspec_dict.__get__(callspec))
    handle = _row(raw)
    if handle is not None:
        for name in _callspec_fields:
            if name not in raw:
                handle.field(raw, name)
        # The original real dict owns every visible mutable container. Native
        # ownership ends before the public dictionary can escape or be edited.
        handle.detach(raw)
        if raw.get(_anchor_key) is handle:
            del raw[_anchor_key]
        ordered = [(name, raw[name]) for name in _callspec_fields]
        ordered.extend((name, value) for name, value in raw.items() if name not in _callspec_fields)
        raw.clear()
        raw.update(ordered)
    return raw


def _callspec_getstate_lazy(callspec: CallSpec2) -> object:
    raw = cast("dict[str, object]", _callspec_dict.__get__(callspec))
    if _row(raw) is not None:
        _callspec_dict_get(callspec)
    return _callspec_getstate(callspec)


_callspec_getstate_code = _callspec_getstate_lazy.__code__


def _state_helpers_standard() -> bool:
    return (
        CallSpec2.__getstate__ is _callspec_getstate_lazy
        and _callspec_getstate_lazy.__code__ is _callspec_getstate_code
        and CallSpec2.__reduce__ is _callspec_reduce
        and CallSpec2.__reduce_ex__ is _callspec_reduce_ex
    )


_CallSpecGetAttribute = _pytest.NativeCallSpecGetAttribute
_reader_method_type = type(_callspec_dict_get.__get__(CallSpec2))
_reader_getter_state = _CallSpecGetAttribute._ordinary_dispatch_admitted
_callspec_getattribute_adapter = _CallSpecGetAttribute(
    _callspec_original_getattribute, _callspec_dict_get, _reader_method_type
)
_callspec_getattribute_adapter.__signature__ = Signature(
    (
        Parameter("callspec", Parameter.POSITIONAL_OR_KEYWORD, annotation="CallSpec2"),
        Parameter("name", Parameter.POSITIONAL_OR_KEYWORD, annotation="str"),
    ),
    return_annotation="object",
)
_reader_getter_get = _CallSpecGetAttribute.__get__
_reader_getter_call = _CallSpecGetAttribute.__call__
_reader_getter_mro = _CallSpecGetAttribute.__mro__


def _callspec_getattribute_standard() -> bool:
    return (
        _reader_slots_standard
        and _callspec_original_getattribute is object.__getattribute__
        and type(_callspec_getattribute_adapter) is _CallSpecGetAttribute
        and _reader_getter_state(
            _callspec_getattribute_adapter, object.__getattribute__, _reader_method_type
        )
        and CallSpec2.__dict__.get("__getattribute__") is _callspec_getattribute_adapter
        and _CallSpecGetAttribute.__mro__ is _reader_getter_mro
        and _CallSpecGetAttribute.__dict__.get("__get__") is _reader_getter_get
        and _CallSpecGetAttribute.__dict__.get("__call__") is _reader_getter_call
        and _CallSpecField.__dict__.get("__get__") is _reader_field_get
        and _CallSpecField.__dict__.get("__set__") is _reader_field_set
        and _CallSpecField.__dict__.get("__delete__") is _reader_field_delete
    )


_reader_field_get = _CallSpecField.__get__
_reader_field_set = _CallSpecField.__set__
_reader_field_delete = _CallSpecField.__delete__
_reader_field_dict = _CallSpecField.__dict__["__dict__"]
_reader_field_state = _CallSpecField._raw_dispatch_admitted
_reader_field_mro = _CallSpecField.__mro__
_reader_row_type = _pytest.NativeDenseCallSpecRow
_reader_row_field = _reader_row_type.field
_reader_row_owns = _reader_row_type.owns
_reader_row_mro = _reader_row_type.__mro__
_reader_gettrace, _reader_getprofile = sys.gettrace, sys.getprofile
_reader_slots_standard = (
    all(
        type(slot) is WrapperDescriptorType and slot.__objclass__ is owner and slot.__name__ == name
        for owner, name, slot in (
            (_CallSpecGetAttribute, "__get__", _reader_getter_get),
            (_CallSpecGetAttribute, "__call__", _reader_getter_call),
            (_CallSpecField, "__get__", _reader_field_get),
            (_CallSpecField, "__set__", _reader_field_set),
            (_CallSpecField, "__delete__", _reader_field_delete),
        )
    )
    and all(
        type(slot) is MethodDescriptorType
        and slot.__objclass__ is _reader_row_type
        and slot.__name__ == name
        for name, slot in (("field", _reader_row_field), ("owns", _reader_row_owns))
    )
    and (
        type(_reader_getter_state) is MethodDescriptorType
        and _reader_getter_state.__objclass__ is _CallSpecGetAttribute
        and _reader_getter_state.__name__ == "_ordinary_dispatch_admitted"
    )
    and (
        type(_reader_field_state) is MethodDescriptorType
        and _reader_field_state.__objclass__ is _CallSpecField
        and _reader_field_state.__name__ == "_raw_dispatch_admitted"
        and type(_reader_field_dict) is GetSetDescriptorType
        and _reader_field_dict.__objclass__ is _CallSpecField
        and _reader_field_dict.__name__ == "__dict__"
    )
)


def native_callspec_reader_context() -> dict[str, object] | None:
    """Capture reader identities; Rust rechecks getters before each dense read."""
    if not _installed or _callspec_raw_get is None or not _reader_slots_standard:
        return None
    entries = (
        (vars(CallSpec2), "__getattribute__", (_callspec_getattribute_adapter,)),
        (vars(CallSpec2), "__dict__", (_callspec_dict,)),
        (vars(_CallSpecField), "__get__", (_reader_field_get,)),
        (vars(_CallSpecField), "__set__", (_reader_field_set,)),
        (vars(_CallSpecField), "__delete__", (_reader_field_delete,)),
        (vars(_CallSpecField), "__getattribute__", ()),
        (vars(_CallSpecField), "__getattr__", ()),
        (vars(_CallSpecField), "__dict__", (_reader_field_dict,)),
        (vars(_CallSpecField), "name", ()),
        (vars(_CallSpecGetAttribute), "__get__", (_reader_getter_get,)),
        (vars(_CallSpecGetAttribute), "__call__", (_reader_getter_call,)),
        (vars(_reader_row_type), "field", (_reader_row_field,)),
        (vars(_reader_row_type), "owns", (_reader_row_owns,)),
        (vars(_reader_row_type), "__getattribute__", ()),
        (vars(_reader_row_type), "__getattr__", ()),
        *(
            (vars(CallSpec2), name, (descriptor,))
            for name, descriptor in _callspec_descriptors.items()
            if name in ("params", "indices", "_arg2scope")
        ),
    )
    return {
        "constructor": CallSpec2,
        "raw_get": _callspec_raw_get,
        "raw_descriptor": _callspec_dict,
        "method_type": _reader_method_type,
        "entries": entries,
        "getter": _callspec_getattribute_adapter,
        "getter_type": _CallSpecGetAttribute,
        "getter_mro": _reader_getter_mro,
        "row_type": _reader_row_type,
        "row_mro": _reader_row_mro,
        "descriptor_type": _CallSpecField,
        "descriptor_mro": _reader_field_mro,
        "descriptors": tuple(
            (name, descriptor)
            for name, descriptor in _callspec_descriptors.items()
            if name in ("params", "indices", "_arg2scope")
        ),
        "gettrace": _reader_gettrace,
        "getprofile": _reader_getprofile,
    }


def _direct_metafunc_standard() -> bool:
    return Metafunc.__getattribute__ is _metafunc_getattribute and all(
        name not in Metafunc.__dict__ for name in ("config", "_calls", "_params_directness")
    )


def _direct_config_enabled(config: object) -> bool:
    # This additional admission read must not invoke customized Config/Stash
    # getters that canonical parameter production and recomputation omit.
    if (
        type(config) is not pytest.Config
        or pytest.Config.__getattribute__ is not _config_getattribute
        or "stash" in pytest.Config.__dict__
        or Stash.__getattribute__ is not _stash_getattribute
        or Stash.__dict__.get("_storage") is not _stash_storage
        or Stash.get is not _stash_get
        or _stash_get.__code__ is not _stash_get_code
        or Stash.__getitem__ is not _stash_getitem
        or _stash_getitem.__code__ is not _stash_getitem_code
    ):
        return False
    stash = _config_dict.__get__(config).get("stash")
    if type(stash) is not Stash:
        return False
    storage = _stash_storage.__get__(stash)
    if type(storage) is not dict:
        return False
    flag = storage.get(_enabled_key, False)
    return type(flag) is bool and flag


def _direct_helpers_standard() -> bool:
    return (
        _recompute_indices is not None
        and _direct_metafunc_standard()
        and _state_helpers_standard()
        and CallSpec2.__dict__.get("id") is _callspec_id
        and _callspec_id.fget.__code__ is _callspec_id_code
        and CallSpec2.__new__ is _callspec_new
        and _callspec_new is _callspec_allocator
        and CallSpec2.__init__ is _callspec_init
        and _callspec_init.__code__ is _callspec_init_code
        and getattr(Metafunc, "_recompute_direct_params_indices", None) is _dense_recompute_indices
        and _dense_recompute_indices.__code__ is _dense_recompute_indices_code
        and _recompute_indices.__code__ is _recompute_indices_code
        and _callspec_getattribute_standard()
        and trusted_callspec_fields()
    )


def direct_parameter_inventory(
    config: pytest.Config,
) -> _pytest.NativeDeferredFunctionInventory | None:
    """Admit the direct producer after canonical callbacks finish."""
    if _direct_helpers_standard() and _direct_config_enabled(config):
        return _inventory
    return None


def _dense_recompute_indices(metafunc: Metafunc) -> None:
    assert _recompute_indices is not None
    if type(metafunc) is not Metafunc or not _direct_metafunc_standard():
        _recompute_indices(metafunc)
        return
    state = _metafunc_dict.__get__(metafunc)
    if (
        not _direct_helpers_standard()
        or not _direct_config_enabled(state.get("config"))
        or not _inventory.recompute_direct_indices(
            state.get("_calls"), state.get("_params_directness"), CallSpec2
        )
    ):
        _recompute_indices(metafunc)


_dense_recompute_indices_code = _dense_recompute_indices.__code__


def _pack_callspecs(callspecs: list[CallSpec2]) -> None:
    if (
        not _state_helpers_standard()
        or CallSpec2.__dict__.get("id") is not _callspec_id
        or _callspec_id.fget.__code__ is not _callspec_id_code
        or not _callspec_getattribute_standard()
        or not trusted_callspec_fields()
    ):
        return
    _inventory.pack_callspecs(callspecs, CallSpec2, _callspec_dict)


# Deferred storage leaves Function's C attribute slots untouched. These are stock
# helper identities, not a second constructor model or per-header guards.
_eager_gettrace, _eager_getprofile = sys.gettrace, sys.getprofile
_eager_gc_enabled = gc.isenabled
_eager_method_get = FunctionType.__dict__["__get__"]
_eager_function_mro = Function.__mro__
_eager_parent_mros = ((Module, Module.__mro__), (Class, Class.__mro__))
_eager_owned_mros = tuple(
    (owner, owner.__mro__) for owner in (NodeKeywords, Stash, _top_request, pytest.Mark, CallSpec2)
)
_eager_fields = (
    "name",
    "parent",
    "config",
    "session",
    "path",
    "_nodeid",
    "_store",
    "__dict__",
    "keywords",
    "own_markers",
    "extra_keyword_matches",
    "stash",
    "_report_sections",
    "user_properties",
    "originalname",
    "_instance",
    "_obj",
    "callspec",
    "_fixtureinfo",
    "fixturenames",
    "funcargs",
    "_request",
)
_eager_entries = tuple(
    (owner, name, owner.__dict__.get(name))
    for owner, names in (
        (
            Function,
            (
                "__init__",
                "from_parent",
                "_initrequest",
                "obj",
                "instance",
                "_getobj",
                "_getinstance",
                "_ALLOW_MARKERS",
                "__abstractmethods__",
            ),
        ),
        (Item, ("__init__", "_check_item_and_collector_diamond_inheritance")),
        (Node, ("__init__", "from_parent", "__slots__")),
        (nodes.NodeMeta, ("_create", "__call__", "__getattribute__", "__setattr__")),
        (PyobjMixin, ("obj",)),
        (
            Class,
            (
                "newinstance",
                "__getattribute__",
                "obj",
                "nodeid",
                "config",
                "session",
                "path",
            ),
        ),
        (Module, ("__getattribute__", "obj", "nodeid", "config", "session", "path")),
        (
            NodeKeywords,
            (
                "__init__",
                "__new__",
                "__getattribute__",
                "__setattr__",
                "node",
                "parent",
                "_markers",
                "update",
                "__abstractmethods__",
            ),
        ),
        (
            Stash,
            (
                "__init__",
                "__new__",
                "__getattribute__",
                "__setattr__",
                "_storage",
                "__abstractmethods__",
            ),
        ),
        (
            fixtures.TopRequest,
            (
                "__init__",
                "__new__",
                "__getattribute__",
                "__setattr__",
                "__dict__",
                "fixturename",
                "_pyfuncitem",
                "_arg2fixturedefs",
                "_fixture_defs",
                "__abstractmethods__",
            ),
        ),
        (
            fixtures.FixtureRequest,
            (
                "__init__",
                "__new__",
                "__getattribute__",
                "__setattr__",
                "__dict__",
                "fixturename",
                "_pyfuncitem",
                "_arg2fixturedefs",
                "_fixture_defs",
            ),
        ),
        (
            fixtures.FuncFixtureInfo,
            ("names_closure", "name2fixturedefs", "__getattribute__"),
        ),
        (pytest.Mark, ("__getattribute__", "__getattr__", "name", "mark")),
        (CallSpec2, ("__bool__", "__len__")),
    )
    for name in names
) + tuple(
    (owner, name, owner.__dict__.get(name))
    for owner in _eager_function_mro
    for name in _eager_fields
)
_eager_entries += tuple(
    (owner, name, owner.__dict__.get(name))
    for owner in dict.fromkeys((*Module.__mro__, *Class.__mro__))
    for name in (
        "obj",
        "_obj",
        "nodeid",
        "config",
        "session",
        "path",
        "__getattribute__",
    )
)
_eager_codes = tuple(
    (value, value.__code__)
    for _, _, original in _eager_entries
    for value in (
        original.__func__
        if type(original) is classmethod
        else original.fget
        if type(original) is property
        else original,
    )
    if type(value) is FunctionType
) + tuple(
    (helper, helper.__code__) for helper in (get_unpacked_marks, structures.normalize_mark_list)
)
_eager_defaults = tuple(
    (
        helper,
        helper.__defaults__,
        helper.__kwdefaults__,
        tuple((helper.__kwdefaults__ or {}).items()),
    )
    for helper, _ in _eager_codes
)
_eager_request_check = fixtures.check_ispytest
_eager_request_check_code = _eager_request_check.__code__
_eager_get_referents = gc.get_referents
_eager_helpers = tuple(
    helper
    for helper in dict.fromkeys(
        [helper for helper, _ in _eager_codes]
        + [
            _eager_request_check,
            _callspec_id.fget,
            _callspec_init,
            _callspec_getstate_lazy,
            _dense_recompute_indices,
            _recompute_indices,
        ]
    )
    if helper is not None
)
_eager_referents = tuple(_eager_get_referents(*_eager_helpers))
_eager_keyword_defaults = tuple(
    (value, tuple(dict.items(value)))
    for helper in _eager_helpers
    if (value := helper.__kwdefaults__) is not None
)
_eager_aliases = (
    (_node_init.__globals__, "NodeKeywords", NodeKeywords),
    (_node_init.__globals__, "Stash", Stash),
    (_function_init.__globals__, "get_unpacked_marks", get_unpacked_marks),
    (_function_init.__globals__, "NOTSET", _function_init.__globals__["NOTSET"]),
    (
        Item._check_item_and_collector_diamond_inheritance.__globals__,
        "Collector",
        nodes.Collector,
    ),
    (_function_initrequest.__globals__, "fixtures", fixtures),
    (
        get_unpacked_marks.__globals__,
        "normalize_mark_list",
        structures.normalize_mark_list,
    ),
    (structures.normalize_mark_list.__globals__, "Mark", pytest.Mark),
    (_fixture_request_init.__globals__, "check_ispytest", fixtures.check_ispytest),
)

# Constructor guards retain these snapshots. The native tail reads
# exact dictionaries and original C descriptors after Python return callbacks.
_eager_absent = object()
_eager_mro_get = type.__dict__["__mro__"].__get__
_eager_type_dict_get = type.__dict__["__dict__"].__get__
_eager_cell_get = CellType.__dict__["cell_contents"].__get__
_eager_native_guard: dict[str, object] | None = None
_native_owners = dict.fromkeys(
    owner
    for root in (
        Function,
        Module,
        Class,
        NodeKeywords,
        Stash,
        _top_request,
        nodes.NodeMeta,
        pytest.Mark,
        CallSpec2,
        fixtures.FuncFixtureInfo,
    )
    for owner in _eager_mro_get(root)
)
_native_fields: dict[type, dict[str, object]] = {}
for _owner, _field, _value in _eager_entries:
    _raw = _eager_type_dict_get(_owner)
    _native_fields.setdefault(_owner, {})[_field] = _raw.get(_field, _eager_absent)
for _owner in _native_owners:
    if _owner is CallSpec2:
        # Dense installation owns this class's packaged attribute adapter.
        # Its expected state is selected below from captured constants.
        continue
    _raw = _eager_type_dict_get(_owner)
    for _field in ("__new__", "__getattribute__", "__setattr__", "__delattr__"):
        _native_fields.setdefault(_owner, {})[_field] = _raw.get(_field, _eager_absent)
_native_maps = tuple(
    (_eager_type_dict_get(owner), tuple(fields.items())) for owner, fields in _native_fields.items()
) + tuple((namespace, ((field, value),)) for namespace, field, value in _eager_aliases)
_native_maps += ((vars(fixtures), (("TopRequest", _top_request),)),)
_eager_native_guard = {
    "helpers": _eager_helpers,
    "helper_type": FunctionType,
    "referents": _eager_referents,
    "get_referents": _eager_get_referents,
    "gc_enabled": _eager_gc_enabled,
    "gettrace": _eager_gettrace,
    "getprofile": _eager_getprofile,
    "cell_get": _eager_cell_get,
    "marker_dict_get": pytest.Mark.__dict__["__dict__"].__get__,
    "cells": tuple(
        (cell, cell.cell_contents)
        for helper in _eager_helpers
        for cell in (helper.__closure__ or ())
    ),
    "kwdefaults": _eager_keyword_defaults,
    "mro_get": _eager_mro_get,
    "mros": tuple((owner, _eager_mro_get(owner)) for owner in _native_owners) + _eager_owned_mros,
    "maps": _native_maps,
    "absent": _eager_absent,
}
_eager_plain_callspec_entries = tuple(
    (field, _eager_type_dict_get(CallSpec2).get(field, _eager_absent))
    for field in (*_callspec_fields, "__getattribute__", "__getstate__")
)
_eager_reader_maps = tuple(
    (_eager_type_dict_get(owner), ((field, value),))
    for owner, field, value in (
        (_CallSpecGetAttribute, "__get__", _reader_getter_get),
        (_CallSpecGetAttribute, "__call__", _reader_getter_call),
        (_CallSpecField, "__get__", _reader_field_get),
        (_CallSpecField, "__set__", _reader_field_set),
        (_CallSpecField, "__delete__", _reader_field_delete),
        (_CallSpecField, "__dict__", _reader_field_dict),
        (_reader_row_type, "field", _reader_row_field),
        (_reader_row_type, "owns", _reader_row_owns),
    )
) + tuple(
    (_eager_type_dict_get(owner), ((field, _eager_absent),))
    for owner in (_CallSpecField, _reader_row_type)
    for field in ("__getattribute__", "__getattr__", "name")
)
_eager_member_getters = {
    **{
        name: Node.__dict__[name].__get__
        for name in ("config", "session", "path", "_nodeid", "__dict__")
        if name in Node.__dict__
    },
    **{
        name: fixtures.FuncFixtureInfo.__dict__[name].__get__
        for name in ("names_closure", "name2fixturedefs")
    },
    "source_dict": FunctionType.__dict__["__dict__"].__get__,
}


def _eager_helpers_standard() -> bool:
    # One traversal snapshots every helper after its audit callback returns.
    referents = _eager_get_referents(*_eager_helpers)
    if _eager_gc_enabled() or _eager_gettrace() is not None or _eager_getprofile() is not None:
        return False
    if len(referents) != len(_eager_referents) or any(
        value is not original for value, original in zip(referents, _eager_referents, strict=True)
    ):
        return False
    return all(
        type(kwdefaults) is dict
        and len(kwdefaults) == len(entries)
        and all(
            key is original_key and value is original_value
            for (key, value), (original_key, original_value) in zip(
                dict.items(kwdefaults), entries, strict=True
            )
        )
        for kwdefaults, entries in _eager_keyword_defaults
    )


def _eager_structure_standard() -> bool:
    return not (
        _eager_gc_enabled()
        or _eager_gettrace() is not None
        or _eager_getprofile() is not None
        or Function.__mro__ is not _eager_function_mro
        or any(owner.__mro__ is not mro for owner, mro in _eager_parent_mros)
        or any(owner.__mro__ is not mro for owner, mro in _eager_owned_mros)
        or Function.__new__ is not object.__new__
        or Function.__getattribute__ is not _function_getattribute
        or Function.__setattr__ is not _function_setattr
        or Function.__delattr__ is not _function_delattr
        or fixtures.TopRequest is not _top_request
        or any(
            owner.__new__ is not object.__new__
            or owner.__getattribute__ is not object.__getattribute__
            or owner.__setattr__ is not object.__setattr__
            for owner in (NodeKeywords, Stash, _top_request)
        )
        or any(owner.__dict__.get(field) is not value for owner, field, value in _eager_entries)
        or any(namespace.get(field) is not value for namespace, field, value in _eager_aliases)
    )


def _eager_class_bindings(
    origin: type, name: str
) -> tuple[object, tuple[tuple[type, tuple[type, ...]], ...], list[object]] | None:
    class_mro = cast("tuple[type, ...]", _eager_mro_get(origin))
    maps = []
    for field in ("__new__", "__init__", "__getattribute__"):
        for owner in class_mro:
            raw = _eager_type_dict_get(owner)
            value = raw.get(field, _eager_absent)
            maps.append((raw, ((field, value),)))
            if value is not _eager_absent:
                if value is not object.__dict__[field]:
                    return None
                break
    raw = _eager_type_dict_get(origin)
    abstract_methods = raw.get("__abstractmethods__", _eager_absent)
    if abstract_methods is not _eager_absent and (
        type(abstract_methods) is not frozenset or abstract_methods
    ):
        return None
    maps.append((raw, (("__abstractmethods__", abstract_methods),)))
    source = None
    for owner in class_mro:
        raw = _eager_type_dict_get(owner)
        value = raw.get(name, _eager_absent)
        maps.append((raw, ((name, value),)))
        if value is not _eager_absent:
            source = value
            break
    return source, ((origin, class_mro),), maps


def _eager_function_context(
    parent: PyCollector,
    name: str,
    fixtureinfo: object,
) -> dict[str, object] | None:
    # Signals are checked in Rust before this snapshot. The admitted loop keeps
    # the GIL, leaves GC policy alone, and calls only captured builtin operations.
    if (
        _eager_gc_enabled()
        or _eager_gettrace() is not None
        or _eager_getprofile() is not None
        or type(parent) not in (Module, Class)
        or type(name) is not str
        or not name
        or type(fixtureinfo) is not fixtures.FuncFixtureInfo
        or not _eager_structure_standard()
        or not _direct_helpers_standard()
        or any(helper.__code__ is not code for helper, code in _eager_codes)
        or _eager_request_check.__code__ is not _eager_request_check_code
        or any(
            helper.__defaults__ is not defaults
            or helper.__kwdefaults__ is not kwdefaults
            or len(helper.__kwdefaults__ or {}) != len(entries)
            or any(
                key is not original_key or value is not original_value
                for (key, value), (original_key, original_value) in zip(
                    (helper.__kwdefaults__ or {}).items(), entries, strict=True
                )
            )
            for helper, defaults, kwdefaults, entries in _eager_defaults
        )
        or any(
            "__bool__" in owner.__dict__ or "__len__" in owner.__dict__
            for owner in type(parent).__mro__
        )
    ):
        return None
    # Recheck structure after the audited helper snapshot.
    if not _eager_helpers_standard() or not _eager_structure_standard():
        return None
    raw_parent = _node_dict.__get__(parent)
    origin = raw_parent.get("_obj")
    test_class = None
    dynamic_mros = ()
    dynamic_maps = [(raw_parent, (("_obj", origin),))]
    if type(parent) is Class:
        if type(origin) is not type or "newinstance" in raw_parent:
            return None
        dynamic_maps.append((raw_parent, (("newinstance", _eager_absent),)))
        class_bindings = _eager_class_bindings(origin, name)
        if class_bindings is None:
            return None
        source, dynamic_mros, class_maps = class_bindings
        dynamic_maps.extend(class_maps)
        test_class = origin
    else:
        if type(origin) is not ModuleType:
            return None
        source = vars(origin).get(name)
        dynamic_maps.append((vars(origin), ((name, source),)))
    if type(source) is not FunctionType:
        return None
    metadata = vars(source)
    if any(type(key) is not str for key in metadata):
        return None
    marker_binding = metadata.get("pytestmark", _eager_absent)
    base_markers = [] if marker_binding is _eager_absent else marker_binding
    dynamic_maps.append((metadata, (("pytestmark", marker_binding),)))
    if type(base_markers) is not list or any(
        type(mark) is not pytest.Mark or type(mark.name) is not str or "mark" in vars(mark)
        for mark in base_markers
    ):
        return None
    parent_id = parent.nodeid
    diamond_flag = next(
        (
            owner.__dict__["_pytest_diamond_inheritance_warning_shown"]
            for owner in Function.__mro__
            if "_pytest_diamond_inheritance_warning_shown" in owner.__dict__
        ),
        False,
    )
    if type(parent_id) is not str or type(diamond_flag) is not bool:
        return None
    if _eager_native_guard is None:
        return None
    if _installed:
        callspec_entries = (
            ("__getattribute__", _callspec_getattribute_adapter),
            ("__getstate__", _callspec_getstate_lazy),
            *((field, _callspec_descriptors[field]) for field in _callspec_fields),
        )
        dynamic_maps.extend(_eager_reader_maps)
        dynamic_mros = (
            *dynamic_mros,
            (_CallSpecGetAttribute, _reader_getter_mro),
            (_CallSpecField, _reader_field_mro),
            (_reader_row_type, _reader_row_mro),
        )
    else:
        callspec_entries = _eager_plain_callspec_entries
    dynamic_maps.append((_eager_type_dict_get(CallSpec2), callspec_entries))
    native_guard = {
        **_eager_native_guard,
        "dynamic_mros": dynamic_mros,
        "dynamic_maps": tuple(dynamic_maps),
    }
    # These accesses resolve stock member descriptors, after their identity
    # guards. The current parent/source is read after generation callbacks.
    context = {
        "native_guard": native_guard,
        "allocator": object.__new__,
        "function_type": Function,
        "keyword_type": NodeKeywords,
        "stash_type": Stash,
        "request_type": _top_request,
        "callspec_type": CallSpec2,
        "marker_type": pytest.Mark,
        "callspec_dict": _callspec_dict,
        "parent": parent,
        "config": parent.config,
        "session": parent.session,
        "path": parent.path,
        "parent_id": parent_id,
        "set_diamond_flag": not diamond_flag,
        "name": name,
        "source": source,
        "test_class": test_class,
        "method_get": _eager_method_get,
        "fixture_info": fixtureinfo,
        "fixture_names": fixtureinfo.names_closure,
        "fixture_defs": fixtureinfo.name2fixturedefs,
        "base_markers": base_markers,
        "metadata": metadata,
    }
    native_guard["members"] = (
        (_eager_member_getters["__dict__"], parent, raw_parent),
        (_eager_member_getters["source_dict"], source, metadata),
        *(
            (_eager_member_getters[field], parent, context[key])
            for field, key in (
                ("config", "config"),
                ("session", "session"),
                ("path", "path"),
                ("_nodeid", "parent_id"),
            )
        ),
        (_eager_member_getters["names_closure"], fixtureinfo, context["fixture_names"]),
        (
            _eager_member_getters["name2fixturedefs"],
            fixtureinfo,
            context["fixture_defs"],
        ),
    )
    return context


def _lazy_genfunctions(self: PyCollector, name: str, funcobj: object) -> Iterator[Function]:
    if not enabled(self.config) or type(self) not in (Module, Class):
        yield from _genfunctions(self, name, funcobj)
        return
    modulecol = self.getparent(Module)
    assert modulecol is not None
    module = modulecol.obj
    clscol = self.getparent(Class)
    cls: object = (clscol and clscol.obj) or None
    definition = FunctionDefinition.from_parent(self, name=name, callobj=funcobj)
    fixtureinfo = definition._fixtureinfo
    metafunc = Metafunc(
        definition=definition,
        fixtureinfo=fixtureinfo,
        config=self.config,
        cls=cls,
        module=module,
        _ispytest=True,
    )
    methods = []
    if hasattr(module, "pytest_generate_tests"):
        methods.append(module.pytest_generate_tests)
    if cls is not None and hasattr(cls, "pytest_generate_tests"):
        methods.append(cast("type[Any]", cls)().pytest_generate_tests)
    self.ihook.pytest_generate_tests.call_extra(methods, {"metafunc": metafunc})
    if not metafunc._calls:
        yield Function.from_parent(self, name=name, fixtureinfo=fixtureinfo)
        return
    metafunc._recompute_direct_params_indices()
    fixtureinfo.prune_dependency_tree()
    if type(metafunc._calls) is list:
        _pack_callspecs(metafunc._calls)
    if type(metafunc._calls) is list:
        items = _inventory.eager_function_family(
            metafunc._calls, _eager_function_context, (self, name, fixtureinfo)
        )
        if items is not None:
            yield from items
            return
    for callspec in metafunc._calls:
        binding = _inventory.callspec_binding(callspec)
        if (
            binding is None
            or CallSpec2.__dict__.get("id") is not _callspec_id
            or _callspec_id.fget.__code__ is not _callspec_id_code
            or not _callspec_getattribute_standard()
        ):
            if binding is not None:
                _callspec_dict_get(callspec)
            identifier, has_ids = callspec.id, bool(callspec._idlist)
        else:
            family, row = binding
            identifier = family.id(row)
            # Empty hidden-ID lists produce the undecorated name in pytest9.
            has_ids = family.has_ids(row)
        subname = f"{name}[{identifier}]" if has_ids else name
        yield Function.from_parent(
            self,
            name=subname,
            callspec=callspec,
            fixtureinfo=fixtureinfo,
            keywords={identifier: True},
            originalname=name,
        )


def function_scoped_items(items: Sequence[Item]) -> bool:
    if (
        not _state_helpers_standard()
        or not _callspec_getattribute_standard()
        or not trusted_callspec_fields()
    ):
        return False
    return _inventory.function_scoped_items(items, _node_dict, Function, CallSpec2, Scope.Function)


def install_sorter(
    config: pytest.Config, manager: pytest.PytestPluginManager, fixture_manager: object
) -> None:
    impl = next(
        (
            hook
            for hook in manager.hook.pytest_collection_modifyitems.get_hookimpls()
            if hook.plugin is fixture_manager
        ),
        None,
    )
    if impl is None:
        raise RuntimeError("pytest fixture manager has no collection-order hook")
    original = impl.function

    def reorder(items: list[Item]) -> None:
        if function_scoped_items(items):
            items[:] = list(dict.fromkeys(items))
        else:
            original(items)
        _inventory.prune()

    object.__setattr__(impl, "function", reorder)
    config.add_cleanup(lambda: object.__setattr__(impl, "function", original))


def install(config: pytest.Config) -> None:
    global _inventory, _installed  # noqa: PLW0603 - interpreter-lifetime adapter installation
    # The fast path requires the pinned constructor and generator shapes.
    if (
        pytest.__version__ != "9.1.1"
        or _recompute_indices is None
        or sysconfig.get_config_var("Py_GIL_DISABLED")
    ):
        return
    if not _installed:
        if PyCollector._genfunctions is not _genfunctions:
            return
        if (
            CallSpec2.__getstate__ is not _callspec_getstate
            or CallSpec2.__reduce__ is not _callspec_reduce
            or CallSpec2.__reduce_ex__ is not _callspec_reduce_ex
        ):
            return
        _inventory = _pytest.NativeDeferredFunctionInventory(_canonical_scopes, _callspec_dict)
        for name in _callspec_fields:
            descriptor = _CallSpecField(name, _callspec_dict, _callspec_dict_get)
            setattr(CallSpec2, name, descriptor)
            _callspec_descriptors[name] = descriptor
        cast("Any", CallSpec2).__getattribute__ = _callspec_getattribute_adapter
        cast("Any", CallSpec2).__getstate__ = _callspec_getstate_lazy
        PyCollector._genfunctions = _lazy_genfunctions
        if getattr(Metafunc, "_recompute_direct_params_indices", None) is _recompute_indices:
            cast("Any", Metafunc)._recompute_direct_params_indices = _dense_recompute_indices
        _installed = True
    config.stash[_enabled_key] = True
    config.add_cleanup(lambda: config.stash.__setitem__(_enabled_key, False))
    config.add_cleanup(_inventory.prune)
