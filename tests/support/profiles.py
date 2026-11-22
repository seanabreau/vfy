"""Read total call counts from cProfile's persisted function records."""

from __future__ import annotations

import pstats
from pathlib import Path
from types import CodeType
from typing import Protocol, cast


class _ProfileStats(Protocol):
    # Typeshed omits Stats.stats; cProfile persists five-field function records.
    stats: dict[tuple[str, int, str], tuple[int, int, float, float, object]]


def profile_call_count(path: Path, code: CodeType) -> int:
    """Return the total calls cProfile recorded for ``code`` in the saved profile."""

    stats = cast(_ProfileStats, pstats.Stats(str(path))).stats
    key = (code.co_filename, code.co_firstlineno, code.co_name)
    assert key in stats
    return stats[key][1]
