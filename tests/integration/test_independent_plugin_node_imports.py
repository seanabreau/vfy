"""Plug-ins import pytest's node classes from `_pytest.nodes` and report import failures."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_plugin_imports_node_classes_from_private_nodes_module(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "nodes_plugin.py",
        """import json
from pathlib import Path

import pytest
from _pytest.nodes import Collector, Directory, File, Item, Node


def pytest_collection_modifyitems(items: list[Item]) -> None:
    Path("nodes.json").write_text(
        json.dumps(
            {
                "identity": [
                    Node in Item.__mro__ and Node in Collector.__mro__,
                    Collector is pytest.Collector,
                    Directory is pytest.Directory,
                    File is pytest.File,
                    Item is pytest.Item,
                ],
                "items": [isinstance(item, Item) for item in items],
            }
        )
    )
""",
    )
    project.write("test_nodes.py", "def test_one():\n    pass\n\ndef test_two():\n    pass\n")
    stock = project.run_pytest("-q", "-p", "nodes_plugin", load_xdist=False)
    stock.expect(0)
    expected = json.loads((project.root / "nodes.json").read_text())
    (project.root / "nodes.json").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q", "-p", "nodes_plugin")
    independent.expect(0)
    assert json.loads((project.root / "nodes.json").read_text()) == expected
    assert expected == {"identity": [True] * 5, "items": [True, True]}


def test_plugin_import_failure_reports_the_import_traceback(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("broken_plugin.py", "import _pytest.no_such_private_module\n")
    project.write("test_body.py", "def test_body():\n    pass\n")
    stock = project.run_pytest("-q", "-p", "broken_plugin", load_xdist=False)
    independent = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-q", "-p", "broken_plugin"
    )
    assert stock.returncode == independent.returncode == 1, stock.stderr + independent.stderr
    for result in (stock, independent):
        assert "broken_plugin.py" in result.stderr
        assert "No module named '_pytest.no_such_private_module'" in result.stderr
