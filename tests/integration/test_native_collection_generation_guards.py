"""Generation-time observers and selection changes retain ordinary pytest items."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = """\
import json
from pathlib import Path
from types import ModuleType
import pytest

def record(phase, request):
    with Path('events.jsonl').open('a') as stream:
        stream.write(json.dumps([
            phase, request.node.nodeid,
            [mark.name for mark in request.node.iter_markers()],
        ]) + '\\n')

@pytest.fixture
def resource(request):
    record('setup', request)
    yield
    record('teardown', request)

def test_plain(resource, request):
    record('call', request)

def test_case(value, resource, request):
    assert value in range(3)
    record('call', request)

def pytest_generate_tests(metafunc):
    with Path('generation.txt').open('a') as stream:
        stream.write(metafunc.definition.name + '\\n')
    if metafunc.definition.name != 'test_case':
        return
    metafunc.parametrize('value', [
        pytest.param(0, id='keep', marks=pytest.mark.chosen),
        pytest.param(1, id='drop'),
        pytest.param(2, id='last'),
    ])
    if MODE == 'keyword':
        metafunc.config.option.keyword = 'keep'
    else:
        observer = ModuleType('late_collection_observer')
        observer.__file__ = str(Path(__file__).with_name('conftest.py'))
        def observe(items):
            Path('inventory.txt').write_text('\\n'.join(item.nodeid for item in items))
        observer.pytest_collection_modifyitems = observe
        metafunc.config.pluginmanager.register(observer, 'late_collection_observer')
"""


@pytest.mark.parametrize("mode", ["observer", "keyword"])
def test_generation_changes_preserve_inventory_selection_and_one_callback(
    tmp_path: Path, mode: str
) -> None:
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nmarkers = chosen\n")
        project.write("test_cases.py", f"MODE = {mode!r}\n" + _SOURCE)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "--junitxml=junit.xml",
            "-q",
        )
        result.expect(0)
        assert ("4 passed" if mode == "observer" else "1 passed, 3 deselected") in result.stdout
        cases = [
            (
                case.get("classname"),
                case.get("name"),
                [(child.tag, child.get("type"), child.get("message")) for child in case],
            )
            for case in ET.parse(project.root / "junit.xml").iter("testcase")
        ]
        observations[engine] = (
            (project.root / "generation.txt").read_text(),
            [json.loads(line) for line in (project.root / "events.jsonl").read_text().splitlines()],
            (project.root / "inventory.txt").read_text() if mode == "observer" else "",
            cases,
        )
    assert observations["pytest-native"] == observations["pytest"]
    generation, events, inventory, _cases = observations["pytest"]
    assert generation == "test_plain\ntest_case\n"
    selected = (
        ["test_plain", "test_case[keep]", "test_case[drop]", "test_case[last]"]
        if mode == "observer"
        else ["test_case[keep]"]
    )
    assert [(phase, nodeid) for phase, nodeid, _markers in events] == [
        (phase, f"test_cases.py::{name}")
        for name in selected
        for phase in ("setup", "call", "teardown")
    ]
    if mode == "observer":
        assert inventory.splitlines() == [f"test_cases.py::{name}" for name in selected]
    assert all("chosen" in markers for _phase, nodeid, markers in events if "[keep]" in nodeid)


def test_generated_empty_parameter_id_uses_installed_pytest_naming(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_cases.py",
            "from pathlib import Path\n"
            "from _pytest.python import CallSpec2\n"
            "def pytest_generate_tests(metafunc):\n"
            "    with Path('generation.txt').open('a') as stream:\n"
            "        stream.write(metafunc.definition.name + '\\n')\n"
            "    metafunc._calls.append(CallSpec2())\n"
            "    metafunc.config.option.keyword = 'test_case'\n"
            "def test_case(request):\n"
            "    Path('executed.txt').write_text(request.node.nodeid)\n",
        )
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        assert "1 passed" in result.stdout
        observations[engine] = (
            (project.root / "generation.txt").read_text(),
            (project.root / "executed.txt").read_text(),
        )
    assert observations["pytest-native"] == observations["pytest"]
    assert observations["pytest"][0] == "test_case\n"
    assert observations["pytest"][1] == (
        "test_cases.py::test_case"
        if hasattr(pytest, "HIDDEN_PARAM")
        else "test_cases.py::test_case[]"
    )
