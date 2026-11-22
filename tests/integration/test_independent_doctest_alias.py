"""The private doctest import exposes the collector used by the active runner."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
@pytest.mark.parametrize("subclass", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_custom_doctest_collector_matches_pytest(
    tmp_path: Path, subclass: bool, failure: bool
) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            """from pathlib import Path
import pytest
from _pytest.doctest import DoctestModule

class CustomDoctest(DoctestModule):
    def collect(self):
        Path('custom-collector-ran').touch()
        return super().collect()

@pytest.fixture
def answer():
    return 42

def pytest_collect_file(file_path, parent):
    if file_path.name == 'example.py':
        return COLLECTOR.from_parent(parent, path=file_path)
""".replace("COLLECTOR", "CustomDoctest" if subclass else "DoctestModule"),
        )
        project.write(
            "example.py",
            '"""\n>>> getfixture("answer")\n' + ("43\n" if failure else "42\n") + '"""\n',
        )
        arguments = ("-q", "example.py", "--junitxml=results.xml")
        result = (
            project.run_python("-m", "pytest", *arguments)
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
        )
        output = result.stdout + result.stderr
        assert result.returncode == int(failure), output
        assert (project.root / "custom-collector-ran").exists() is subclass
        cases = [
            (case.get("classname"), case.get("name"), [child.tag for child in case])
            for case in ET.parse(project.root / "results.xml").iter("testcase")
        ]
        assert len(cases) == 1, output
        if failure:
            assert "Expected:" in output
            assert "Got:" in output
        observations.append(cases)
    assert observations[0] == observations[1]


def test_private_doctest_alias_loads_actual_collector_on_access(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import sys
import _pytest.doctest as alias

def pytest_configure(config):
    assert 'vfy.independent.doctest_support' not in sys.modules
    assert 'DoctestModule' in dir(alias)
    assert 'DoctestItem' in dir(alias)
    assert 'vfy.independent.doctest_support' not in sys.modules
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        classes = list(pool.map(lambda _: alias.DoctestModule, range(8)))
    assert all(cls is classes[0] for cls in classes)
    namespace = {}
    exec("from _pytest.doctest import *", namespace)
    assert namespace["DoctestModule"] is classes[0]
    assert namespace["DoctestItem"] is alias.DoctestItem

def pytest_collect_file(file_path, parent):
    if file_path.name == 'example.py':
        return alias.DoctestModule.from_parent(parent, path=file_path)

def pytest_collection_modifyitems(items):
    assert any(type(item.parent) is alias.DoctestModule for item in items)
""",
    )
    project.write("example.py", '"""\n>>> 1 + 1\n2\n"""\n')
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q", "example.py")
    result.expect(0)
    assert "1 passed" in result.stdout


@requires_native_pytest
def test_doctest_item_alias_classifies_collected_items_like_pytest(tmp_path: Path) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            """import json
from pathlib import Path
import _pytest.doctest
import pytest

def pytest_collection_finish(session):
    Path('items.json').write_text(json.dumps({
        item.nodeid: isinstance(item, _pytest.doctest.DoctestItem)
        for item in session.items
    }))
    assert _pytest.doctest.DoctestItem is pytest.DoctestItem
""",
        )
        project.write("test_text.txt", ">>> 1 + 1\n2\n")
        project.write("test_normal.py", "def test_normal(): pass\n")
        result = (
            project.run_python("-m", "pytest", "-q")
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        result.expect(0)
        observations.append(json.loads((project.root / "items.json").read_text()))
    assert (
        observations
        == [{"test_normal.py::test_normal": False, "test_text.txt::test_text.txt": True}] * 2
    )
