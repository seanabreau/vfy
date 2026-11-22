"""CPython unpacking for a node's caller-owned source-location pair."""

from __future__ import annotations

from collections.abc import Iterable


def unpack_location(values: Iterable[object]) -> tuple[object, object]:
    path, lineno = values
    return path, lineno
