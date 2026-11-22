from __future__ import annotations

from formatting import format_count


def test_format_count() -> None:
    assert format_count(42) == "42"
