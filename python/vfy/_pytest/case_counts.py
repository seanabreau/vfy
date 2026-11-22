"""Expanded case counts for pytest's collection and deselection summaries."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import cast, override


class _ExpandedDeselectedItems(list[object]):
    """Keep expanded terminal counts without storing one reference per case."""

    def __init__(self, items: Iterable[object] = ()) -> None:
        super().__init__(items)
        self._repetitions: list[tuple[object, int]] = []
        self._repeated_count = 0

    def add_repetitions(self, item: object, count: int) -> None:
        if count <= 0:
            return
        self._repetitions.append((item, count))
        self._repeated_count += count

    @override
    def __len__(self) -> int:
        return list.__len__(self) + self._repeated_count

    @override
    def __iter__(self) -> Iterator[object]:
        yield from list.__iter__(self)
        for item, count in self._repetitions:
            for _ in range(count):
                yield item

    def filtered_for_display(self) -> _ExpandedDeselectedItems:
        visible = _ExpandedDeselectedItems(
            item for item in list.__iter__(self) if getattr(item, "count_towards_summary", True)
        )
        for item, count in self._repetitions:
            if getattr(item, "count_towards_summary", True):
                visible.add_repetitions(item, count)
        return visible


def _item_case_count(item: object) -> int:
    if (count := getattr(item, "_native_case_count", None)) is not None:
        return cast(int, count)
    callspecs = getattr(item, "_native_callspecs", None)
    return len(callspecs) if callspecs is not None else 1


def _expanded_count(items: Sequence[object]) -> int:
    return sum(_item_case_count(item) for item in items)
