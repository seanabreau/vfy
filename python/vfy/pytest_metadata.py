"""Public metadata protocol for vfy's built-in HTML reporter."""

from __future__ import annotations

from pytest import StashKey  # noqa: PT013 - retain key identity without retaining a session facade.

metadata_key: StashKey[dict[str, object]] = StashKey()

__all__ = ["metadata_key"]
