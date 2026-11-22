"""CPython exception helpers used by native hooks and the independent frontend."""

from __future__ import annotations

from collections.abc import Callable
from types import GetSetDescriptorType, TracebackType
from typing import Never

__tracebackhide__ = True

_EXCEPTION_TRACEBACK = BaseException.__dict__["__traceback__"]
_EXCEPTION_CONTEXT = BaseException.__dict__["__context__"]
assert isinstance(_EXCEPTION_TRACEBACK, GetSetDescriptorType)
assert isinstance(_EXCEPTION_CONTEXT, GetSetDescriptorType)


def generator_type() -> type:
    """Return the builtin generator type without consulting mutable types module state."""
    return (value for value in ()).__class__


def get_result(exception: BaseException, traceback: TracebackType | None) -> Never:
    """Raise a retained hook exception so CPython sets its traceback, as pluggy's Result does."""
    raise exception.with_traceback(traceback)


def plugin_validation_error_init(self: Exception, plugin: object, message: str) -> None:
    """Initialize the public plugin validation exception's instance fields."""
    Exception.__init__(self, message)
    vars(self)["plugin"] = plugin


def call_while_handling[**P, T](
    callback: Callable[P, T], exception: BaseException | None, *args: P.args, **kwargs: P.kwargs
) -> T:
    """Invoke a callback with CPython's handled-exception state restored."""
    if exception is None:
        return callback(*args, **kwargs)
    traceback = _EXCEPTION_TRACEBACK.__get__(exception)
    context = _EXCEPTION_CONTEXT.__get__(exception)
    assert traceback is None or isinstance(traceback, TracebackType)
    assert context is None or isinstance(context, BaseException)
    try:
        raise exception
    except type(exception):
        # Activating a retained error can replace its context with an ambient
        # exception. Restore its actual slots without calling user descriptors.
        _EXCEPTION_CONTEXT.__set__(exception, context)
        _EXCEPTION_TRACEBACK.__set__(exception, traceback)
        return callback(*args, **kwargs)
