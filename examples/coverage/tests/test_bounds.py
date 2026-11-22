from __future__ import annotations

from bounds import clamp_low


def test_positive_value() -> None:
    assert clamp_low(5) == 5
