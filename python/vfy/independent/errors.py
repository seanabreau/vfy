"""Owned exceptions for independent frontend acquisition boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import override


class ConftestImportFailure(Exception):  # noqa: N818 - preserve pytest's exception identity name.
    """Retain the conftest path and its original import exception."""

    def __init__(self, path: Path, *, cause: Exception) -> None:
        self.path = path
        self.cause = cause
        super().__init__(path, cause)

    @override
    def __str__(self) -> str:
        return f"{type(self.cause).__name__}: {self.cause} (from {self.path})"


class TestModuleImportFailure(Exception):  # noqa: N818 - internal marker for the collect report.
    """Retain the test module path and the ImportError or SyntaxError that stopped its import.

    The native collect report renders the text; Python only marks the failure.
    """

    def __init__(self, path: Path, *, cause: BaseException) -> None:
        self.path = path
        self.cause = cause
        super().__init__(path, cause)
