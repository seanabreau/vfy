"""Stabilize the pinned Datasette tests used by the ecosystem comparison."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from importlib import import_module
from pathlib import Path

import pytest

_RELATIONSHIP_TEST = "tests/test_api.py::test_homepage_sort_by_relationships"
_INSPECT_TESTS = {
    "tests/test_cli.py::test_inspect_cli",
    "tests/test_cli.py::test_inspect_cli_writes_to_file",
}
_TEST_PLUGINS = {
    ("tests/test_plugins.py", "test_hook_actors_from_ids"): "ActorsFromIdsPlugin",
    (
        "tests/test_permissions.py",
        "test_permission_check_explains_required_actions",
    ): "store-query-test",
}
_MODULE_PLUGINS = {
    "tests/test_config_dir.py": "hooray.py",
    "tests/test_plugins.py": "extra_vars.py",
}


@pytest.fixture(autouse=True)
def allow_homepage_row_counts(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep runner scheduling from changing the relationship sort tie-breaker."""

    if request.node.nodeid != _RELATIONSHIP_TEST:
        return
    datasette = request.getfixturevalue("ds_client").ds
    database = datasette.get_database("fixtures")
    table_counts: Callable[[int], Awaitable[dict[str, int | None]]] = database.table_counts
    query_budget = datasette.sql_time_limit_ms
    assert isinstance(query_budget, int)

    async def count_with_fixture_budget(limit: int = 10) -> dict[str, int | None]:
        return await table_counts(query_budget if limit == 10 else limit)

    monkeypatch.setattr(database, "table_counts", count_with_fixture_budget)


@pytest.fixture(autouse=True)
def enter_inspect_database_directory(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep inspect's relative database path independent of test order."""

    if request.node.nodeid not in _INSPECT_TESTS:
        return
    client = request.getfixturevalue("app_client")
    database_path = Path(client.ds.get_database("fixtures").path)
    try:
        Path.cwd()
    except FileNotFoundError:
        # Earlier Datasette fixtures can delete the directory they entered.
        os.chdir(request.config.rootpath)
    monkeypatch.chdir(database_path.parent)


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    """Remove the global registrations fixed upstream in 61a81b62."""

    path, _, test_name = item.nodeid.partition("::")
    test_plugin = _TEST_PLUGINS.get((path, test_name))
    module_plugin = _MODULE_PLUGINS.get(path)
    if nextitem is not None and nextitem.path == item.path:
        module_plugin = None
    if test_plugin is None and module_plugin is None:
        return
    manager = import_module("datasette.plugins").pm
    for plugin in (test_plugin, module_plugin):
        if plugin is not None:
            manager.unregister(name=plugin)
