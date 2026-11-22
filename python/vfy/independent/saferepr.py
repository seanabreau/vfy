"""Bind CPython reprlib dispatch to the native safe representation policy."""

from __future__ import annotations

import reprlib
from typing import override

from vfy.native import _pytest


class SafeRepr(reprlib.Repr):
    def __init__(self, maxsize: int | None, use_ascii: bool = False) -> None:
        super().__init__()
        self.maxstring = maxsize if maxsize is not None else 1_000_000_000
        self.maxsize = maxsize
        self.use_ascii = use_ascii

    @override
    def repr(self, x: object) -> str:
        return _pytest.independent_saferepr_render(self, x)

    @override
    def repr_instance(self, x: object, level: int) -> str:
        return _pytest.independent_saferepr_instance(self, x)

    @override
    def repr_dict(self, x: dict[object, object], level: int) -> str:
        return _pytest.independent_saferepr_dict(self, x, level)


def saferepr(obj: object, maxsize: int | None = 240, use_ascii: bool = False) -> str:
    return SafeRepr(maxsize, use_ascii).repr(obj)
