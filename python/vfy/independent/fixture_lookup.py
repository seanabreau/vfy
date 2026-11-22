"""Fixture lookup exceptions retain the original requesting runtime objects."""

from __future__ import annotations

from collections.abc import Sequence
from os import PathLike
from typing import TYPE_CHECKING, override

from vfy.native import _pytest

if TYPE_CHECKING:
    from vfy._pytest_native import NativeIndependentRequest


class FixtureLookupError(LookupError):
    """Retain the request and its selected fixture stack for a failed lookup."""

    def __init__(
        self, argname: str | None, request: NativeIndependentRequest, msg: str | None = None
    ) -> None:
        self.argname = argname
        self.request = request
        self.fixturestack = request._get_fixturestack()
        self.msg = msg

    def formatrepr(self) -> FixtureLookupErrorRepr:
        return _pytest.independent_fixture_lookup_format(self)


class FixtureLookupErrorRepr:
    """Expose mutable lookup diagnostics consumed by the native renderer."""

    def __init__(
        self,
        filename: str | PathLike[str],
        firstlineno: int,
        tblines: Sequence[str],
        errorstring: str,
        argname: str | None,
    ) -> None:
        self.tblines = tblines
        self.errorstring = errorstring
        self.filename = filename
        self.firstlineno = firstlineno
        self.argname = argname

    def toterminal(self, tw: object) -> None:
        _pytest.independent_fixture_lookup_terminal(self, tw)

    @override
    def __str__(self) -> str:
        return _pytest.independent_fixture_lookup_text(self)

    @override
    def __repr__(self) -> str:
        return f"<{self.__class__} instance at {id(self):0x}>"
