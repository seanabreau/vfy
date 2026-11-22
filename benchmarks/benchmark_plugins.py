"""Load pytest-asyncio in nested project pytest sessions."""

from __future__ import annotations

from _pytest.main import Session


def pytest_sessionstart(session: Session) -> None:
    """Load pytest-asyncio only in nested upstream pytest processes.

    Datasette's child pytest processes need the upstream plug-in before
    collection. The timed parent uses vfy's asyncio plug-in and rejects
    pytest-asyncio.
    """

    config = session.config
    manager = config.pluginmanager
    if manager.hasplugin("vfy.pytest_plugin") or manager.hasplugin("vfy"):
        return
    if not manager.hasplugin("pytest_asyncio"):
        manager.import_plugin("pytest_asyncio.plugin")
