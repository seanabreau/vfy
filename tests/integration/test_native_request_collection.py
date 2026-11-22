"""Request-visible selected inventories preserve ordinary pytest item behavior."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from _pytest.python import Function

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest
from tests.support.profiles import profile_call_count

pytestmark = requires_native_pytest


def _outcomes(project: VfyProject) -> list[tuple[str | None, list[tuple[str, str | None]]]]:
    return [
        (
            item.get("name"),
            [
                (child.tag, child.get("message"))
                for child in item
                if child.tag in {"failure", "error", "skipped"}
            ],
        )
        for item in ET.parse(project.root / "junit.xml").iter("testcase")
    ]


@pytest.mark.parametrize("discarded_count", [64, 1024])
def test_marker_deselection_bounds_request_items_and_preserves_future_mutation(
    tmp_path: Path, discarded_count: int
) -> None:
    source = """import json
from pathlib import Path
import pytest
from _pytest.python import Function

def emit(value):
    with Path('events.jsonl').open('a') as stream:
        stream.write(json.dumps(value) + '\\n')

@pytest.fixture(autouse=True)
def observe(request):
    if request.node.name != 'test_first':
        return request.session.items
    items = request.session.items
    assert len(items) == 4
    assert items[0] is request.node
    assert all(type(item) is Function and item.session is request.session for item in items)
    assert len({id(item) for item in items}) == len(items)
    assert all(item._request.node is item for item in items)
    emit(['inventory', [item.nodeid for item in items],
          [dict(item.callspec.params) if hasattr(item, 'callspec') else {} for item in items]])
    for item in items[1:]:
        item.get_closest_marker('payload').kwargs['live'] = 'changed'
    items[2].add_marker(pytest.mark.skip(reason='future case skip'))
    def replacement(value, observe):
        emit(['replacement', value])
    items[1].obj = replacement
    return items

def test_first(observe):
    emit(['first'])

@pytest.mark.payload(live='original')
@pytest.mark.parametrize('value', [1, 0, 2])
def test_future(value, observe):
    assert all(item.get_closest_marker('payload').kwargs['live'] == 'changed'
               for item in observe[1:])
    emit(['original', value])

@pytest.mark.discarded
@pytest.mark.parametrize('value', range(DISCARDED_COUNT))
def test_discarded(value, observe):
    raise AssertionError('deselected body ran')
""".replace("DISCARDED_COUNT", str(discarded_count))
    observations: dict[str, object] = {}
    constructor_calls: dict[str, int] = {}
    code = Function.from_parent.__func__.__code__
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nmarkers =\n payload\n discarded\n")
        project.write("test_cases.py", source)
        profile = project.root / "profile"
        profile.mkdir()
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "--junitxml=junit.xml",
            "-m",
            "not discarded",
            "-q",
            env={"VFY_PROFILE_DIR": str(profile)},
        )
        result.expect(0)
        observations[engine] = (
            [json.loads(line) for line in (project.root / "events.jsonl").read_text().splitlines()],
            _outcomes(project),
        )
        # Profiling observes the canonical constructor without replacing it or
        # installing a collection hook that requires ordinary parameter items.
        constructor_calls[engine] = profile_call_count(profile / "controller.pstats", code)
    assert (
        observations["pytest-native"]
        == observations["pytest"]
        == (
            [
                [
                    "inventory",
                    [
                        "test_cases.py::test_first",
                        "test_cases.py::test_future[1]",
                        "test_cases.py::test_future[0]",
                        "test_cases.py::test_future[2]",
                    ],
                    [{}, {"value": 1}, {"value": 0}, {"value": 2}],
                ],
                ["first"],
                ["replacement", 1],
                ["original", 2],
            ],
            [
                ("test_first", []),
                ("test_future[1]", []),
                ("test_future[0]", [("skipped", "future case skip")]),
                ("test_future[2]", []),
            ],
        )
    )
    assert constructor_calls["pytest"] >= discarded_count
    if tuple(map(int, pytest.version_tuple[:2])) >= (9, 2):
        assert constructor_calls["pytest-native"] >= discarded_count
    else:
        assert constructor_calls["pytest-native"] <= 16


@pytest.mark.skipif(
    tuple(map(int, pytest.version_tuple[:2])) < (8, 3),
    reason="marker keyword expressions require pytest 8.3",
)
def test_marker_comparison_observes_real_request_items_before_selection(tmp_path: Path) -> None:
    source = """import inspect, json
from pathlib import Path
import pytest
from _pytest.python import Function

class LiveValue:
    def __eq__(self, other):
        frame = inspect.currentframe()
        while frame:
            items = frame.f_locals.get('items')
            if type(items) is list and items and all(isinstance(item, Function) for item in items):
                break
            frame = frame.f_back
        assert frame is not None
        assert len(items) == 3
        assert all(type(item) is Function and item._request.node is item for item in items)
        with Path('comparison.jsonl').open('a') as stream:
            stream.write(json.dumps([item.nodeid for item in items]) + '\\n')
        return other == 1

@pytest.fixture(autouse=True)
def observe(request):
    assert len(request.session.items) == 3

@pytest.mark.payload(value=LiveValue())
@pytest.mark.parametrize('value', [1, 0, 2])
def test_case(value):
    assert value in (1, 0, 2)
"""
    observations: dict[str, object] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nmarkers = payload\n")
        project.write("test_cases.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "--junitxml=junit.xml",
            "-m",
            "payload(value=1)",
            "-q",
        )
        result.expect(0)
        observations[engine] = (
            [
                json.loads(line)
                for line in (project.root / "comparison.jsonl").read_text().splitlines()
            ],
            _outcomes(project),
        )
    assert (
        observations["pytest-native"]
        == observations["pytest"]
        == (
            [[f"test_cases.py::test_case[{value}]" for value in (1, 0, 2)]] * 3,
            [(f"test_case[{value}]", []) for value in (1, 0, 2)],
        )
    )


@pytest.mark.parametrize("session_access", ["generation", "callable-id"])
def test_id_callbacks_preserve_retained_session_for_later_observers(
    tmp_path: Path, session_access: str
) -> None:
    shared = """import json, threading
from pathlib import Path
session = None
started = threading.Event()
finished = threading.Event()
thread = None

def inventory():
    return [[item.nodeid, dict(item.callspec.params) if hasattr(item, 'callspec') else None]
            for item in session.items]

def background():
    assert started.wait(10)
    Path('background.json').write_text(json.dumps(inventory()))
    finished.set()
"""
    first = """import inspect, threading, pytest
import shared

SESSION_CAPTURE

def ids(value):
    ID_CAPTURE
    assert shared.session is not None
    shared.thread = threading.Thread(target=shared.background)
    shared.thread.start()
    return 'first'

@pytest.mark.parametrize('value', [1], ids=ids)
def test_first(value):
    pass
"""
    if session_access == "generation":
        first = first.replace(
            "SESSION_CAPTURE",
            "def pytest_generate_tests(metafunc):\n"
            "    shared.session = metafunc.definition.session",
        ).replace("ID_CAPTURE", "pass")
    else:
        first = first.replace("SESSION_CAPTURE", "").replace(
            "ID_CAPTURE",
            "frame = inspect.currentframe()\n"
            "    while frame:\n"
            "        metafunc = frame.f_locals.get('metafunc')\n"
            "        if isinstance(metafunc, pytest.Metafunc):\n"
            "            shared.session = metafunc.definition.session\n"
            "            break\n"
            "        frame = frame.f_back",
        )
    future = """import pytest
@pytest.mark.parametrize('value', [1, 0, 2])
def test_future(value):
    assert value in (1, 0, 2)
"""
    later = """import json
from pathlib import Path
import pytest
import shared
from _pytest.python import Function

shared.started.set()
assert shared.finished.wait(10)
shared.thread.join()
Path('import.json').write_text(json.dumps(shared.inventory()))

@pytest.fixture
def observer_without_request():
    items = shared.session.items
    assert len(items) == 5
    assert all(type(item) is Function for item in items)
    assert items[-1]._request.node is items[-1]
    Path('fixture.json').write_text(json.dumps(shared.inventory()))

def later_ids(value):
    Path('later-id.json').write_text(json.dumps(shared.inventory()))
    return 'last'

@pytest.mark.parametrize('value', [7], ids=later_ids)
def test_observe(value, observer_without_request):
    assert value == 7
"""
    observations: dict[str, object] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        for name, source in (
            ("shared.py", shared),
            ("test_a.py", first),
            ("test_b.py", future),
            ("test_c.py", later),
        ):
            project.write(name, source)
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "--junitxml=junit.xml", "-q"
        )
        result.expect(0)
        observations[engine] = (
            {
                name: json.loads((project.root / name).read_text())
                for name in ("background.json", "import.json", "later-id.json", "fixture.json")
            },
            _outcomes(project),
        )
    before_later = [
        ["test_a.py::test_first[first]", {"value": 1}],
        *[[f"test_b.py::test_future[{value}]", {"value": value}] for value in (1, 0, 2)],
    ]
    assert (
        observations["pytest-native"]
        == observations["pytest"]
        == (
            {
                "background.json": before_later,
                "import.json": before_later,
                "later-id.json": before_later,
                "fixture.json": [*before_later, ["test_c.py::test_observe[last]", {"value": 7}]],
            },
            [
                ("test_first[first]", []),
                *[(f"test_future[{value}]", []) for value in (1, 0, 2)],
                ("test_observe[last]", []),
            ],
        )
    )
