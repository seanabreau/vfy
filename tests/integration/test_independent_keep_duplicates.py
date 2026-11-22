"""Repeated targets retain real collector and fixture-definition identities."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import cast

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()
_TARGETS = {
    "file": ["d/test_case.py", "d/test_case.py"],
    "directory": ["d", "d"],
    "file-selector": ["d/test_case.py", "d/test_case.py::TestGroup::test_case"],
    "selector-file": ["d/test_case.py::TestGroup::test_case", "d/test_case.py"],
    "selector": ["d/test_case.py::TestGroup::test_case"] * 2,
    "class": ["d/test_case.py::TestGroup"] * 2,
}
_OBSERVER = """import json
from pathlib import Path
import pytest

objects = []
events = []

def ordinal(obj):
    for index, previous in enumerate(objects):
        if previous is obj:
            return index
    objects.append(obj)
    return len(objects) - 1

def event(*values):
    events.append(list(values))

@pytest.fixture(scope='session')
def session_owner():
    event('session.setup')
    yield
    event('session.teardown')

@pytest.fixture(scope='module')
def conftest_owner(request):
    event('conftest.setup', ordinal(request.node))
    yield
    event('conftest.teardown', ordinal(request.node))

def pytest_itemcollected(item):
    event('itemcollected', ordinal(item), item.nodeid,
          ordinal(item.getparent(pytest.Module)))

def pytest_collection_finish(session):
    event('selected', [ordinal(item) for item in session.items])

def pytest_sessionfinish(session, exitstatus):
    event('finish', int(exitstatus), session.testscollected, session.testsfailed)
    Path('observed.json').write_text(json.dumps(events))
"""
_BODY = """import pytest
from conftest import event, ordinal

module_entries = []

@pytest.fixture(scope='module')
def module_owner(request):
    token = len(module_entries)
    module_entries.append(token)
    event('module.setup', ordinal(request.node), token)
    yield request.node
    event('module.teardown', ordinal(request.node), token)

class TestGroup:
    @pytest.fixture(scope='class')
    def class_owner(self, request):
        event('class.setup', ordinal(request.node), ordinal(self))
        yield self
        event('class.teardown', ordinal(request.node), ordinal(self))

    def test_case(self, request, module_owner, class_owner, session_owner, conftest_owner):
        definitions = request._fixture_defs
        class_binding = definitions['class_owner']
        assert class_binding.func.__self__ is class_owner
        assert self is not class_owner
        event('call', ordinal(request.node), ordinal(module_owner), ordinal(class_owner),
              ordinal(class_binding), ordinal(definitions['module_owner']),
              ordinal(definitions['session_owner']), ordinal(definitions['conftest_owner']))
"""


def _run(project: VfyProject, engine: str, targets: list[str], option: str | None) -> list[object]:
    arguments = ["-m", engine, "-q"]
    if option is not None:
        arguments.append(option)
    if engine == "vfy.independent.entry":
        arguments.append("--native-result-json=execution.json")
    result = project.run_python(*arguments, *targets, env={"PYTHONPATH": _SOURCE})
    result.expect(0)
    observed = cast("list[object]", json.loads((project.root / "observed.json").read_text()))
    if engine == "vfy.independent.entry":
        record = json.loads((project.root / "execution.json").read_text())
        assert record["engine"] == "vfy-independent-v1"
        assert record["exitstatus"] == 0
        assert [case["test_index"] for case in record["cases"]] == list(range(record["collected"]))
    return observed


@pytest.mark.parametrize("mode", _TARGETS)
@pytest.mark.parametrize("option", [None, "--keepduplicates", "--keep-duplicates"])
def test_duplicate_targets_preserve_actual_fixture_definitions(
    tmp_path: Path, mode: str, option: str | None
) -> None:
    observations = []
    for engine in ("pytest", "vfy.independent.entry"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", _OBSERVER)
        project.write("d/test_case.py", _BODY)
        observations.append(_run(project, engine, _TARGETS[mode], option))
    assert observations[0] == observations[1]
    if option is not None and mode in {"directory", "file-selector"}:
        module_events = [
            event
            for event in observations[0]
            if isinstance(event, list) and event[0].startswith("module.")
        ]
        assert [event[0] for event in module_events] == [
            "module.setup",
            "module.setup",
            "module.teardown",
            "module.teardown",
        ]
        assert [event[2] for event in module_events] == [0, 1, 1, 0]


@pytest.mark.parametrize("option", ["--keepduplicates", "--keep-duplicates"])
def test_duplicate_file_workers_keep_global_occurrences_and_property_homes(
    tmp_path: Path, option: str
) -> None:
    snapshots = []
    for engine in ("pytest", "vfy.independent.entry"):
        project = VfyProject(tmp_path / engine)
        body = """import os
from pathlib import Path

def test_case(record_property):
    record_property('file', Path(__file__).name)
    with Path('workers.txt').open('a') as stream:
        stream.write(str(os.getpid()) + '\\n')
"""
        project.write("test_a.py", body)
        project.write("test_b.py", body)
        arguments = ["-m", engine, "-q", option, "--junitxml=result.xml"]
        if engine == "vfy.independent.entry":
            arguments.extend(["-j2", "--native-result-json=execution.json"])
        result = project.run_python(
            *arguments, "test_a.py", "test_b.py", "test_a.py", env={"PYTHONPATH": _SOURCE}
        )
        result.expect(0)
        root = ET.parse(project.root / "result.xml").getroot()
        snapshots.append(
            [
                (
                    case.attrib["name"],
                    [value.attrib for value in case.findall("properties/property")],
                )
                for case in root.findall(".//testcase")
            ]
        )
        if engine == "vfy.independent.entry":
            record = json.loads((project.root / "execution.json").read_text())
            assert record["collected"] == 3
            assert [case["test_index"] for case in record["cases"]] == [0, 1, 2]
            pids = {int(value) for value in (project.root / "workers.txt").read_text().splitlines()}
            assert len(pids) == 2
            assert record["execution_topology"]["active_workers"] == 2
            assert set(record["execution_topology"]["worker_pids"]) == pids
    assert snapshots[0] == snapshots[1]
    assert len(snapshots[0]) == 3
