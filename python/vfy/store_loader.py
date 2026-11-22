"""Coverage data loading for commands that read a combined data file."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .errors import NoDataError

if TYPE_CHECKING:
    from .data import CoverageStore


def _load_store(data_file: str, aliases: list[list[str]] | None = None) -> CoverageStore:
    from .data import CoverageStore  # noqa: PLC0415 - parsed commands load storage on demand

    path = Path(data_file)
    store, count = CoverageStore.combine_transaction(
        [], output=data_file, include_output=True, aliases=aliases
    )
    if count or store is not None:
        assert store is not None
        return store
    if path.exists():
        return CoverageStore.read(path)
    raise NoDataError(f"coverage data file not found: {data_file}")
