"""Explicit parameter selectors retain literal IDs and later marker filtering."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = """\
import json
from pathlib import Path
import pytest

@pytest.fixture
def resource(request):
    def record(phase):
        with Path('events.jsonl').open('a') as stream:
            stream.write(json.dumps([
                phase, request.node.nodeid,
                [mark.name for mark in request.node.iter_markers()],
            ]) + '\\n')
    record('setup')
    yield
    record('teardown')

@pytest.mark.parametrize('value', [
    pytest.param(0, id='plain'),
    pytest.param(1, id='nested[part]'),
    pytest.param(2, id='rgb[38:2:1:2:3m'),
    pytest.param(3, id='one::two'),
    pytest.param(4, id='excluded[part]', marks=pytest.mark.stress),
    pytest.param(5, id='xfail[part]', marks=pytest.mark.xfail(reason='expected', strict=True)),
])
def test_case(value, resource):
    assert value != 5

def test_other():
    pass

def pytest_generate_tests(metafunc):
    with Path('generation.txt').open('a') as stream:
        stream.write(metafunc.definition.name + '\\n')
"""

_ALLOCATIONS = """\
from pathlib import Path
from _pytest.python import Function

original = Function.from_parent.__func__

def pytest_configure(config):
    def construct(cls, *args, **kwargs):
        if 'callspec' in kwargs:
            with Path('allocated.txt').open('a') as stream:
                stream.write(kwargs['name'] + '\\n')
        return original(cls, *args, **kwargs)
    Function.from_parent = classmethod(construct)

def pytest_unconfigure(config):
    Function.from_parent = classmethod(original)
"""


def test_literal_selected_parameters_keep_mark_filtering_and_teardown(tmp_path: Path) -> None:
    observations = {}
    allocations = {}
    selected_ids = ["nested[part]", "rgb[38:2:1:2:3m", "excluded[part]", "xfail[part]"]
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\naddopts = -m 'not stress'\nmarkers = stress\n")
        project.write("test_cases.py", _SOURCE)
        project.write("conftest.py", _ALLOCATIONS)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "--junitxml=junit.xml",
            "-q",
            *(f"test_cases.py::test_case[{parameter}]" for parameter in selected_ids),
        )
        result.expect(0)
        assert "2 passed, 1 deselected, 1 xfailed" in result.stdout
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
            cases,
        )
        allocations[engine] = (project.root / "allocated.txt").read_text().splitlines()
    assert observations["pytest-native"] == observations["pytest"]
    assert observations["pytest-native"][0] == "test_case\ntest_other\n"
    assert len(allocations["pytest"]) == 6
    assert allocations["pytest-native"] == allocations["pytest"]


def test_missing_literal_parameter_keeps_collection_diagnostic(tmp_path: Path) -> None:
    observations = {}
    history_sentinel = json.dumps(
        {
            "preserved": "missing-literal-parameter",
            "tests": {
                "test_cases.py::test_case[plain]": {"duration_ms": 123, "observations": []},
            },
        },
        indent=2,
        sort_keys=True,
    )
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\naddopts = -m 'not stress'\nmarkers = stress\n")
        project.write("test_cases.py", _SOURCE)
        history = project.write(".vfy/test-history.json", history_sentinel)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "test_cases.py::test_case[missing[part]]",
        )
        observations[engine] = (
            result.returncode,
            result.stderr[result.stderr.index("ERROR:") :].replace(str(project.root), "ROOT"),
            (project.root / "generation.txt").read_text(),
            (project.root / "events.jsonl").exists(),
        )
        assert history.read_bytes() == history_sentinel.encode(), result.stdout + result.stderr
    assert observations["pytest-native"] == observations["pytest"]
    status, diagnostic, generation, executed = observations["pytest"]
    assert status == 4
    assert "not found: ROOT/test_cases.py::test_case" in diagnostic
    assert generation == "test_case\ntest_other\n"
    assert not executed
