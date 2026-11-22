"""Shared identities for the independent runner's private Python protocols."""

from __future__ import annotations

from enum import Enum
from typing import Final


class NotSetType(Enum):
    token = 0


NOTSET: Final = NotSetType.token
