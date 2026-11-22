"""Late diamond-check aliases retain canonical eager constructor warnings."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.native import run_parameter_suite

pytestmark: list[pytest.MarkDecorator] = [
    pytest.mark.skipif(
        sys.platform == "win32" or pytest.__version__ != "9.1.1",
        reason="eager Function batching requires Unix and pinned pytest 9.1.1",
    )
]


def test_eager_function_batch_keeps_late_collector_alias_warning(tmp_path: Path) -> None:
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\nfilterwarnings = error\n")
        project.write(
            "conftest.py",
            "import gc\nimport json\nimport os\nfrom pathlib import Path\n"
            "def pytest_collection():\n    gc.disable()\n"
            "def pytest_collection_finish(session):\n"
            "    data = {'nodeids': [item.nodeid for item in session.items]}\n"
            "    Path('collection.json').write_text(json.dumps(data))\n"
            "    gc.enable()\n",
        )
        project.write(
            "test_family.py",
            "import json\nimport os\nfrom pathlib import Path\n"
            "from _pytest import nodes\n"
            "from _pytest.python import Function, PyobjMixin\n"
            "def pytest_generate_tests(metafunc):\n"
            "    metafunc.parametrize('value', range(4))\n"
            "    if metafunc.function.__name__ == 'test_control':\n"
            "        return\n"
            "    nodes.Collector = PyobjMixin\n"
            "    Function._pytest_diamond_inheritance_warning_shown = False\n"
            "def test_control(value):\n    Path('executed').touch()\n"
            "def test_changed(value):\n    Path('executed').touch()\n",
        )
        result = run_parameter_suite(project, route, env={})
        assert result.returncode == 2, result.stdout + result.stderr
        assert (
            "Function is an Item subclass and should not be a collector, "
            "however its bases PyobjMixin are collectors."
        ) in result.stdout + result.stderr
        assert not (project.root / "executed").exists()
        collection = json.loads((project.root / "collection.json").read_text())
        assert collection["nodeids"] == []
