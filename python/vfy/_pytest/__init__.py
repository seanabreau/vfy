"""Private adapters between pytest and the native test engine."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .compatibility import (
        PytestCompatibilityPolicy as PytestCompatibilityPolicy,
        pytest_compatibility_policy as pytest_compatibility_policy,
    )


def __getattr__(name: str) -> object:
    """Load upstream-version policy only for consumers of the native adapter."""
    if name in {"PytestCompatibilityPolicy", "pytest_compatibility_policy"}:
        from . import compatibility  # noqa: PLC0415 - native pytest compatibility only

        return getattr(compatibility, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
