"""Mutable parameter containers consumed by the native independent runner."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from functools import total_ordering
from typing import TYPE_CHECKING, Self

from vfy import native

from .facade import fail

if TYPE_CHECKING:
    from .facade import MarkDecorator


@total_ordering
class Scope(Enum):
    Function = "function"
    Class = "class"
    Module = "module"
    Package = "package"
    Session = "session"

    def next_lower(self) -> Scope:
        index = _SCOPE_INDICES[self]
        if index == 0:
            raise ValueError(f"{self} is the lower-most scope")
        return _SCOPE_ORDER[index - 1]

    def next_higher(self) -> Scope:
        index = _SCOPE_INDICES[self]
        if index == len(_SCOPE_ORDER) - 1:
            raise ValueError(f"{self} is the upper-most scope")
        return _SCOPE_ORDER[index + 1]

    def __lt__(self, other: Scope) -> bool:
        return _SCOPE_INDICES[self] < _SCOPE_INDICES[other]

    @classmethod
    def from_user(cls, scope_name: str, descr: str, where: str | None = None) -> Self:
        try:
            return cls(scope_name)
        except ValueError:
            fail(
                f"{descr} {f'from {where} ' if where else ''}"
                f"got an unexpected scope value '{scope_name}'",
                pytrace=False,
            )


_SCOPE_ORDER = tuple(Scope)
_SCOPE_INDICES = {scope: index for index, scope in enumerate(_SCOPE_ORDER)}


class CollectError(Exception):
    """A parameter definition cannot produce valid collected items."""


@dataclass(frozen=True)
class CallSpec2:
    """Hold parameter containers for one test item."""

    params: dict[str, object] = field(default_factory=dict)
    indices: dict[str, int] = field(default_factory=dict)
    _arg2scope: Mapping[str, Scope] = field(default_factory=dict)
    _idlist: Sequence[str] = field(default_factory=tuple)
    marks: list[MarkDecorator] = field(default_factory=list)

    def setmulti(
        self,
        *,
        argnames: Iterable[str],
        valset: Iterable[object],
        id: str,  # noqa: A002 - pytest's public keyword.
        marks: Iterable[MarkDecorator],
        scope: Scope,
        param_index: int,
        nodeid: str,
    ) -> CallSpec2:
        return native._pytest.independent_callspec_setmulti(
            self, argnames, valset, id, marks, scope, param_index, nodeid
        )

    def getparam(self, name: str) -> object:
        try:
            return self.params[name]
        except KeyError as error:
            raise ValueError(name) from error

    @property
    def id(self) -> str:
        return "-".join(self._idlist)
