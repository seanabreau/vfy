"""Adapt native xdist channel codecs without changing execnet's other channels."""

from __future__ import annotations

import builtins
from collections.abc import Callable
from importlib.metadata import version
from operator import attrgetter, is_not
from pathlib import Path
from types import FunctionType, MethodType
from typing import TYPE_CHECKING, cast, override

from execnet import gateway_base

from vfy.native import _pytest

if TYPE_CHECKING:
    from _pytest.config import Config
    from xdist.remote import WorkerInteractor
    from xdist.workermanage import WorkerController

from .xdist_support import trusted_xdist_plugin


class _TransportGlobals(dict[str, object]):
    def __init__(self, function: FunctionType, name: str, codec: Callable[..., object]) -> None:
        super().__init__({"__builtins__": function.__builtins__})
        self._namespace = function.__globals__
        self._name = name
        self._codec = codec

    @override
    def __getitem__(self, name: str) -> object:
        return self._codec if name == self._name else self._namespace[name]


def install_decoder(node: WorkerController) -> None:
    """Keep upstream channel routing and callbacks while replacing their decoder."""
    if version("execnet") != "2.1.2":
        return
    factory = node.gateway._channelfactory
    with node.gateway._receivelock:
        original = factory._local_receive
        previous = factory.__dict__.get("_local_receive")
    if (
        not isinstance(original, MethodType)
        or original.__func__ is not gateway_base.ChannelFactory._local_receive
        or original.__func__.__closure__ is not None
        or Path(original.__func__.__code__.co_filename).resolve()
        != Path(gateway_base.__file__).resolve()
        or "loads_internal" not in original.__func__.__globals__
    ):
        return
    original_loads = cast(
        Callable[[bytes, object | None, tuple[bool, bool] | None], object],
        original.__func__.__globals__["loads_internal"],
    )
    if (
        not isinstance(original_loads, FunctionType)
        or original_loads.__name__ != "loads_internal"
        or Path(original_loads.__code__.co_filename).resolve()
        != Path(gateway_base.__file__).resolve()
    ):
        return
    loads_code = original_loads.__code__
    decoder = _pytest.NativeExecnetDecoder()
    namespace = original.__func__.__globals__
    unserializer = gateway_base.Unserializer
    handlers = {
        b"L": "load_none",
        b"R": "load_true",
        b"C": "load_false",
        b"F": "load_int",
        b"G": "load_int",
        b"H": "load_longint",
        b"I": "load_longint",
        b"D": "load_float",
        b"T": "load_complex",
        b"N": "load_py3string",
        b"M": "load_py2string",
        b"A": "load_bytes",
        b"S": "load_unicode",
        b"K": "load_newlist",
        b"P": "load_setitem",
        b"J": "load_newdict",
        b"@": "load_buildtuple",
        b"O": "load_set",
        b"E": "load_frozenset",
        b"Q": "load_stop",
        b"B": "load_channel",
    }
    if (
        type(unserializer.num2func) is not dict
        or unserializer.num2func
        != {opcode: getattr(unserializer, name, None) for opcode, name in handlers.items()}
        or any(
            not isinstance(function, FunctionType)
            or function is not getattr(unserializer, function.__name__, None)
            or Path(function.__code__.co_filename).resolve()
            != Path(gateway_base.__file__).resolve()
            for function in unserializer.num2func.values()
        )
    ):
        return
    dispatch = unserializer.num2func.copy()
    helpers = tuple(
        (name, getattr(unserializer, name, None))
        for name in ("__init__", "load", "_read_int4", "_read_byte_string", "_load_collection")
    )
    if any(
        not isinstance(function, FunctionType)
        or Path(function.__code__.co_filename).resolve() != Path(gateway_base.__file__).resolve()
        for _name, function in helpers
    ):
        return
    load_defaults = unserializer.load.__defaults__
    if type(load_defaults) is not tuple or len(load_defaults) != 1 or load_defaults[0] is not False:
        return
    functions = tuple(
        {
            cast(FunctionType, function)
            for function in (*dispatch.values(), *(f for _, f in helpers))
        }
    )
    codes = tuple(function.__code__ for function in functions)
    function_code = attrgetter("__code__")
    builtin_namespace = vars(builtins)
    constructors = {
        name: getattr(builtins, name) for name in ("int", "complex", "tuple", "set", "frozenset")
    }
    if any(
        not isinstance(value, type) or value.__module__ != "builtins" or value.__name__ != name
        for name, value in constructors.items()
    ):
        return

    def loads(
        data: bytes,
        channel_or_gateway: object | None = None,
        *strconfig: tuple[bool, bool] | None,
    ) -> object:
        channel = getattr(node, "channel", None)
        upstream = cast(Callable[..., object], namespace["loads_internal"])
        current_dispatch = unserializer.num2func
        if (
            channel is None
            or channel_or_gateway is not channel
            or upstream is not original_loads
            or upstream.__code__ is not loads_code
            or gateway_base.Unserializer is not unserializer
            or type(current_dispatch) is not dict
            or current_dispatch != dispatch
            or any(getattr(unserializer, name, None) is not function for name, function in helpers)
            or unserializer.load.__defaults__ is not load_defaults
            or any(map(is_not, map(function_code, functions), codes))
            or any(
                namespace.get(name, builtin_namespace[name]) is not expected
                for name, expected in constructors.items()
            )
        ):
            return upstream(data, channel_or_gateway, *strconfig)
        configuration = channel._strconfig
        if (
            type(configuration) is not tuple
            or len(configuration) != 2
            or type(configuration[0]) is not bool
            or type(configuration[1]) is not bool
        ):
            return upstream(data, channel_or_gateway, *strconfig)
        return decoder.loads(data, configuration, channel)

    # A receiver subclass would duplicate upstream's callback and error handling.
    # Bind its unchanged code with a decoder local to this factory instead.
    receive = FunctionType(
        original.__func__.__code__,
        _TransportGlobals(cast(FunctionType, original.__func__), "loads_internal", loads),
        argdefs=original.__func__.__defaults__,
    )
    receive.__kwdefaults__ = original.__func__.__kwdefaults__
    installed = MethodType(receive, factory)
    with node.gateway._receivelock:
        current = factory._local_receive
        if (
            not isinstance(current, MethodType)
            or current.__func__ is not original.__func__
            or factory.__dict__.get("_local_receive") is not previous
        ):
            return
        factory.__dict__["_local_receive"] = installed

    def restore() -> None:
        with node.gateway._receivelock:
            if factory.__dict__.get("_local_receive") is not installed:
                return
            if previous is None:
                factory.__dict__.pop("_local_receive", None)
            else:
                factory.__dict__["_local_receive"] = previous

    node.config.add_cleanup(restore)


def install_encoder(config: Config) -> None:
    """Encode only the upstream worker's event channel with native primitives."""
    if version("execnet") != "2.1.2":
        return
    worker = next(
        (
            cast("WorkerInteractor", plugin)
            for name, plugin in config.pluginmanager.list_name_plugin()
            if type(plugin).__name__ == "WorkerInteractor" and trusted_xdist_plugin(plugin, name)
        ),
        None,
    )
    if worker is None:
        return
    channel = worker.channel
    with channel.gateway._receivelock:
        original = channel.send
        previous = channel.__dict__.get("send")
    canonical = gateway_base.Channel.send
    if (
        not isinstance(original, MethodType)
        or original.__func__ is not type(channel).send
        or original.__func__.__closure__ is not None
        or Path(canonical.__code__.co_filename).resolve() != Path(gateway_base.__file__).resolve()
        or original.__func__.__code__.co_code != canonical.__code__.co_code
        or original.__func__.__code__.co_names != canonical.__code__.co_names
        or original.__func__.__code__.co_consts != canonical.__code__.co_consts
    ):
        return
    namespace = original.__func__.__globals__
    upstream = cast(Callable[[object], bytes], namespace["dumps_internal"])
    if not isinstance(upstream, FunctionType):
        return
    encoder = _pytest.NativeExecnetEncoder(upstream)

    def dumps(value: object) -> bytes:
        current = cast(Callable[[object], bytes], namespace["dumps_internal"])
        return encoder.dumps(value) if current is upstream else current(value)

    send = FunctionType(
        original.__func__.__code__,
        _TransportGlobals(cast(FunctionType, original.__func__), "dumps_internal", dumps),
        argdefs=original.__func__.__defaults__,
    )
    send.__kwdefaults__ = original.__func__.__kwdefaults__
    installed = MethodType(send, channel)
    with channel.gateway._receivelock:
        current = channel.send
        if (
            not isinstance(current, MethodType)
            or current.__func__ is not original.__func__
            or channel.__dict__.get("send") is not previous
        ):
            return
        channel.__dict__["send"] = installed

    def restore() -> None:
        with channel.gateway._receivelock:
            if channel.__dict__.get("send") is not installed:
                return
            if previous is None:
                channel.__dict__.pop("send", None)
            else:
                channel.__dict__["send"] = previous

    config.add_cleanup(restore)
