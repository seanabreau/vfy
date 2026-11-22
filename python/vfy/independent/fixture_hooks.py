"""Use CPython's actual unpack operation for cached fixture exceptions."""

from __future__ import annotations

from collections.abc import Iterable


def _unpack_cached_error(values: Iterable[object]) -> tuple[object, object]:
    exception, traceback = values
    return exception, traceback
