"""Mutating the constructor's zero-super cell preserves stock collection failure."""

from __future__ import annotations

import json
import re
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


def test_eager_function_batch_keeps_changed_constructor_class_cell(tmp_path: Path) -> None:
    errors = {}
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
            "from _pytest.python import Function\n"
            "class Unrelated:\n    pass\n"
            "def pytest_generate_tests(metafunc):\n"
            "    metafunc.parametrize('value', range(4))\n"
            "    if metafunc.function.__name__ == 'test_control':\n"
            "        return\n"
            "    helper = Function.__init__\n"
            "    index = helper.__code__.co_freevars.index('__class__')\n"
            "    assert helper.__closure__[index].cell_contents is Function\n"
            "    helper.__closure__[index].cell_contents = Unrelated\n"
            "def test_control(value):\n    Path('executed').touch()\n"
            "def test_victim(value):\n    Path('executed').touch()\n",
        )
        result = run_parameter_suite(project, route, env={})
        project.write("child-stdout.txt", result.stdout)
        project.write("child-stderr.txt", result.stderr)
        assert result.returncode == 2, result.stdout + result.stderr
        error = re.search(r"TypeError: (super[^\n]+)", result.stdout + result.stderr)
        assert error is not None, result.stdout + result.stderr
        errors[route] = error.group(1)
        assert not (project.root / "executed").exists()
        collection = json.loads((project.root / "collection.json").read_text())
        assert collection["nodeids"] == []
    assert errors["native"] == errors["pytest"]
