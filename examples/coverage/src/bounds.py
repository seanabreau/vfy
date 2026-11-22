from __future__ import annotations


def clamp_low(value: int) -> int:
    if value < 0:
        return 0
    return value
