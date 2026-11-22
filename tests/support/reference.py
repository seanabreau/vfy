"""Own the in-memory databases used by Coverage.py differential tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from coverage import Coverage


@contextmanager
def reference_coverage(
    *, branch: bool = False, config_file: bool | str = True
) -> Iterator[Coverage]:
    reference = Coverage(data_file=None, branch=branch, config_file=config_file)
    try:
        yield reference
    finally:
        # Coverage.py retains in-memory SQLite connections until explicitly forced closed.
        reference.get_data().close(force=True)
