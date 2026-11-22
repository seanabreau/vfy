"""pytest's ``Repr*`` exception-report classes, rendered by the native formatter."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Protocol, override

from vfy.native import _pytest


class TerminalPart(Protocol):
    def toterminal(self, tw: object) -> None: ...


@dataclass(eq=False)
class TerminalRepr:
    @override
    def __str__(self) -> str:
        return _pytest.independent_value_repr_text(self)

    @override
    def __repr__(self) -> str:
        return f"<{self.__class__} instance at {id(self):0x}>"

    def toterminal(self, tw: object) -> None:
        raise NotImplementedError


class _NativeTerminalRepr(TerminalRepr):
    """Representations whose terminal output is rendered by the native writer."""

    @override
    def toterminal(self, tw: object) -> None:
        _pytest.independent_value_repr_terminal(self, tw)


@dataclass(eq=False)
class ExceptionRepr(_NativeTerminalRepr):
    reprtraceback: ReprTraceback
    reprcrash: ReprFileLocation | None
    sections: list[tuple[str, str, str]] = field(init=False, default_factory=list)

    def addsection(self, name: str, content: str, sep: str = "-") -> None:
        self.sections.append((name, content, sep))


@dataclass(eq=False)
class ExceptionChainRepr(ExceptionRepr):
    chain: Sequence[tuple[ReprTraceback, ReprFileLocation | None, str | None]]
    _vfy_diagnostic: object | None = field(init=False, default=None, repr=False)

    def __init__(
        self, chain: Sequence[tuple[ReprTraceback, ReprFileLocation | None, str | None]]
    ) -> None:
        super().__init__(reprtraceback=chain[-1][0], reprcrash=chain[-1][1])
        self.chain = chain


@dataclass(eq=False)
class ReprTraceback(_NativeTerminalRepr):
    reprentries: Sequence[ReprEntry | ReprEntryNative]
    extraline: str | None
    style: str
    entrysep: ClassVar[str] = "_ "


@dataclass(eq=False)
class ReprEntry(_NativeTerminalRepr):
    lines: Sequence[str]
    reprfuncargs: TerminalPart | None
    reprlocals: TerminalPart | None
    reprfileloc: ReprFileLocation | None
    style: str

    def _write_entry_lines(self, tw: object) -> None:
        _pytest.independent_repr_entry_lines(self, tw)

    @override
    def __str__(self) -> str:
        return _pytest.independent_value_entry_text(self)


@dataclass(eq=False)
class ReprFileLocation(_NativeTerminalRepr):
    path: str
    lineno: int | None
    message: str

    def __post_init__(self) -> None:
        self.path = str(self.path)


class ReprExceptionInfo(ExceptionRepr):
    pass


@dataclass(eq=False)
class ReprFuncArgs(_NativeTerminalRepr):
    args: Sequence[tuple[str, object]]


@dataclass(eq=False)
class ReprLocals(TerminalRepr):
    lines: Sequence[str]

    @override
    def toterminal(self, tw: object, indent: str = "") -> None:
        _pytest.independent_repr_locals_terminal(self, tw, indent)


class ReprTracebackNative(ReprTraceback):
    def __init__(self, tblines: Sequence[str], *, extraline: str | None = None) -> None:
        self.reprentries = [ReprEntryNative(tblines)]
        self.extraline = extraline
        self.style = "native"


@dataclass(eq=False)
class ReprEntryNative(_NativeTerminalRepr):
    lines: Sequence[str]
    style: ClassVar[str] = "native"
