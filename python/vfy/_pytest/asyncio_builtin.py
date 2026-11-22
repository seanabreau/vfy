"""Load built-in asyncio support without upstream configuration warnings."""

from __future__ import annotations

import pytest

pytest_plugins = ("vfy.asyncio_plugin",)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    # Pytest must import the declared plugin itself before sharing its stash key.
    from vfy.asyncio_plugin import _BUILTIN_ASYNCIO  # noqa: PLC0415

    config.stash[_BUILTIN_ASYNCIO] = True
