"""Late constructor changes retain canonical eager Function behavior."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.native import run_parameter_suite

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or pytest.__version__ != "9.1.1",
    reason="eager Function batching requires Unix and pinned pytest 9.1.1",
)


def test_eager_function_batch_falls_back_after_callobj_default_changes(tmp_path: Path) -> None:
    observations: dict[str, tuple[object, list[str]]] = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            "import gc\nimport json\nimport os\nfrom pathlib import Path\n"
            "def pytest_collection():\n    gc.disable()\n"
            "def pytest_collection_finish(session):\n"
            "    assert len(session.items) == 8\n"
            "    module = session.items[0].module\n"
            "    rows = []\n"
            "    for item in session.items:\n"
            "        if item.originalname == 'test_control':\n"
            "            assert item.obj is item.module.test_control\n"
            "            continue\n"
            "        assert item.obj is module.replacement\n"
            "        assert item.instance is None\n"
            "        rows.append([item.nodeid, item.keywords['bound_source']])\n"
            "    Path(f'collection-{os.getpid()}.json').write_text(json.dumps(rows))\n"
            "    gc.enable()\n",
        )
        project.write(
            "test_family.py",
            "from pathlib import Path\nfrom _pytest.python import Function\n"
            "def replacement(value, request):\n"
            "    assert request.node.obj is replacement\n"
            "    with Path('executed').open('a') as stream:\n"
            "        stream.write(f'replacement:{value}\\n')\n"
            "replacement.bound_source = 'replacement'\n"
            "def pytest_generate_tests(metafunc):\n"
            "    metafunc.parametrize('value', range(4))\n"
            "    if metafunc.function.__name__ == 'test_control':\n"
            "        return\n"
            "    defaults = list(Function.__init__.__defaults__)\n"
            "    defaults[2] = replacement\n"
            "    Function.__init__.__defaults__ = tuple(defaults)\n"
            "def test_control(value):\n"
            "    pass\n"
            "def test_family(value, request):\n"
            "    raise AssertionError('original callable executed')\n"
            "test_family.bound_source = 'original'\n",
        )
        environment = {"VFY_WORKER_START": "exec"}
        result = run_parameter_suite(project, route, env=environment)
        assert result.returncode == 0, result.stdout + result.stderr
        collections = [
            json.loads(path.read_text()) for path in project.root.glob("collection-*.json")
        ]
        assert len(collections) == (2 if route == "native" else 1)
        assert all(collection == collections[0] for collection in collections)
        observations[route] = (
            collections[0],
            sorted((project.root / "executed").read_text().splitlines()),
        )
    assert observations["native"] == observations["pytest"]
    assert observations["pytest"][1] == [f"replacement:{value}" for value in range(4)]


def test_eager_function_batch_keeps_late_abstract_class_constructor_error(tmp_path: Path) -> None:
    warning = "<class '_pytest.python.Function'> is not using a cooperative constructor"
    messages: dict[str, list[str]] = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "pytest.ini", "[pytest]\nfilterwarnings = default::pytest.PytestDeprecationWarning\n"
        )
        project.write(
            "conftest.py",
            "import gc\nimport os\nfrom pathlib import Path\n"
            "def pytest_collection():\n    gc.disable()\n"
            "def pytest_sessionfinish(session):\n"
            "    if session.items:\n"
            "        assert len(session.items) == 4\n"
            "        assert all(item.originalname == 'test_control' for item in session.items)\n"
            "    Path('finished').touch()\n"
            "    gc.enable()\n",
        )
        project.write(
            "test_family.py",
            "from pathlib import Path\nimport pytest\n"
            "@pytest.mark.parametrize('value', range(4))\n"
            "def test_control(value):\n"
            "    Path('executed').touch()\n"
            "class TestFamily:\n"
            "    def pytest_generate_tests(self, metafunc):\n"
            "        metafunc.parametrize('value', range(4))\n"
            "        TestFamily.__abstractmethods__ = frozenset({'test_family'})\n"
            "    def test_family(self, value):\n"
            "        Path('executed').touch()\n",
        )
        environment = {"VFY_WORKER_START": "exec"}
        result = run_parameter_suite(project, route, env=environment)
        output = result.stdout + result.stderr
        assert result.returncode == pytest.ExitCode.INTERRUPTED, output
        messages[route] = [
            line.split("TypeError:", 1)[1].strip()
            for line in output.splitlines()
            if "TypeError:" in line and "abstract" in line and "TestFamily" in line
        ]
        assert messages[route], output
        assert warning in output
        assert (project.root / "finished").exists(), output
        assert not (project.root / "executed").exists()
    assert messages["native"] == messages["pytest"]


def test_eager_function_batch_keeps_base_marker_normalization_after_mapping_mutation(
    tmp_path: Path,
) -> None:
    observations: dict[str, tuple[object, list[str]]] = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "pytest.ini",
            "[pytest]\nfilterwarnings = ignore::pytest.PytestUnknownMarkWarning\n",
        )
        project.write(
            "conftest.py",
            "import gc\nimport json\nimport os\nfrom pathlib import Path\n"
            "def pytest_collection():\n    gc.disable()\n"
            "def pytest_collection_finish(session):\n"
            "    assert len(session.items) == 8\n"
            "    rows = []\n"
            "    for item in session.items:\n"
            "        if item.originalname == 'test_control':\n"
            "            assert item.obj is item.module.test_control\n"
            "            continue\n"
            "        names = [mark.name for mark in item.own_markers]\n"
            "        assert names == ['after']\n"
            "        assert 'after' in item.keywords and 'before' not in item.keywords\n"
            "        rows.append([item.nodeid, names])\n"
            "    Path(f'collection-{os.getpid()}.json').write_text(json.dumps(rows))\n"
            "    gc.enable()\n",
        )
        project.write(
            "test_family.py",
            "from pathlib import Path\nimport pytest\n"
            "def pytest_generate_tests(metafunc):\n"
            "    metafunc.parametrize('value', range(4))\n"
            "    if metafunc.function.__name__ == 'test_control':\n"
            "        return\n"
            "    mark = metafunc.function.pytestmark[0]\n"
            "    vars(mark)['mark'] = pytest.mark.after.mark\n"
            "def test_control(value):\n"
            "    pass\n"
            "@pytest.mark.before\n"
            "def test_family(value, request):\n"
            "    assert request.node.get_closest_marker('after') is not None\n"
            "    with Path('executed').open('a') as stream:\n"
            "        stream.write(str(value) + '\\n')\n",
        )
        environment = {"VFY_WORKER_START": "exec"}
        result = run_parameter_suite(project, route, env=environment)
        assert result.returncode == 0, result.stdout + result.stderr
        collections = [
            json.loads(path.read_text()) for path in project.root.glob("collection-*.json")
        ]
        assert len(collections) == (2 if route == "native" else 1)
        assert all(collection == collections[0] for collection in collections)
        observations[route] = (
            collections[0],
            sorted((project.root / "executed").read_text().splitlines()),
        )
    assert observations["native"] == observations["pytest"]
    assert observations["pytest"][1] == [str(value) for value in range(4)]
