"""Class collection failures keep successful siblings and the stock failure location."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CONFTEST = """import json
from pathlib import Path
import pytest

events = []

def pytest_configure(config):
    original_newinstance = pytest.Class.newinstance
    def newinstance(node):
        if node.name == 'TestBroken':
            if ORIGIN == 'module':
                node.obj.fail_scan()
            raise ValueError('actual class graph failure')
        return original_newinstance(node)
    pytest.Class.newinstance = newinstance

def pytest_itemcollected(item):
    events.append(['item', item.nodeid])

def pytest_sessionfinish(session, exitstatus):
    events.append(['finish', int(exitstatus), session.testscollected, session.testsfailed])
    Path('observation.json').write_text(json.dumps(events))
"""

_MODULE = """class TestBroken:
    @staticmethod
    def fail_scan():
        raise ValueError('actual class graph failure')
    def test_broken(self):
        raise AssertionError('must not run')
class TestGood:
    def test_good(self):
        raise AssertionError('must not run')
def test_plain():
    raise AssertionError('must not run')
"""


@pytest.mark.parametrize("style", ["auto", "short", "long"])
@pytest.mark.parametrize("origin", ["conftest", "module"])
def test_class_collection_failure_keeps_siblings_and_stock_location(
    tmp_path: Path, style: str, origin: str
) -> None:
    observations = {}
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", f"ORIGIN = {origin!r}\n" + _CONFTEST)
        project.write("test_siblings.py", _MODULE)
        module = "pytest" if engine == "stock" else "vfy.independent.entry"
        result = project.run_python(
            "-B", "-m", module, "-q", "--tb", style, env={"PYTHONDONTWRITEBYTECODE": "1"}
        )
        result.expect(2)
        assert "INTERNALERROR" not in result.stderr
        assert "actual class graph failure" in result.stdout
        source = _MODULE if origin == "module" else (project.root / "conftest.py").read_text()
        line = next(
            index
            for index, text in enumerate(source.splitlines(), 1)
            if "raise ValueError('actual class graph failure')" in text
        )
        path = "test_siblings.py" if origin == "module" else "conftest.py"
        assert f"{path}:{line}" in result.stdout
        observations[engine] = json.loads((project.root / "observation.json").read_text())
    assert (
        observations["native"]
        == observations["stock"]
        == [
            ["item", "test_siblings.py::TestGood::test_good"],
            ["item", "test_siblings.py::test_plain"],
            ["finish", 2, 2, 1],
        ]
    )
