"""Current-test environment values follow the live item node ID."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_current_test_environment_normalizes_live_node_id(tmp_path: Path) -> None:
    observed = {}
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import json, os, pytest
from pathlib import Path

events = []
class NodeId(str):
    pass

def pytest_collection_modifyitems(items):
    items[0]._nodeid = NodeId('test_case.py::test_case-é\\0suffix')

@pytest.fixture(autouse=True)
def observe():
    events.append(os.environ['PYTEST_CURRENT_TEST'])
    yield
    events.append(os.environ['PYTEST_CURRENT_TEST'])

def pytest_sessionfinish():
    Path('events.json').write_text(json.dumps(events))
""",
        )
        project.write(
            "test_case.py",
            "import os\nfrom conftest import events\n"
            "def test_case():\n    events.append(os.environ['PYTEST_CURRENT_TEST'])\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observed[engine] = json.loads((project.root / "events.json").read_text())
    assert (
        observed["native"]
        == observed["stock"]
        == [
            f"test_case.py::test_case-é(null)suffix ({phase})"
            for phase in ("setup", "call", "teardown")
        ]
    )
