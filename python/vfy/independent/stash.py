"""Typed storage for arbitrary Python plugin state on owned pytest objects."""

from __future__ import annotations

from typing import Generic, TypeVar, cast

T = TypeVar("T")


class StashKey(Generic[T]):  # noqa: UP046 - pytest keys require invariant Generic parameters.
    """Give each plugin an identity key associated with its value type."""

    __slots__ = ()


class Stash:
    """Keep plugin values until their owning configuration or node is released."""

    __slots__ = ("_storage",)

    def __init__(self) -> None:
        self._storage: dict[object, object] = {}

    def __getitem__[T](self, key: StashKey[T]) -> T:
        return cast("T", self._storage[key])

    def __setitem__[T](self, key: StashKey[T], value: T) -> None:
        self._storage[key] = value

    def __delitem__[T](self, key: StashKey[T]) -> None:
        del self._storage[key]

    def __contains__[T](self, key: StashKey[T]) -> bool:
        return key in self._storage

    def __len__(self) -> int:
        return len(self._storage)

    def get[T, D](self, key: StashKey[T], default: D) -> T | D:
        """Read through the live item protocol, preserving subclass overrides."""
        try:
            return self[key]
        except KeyError:
            return default

    def setdefault[T](self, key: StashKey[T], default: T) -> T:
        """Insert only when item lookup raises KeyError."""
        try:
            return self[key]
        except KeyError:
            self[key] = default
            return default
