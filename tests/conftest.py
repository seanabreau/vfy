"""Pytest configuration shared by every test layer."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.support.vfy_project import started_commands, stop_commands_started_since


@pytest.fixture(autouse=True)
def _stop_leftover_project_commands() -> Iterator[None]:
    """Stop managed commands that a test leaves running after it finishes."""

    before = started_commands()
    try:
        yield
    finally:
        stop_commands_started_since(before)
