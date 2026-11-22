"""Fix the collection clock for both routes of a controlled corpus recipe."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from freezegun import freeze_time

if TYPE_CHECKING:
    from _pytest.config import Config
    from freezegun.api import _freeze_time

_clock: _freeze_time | None = None


def _stop_clock() -> None:
    global _clock  # noqa: PLW0603 - the pytest session owns this temporary clock
    if _clock is not None:
        _clock.stop()
        _clock = None


def pytest_configure(config: Config) -> None:
    """Start the recipe clock before test modules are collected."""
    global _clock  # noqa: PLW0603 - restored by collection or configuration cleanup
    clock = freeze_time(
        os.environ["VFY_CORPUS_COLLECTION_CLOCK"], ignore=["vfy", "_pytest", "pytest"]
    )
    clock.start()
    _clock = clock
    config.add_cleanup(_stop_clock)


def pytest_collection_modifyitems() -> None:
    """Restore the live clock before tests and fixtures execute."""
    _stop_clock()
