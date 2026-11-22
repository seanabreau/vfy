"""Explicit native parameter selection preserves pytest generation and item contracts."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest
from tests.support.profiles import profile_call_count

pytestmark = requires_native_pytest

_SOURCE = """\
from pathlib import Path
import pytest
from src.sample import measured

@pytest.fixture
def resource(request):
    with Path('events.txt').open('a') as stream:
        stream.write('setup:' + request.node.nodeid + '\\n')
    yield
    with Path('events.txt').open('a') as stream:
        stream.write('teardown:' + request.node.nodeid + '\\n')

@pytest.mark.parametrize('value', [
    pytest.param(0, id='dup'),
    pytest.param(1, id='dup', marks=pytest.mark.xfail(reason='expected', strict=True)),
    pytest.param(2, id='dup0', marks=pytest.mark.skip(reason='expected')),
    pytest.param(3),
], ids=[None, None, None, 'π'])
def test_case(value, resource):
    assert measured(value) != 1

def test_other(value):
    assert value == 99
"""

_GENERATION = """\
from pathlib import Path

def pytest_generate_tests(metafunc):
    with Path('generation.txt').open('a') as stream:
        stream.write(metafunc.definition.name + '\\n')
    if metafunc.definition.name == 'test_other':
        metafunc.parametrize('value', [99], ids=['other'])
"""


@pytest.mark.parametrize(
    "arguments",
    [
        ("test_cases.py::test_case[dup1]",),
        ("test_cases.py::test_case[dup2]", "test_cases.py::test_case[dup0]"),
        ("test_cases.py::test_case[π]", "test_cases.py::test_case[dup1]"),
        ("test_cases.py::test_case", "test_cases.py::test_case[dup1]"),
        ("test_cases.py", "test_cases.py::test_case[dup1]"),
        ("test_cases.py::test_case[dup1]", "test_cases.py::test_case[dup1]"),
        ("--keep-duplicates", "test_cases.py::test_case[dup1]", "test_cases.py::test_case[dup1]"),
    ],
)
def test_explicit_parameters_preserve_ids_marks_generation_and_coverage(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "pytest.ini",
            "[pytest]\n"
            "disable_test_id_escaping_and_forfeit_all_rights_to_community_support = True\n",
        )
        project.write("src/__init__.py", "")
        project.write("src/sample.py", "def measured(value):\n    return value\n")
        project.write("test_cases.py", _SOURCE + "\n" + _GENERATION)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--source=src",
            "--cov-branch",
            "--cov-report=json:coverage.json",
            "--junitxml=junit.xml",
            "-q",
            *arguments,
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        report = json.loads(
            (project.root / "coverage.json").read_text().replace(str(project.root), "ROOT")
        )
        report["meta"].pop("timestamp")
        outcomes = [
            (
                case.get("classname"),
                case.get("name"),
                [(child.tag, child.get("type"), child.get("message")) for child in case],
            )
            for case in ET.parse(project.root / "junit.xml").iter("testcase")
        ]
        observations[engine] = (
            (project.root / "generation.txt").read_text(),
            (project.root / "events.txt").read_text()
            if (project.root / "events.txt").exists()
            else "",
            outcomes,
            report,
        )
    assert observations["pytest-native"] == observations["pytest"]
    assert "test_other\n" in observations["pytest-native"][0]


@pytest.mark.parametrize("failure", ["generation", "missing-case", "missing-function"])
def test_parameter_selection_preserves_collection_errors(tmp_path: Path, failure: str) -> None:
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_cases.py",
            "import pytest\nfrom pathlib import Path\n"
            "@pytest.mark.parametrize('value', [0, 1])\n"
            "def test_case(value):\n    Path('executed.txt').write_text(str(value))\n"
            "def test_other(value):\n    pass\n"
            "def pytest_generate_tests(metafunc):\n"
            "    if metafunc.definition.name == 'test_other':\n"
            + (
                "        raise ValueError('unselected generation failed')\n"
                if failure == "generation"
                else "        metafunc.parametrize('value', [99])\n"
            ),
        )
        target = {
            "generation": "test_cases.py::test_case[0]",
            "missing-case": "test_cases.py::test_case[missing]",
            "missing-function": "test_cases.py::test_missing",
        }[failure]
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "-q", target
        )
        diagnostic = "unselected generation failed" if failure == "generation" else "not found: "
        observations[engine] = (
            result.returncode,
            diagnostic in result.stdout + result.stderr,
            (project.root / "executed.txt").exists(),
        )
    assert observations["pytest"] == (4, True, False)
    assert observations["pytest-native"] == observations["pytest"]


@pytest.mark.parametrize("installation", ["collect-file", "configure-module", "configure-base"])
@pytest.mark.parametrize(
    "arguments",
    [(), ("test_cases.py::test_case[1]",)],
    ids=["all", "selected"],
)
def test_collection_report_observer_receives_complete_parameter_family(
    tmp_path: Path, installation: str, arguments: tuple[str, ...]
) -> None:
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_cases.py", PARAMETER_SOURCE)
        observer = (
            "from pathlib import Path\nimport pytest\n"
            "@pytest.hookimpl(wrapper=True)\n"
            "def pytest_collect_file(file_path, parent):\n"
            "    collectors = yield\n"
            "    for collector in collectors:\n"
            "        if collector.name == 'test_cases.py':\n"
            "            original = collector.collect\n"
            "            def collect(original=original):\n"
            "                items = list(original())\n"
            "                Path('children.txt').write_text(\n"
            "                    '\\n'.join(item.nodeid for item in items))\n"
            "                return items\n"
            "            collector.collect = collect\n"
            "    return collectors\n"
        )
        if installation.startswith("configure-"):
            collector_type = "Module" if installation == "configure-module" else "PyCollector"
            observer = (
                "from pathlib import Path\n"
                f"from _pytest.python import {collector_type} as Collector\n"
                "original = Collector.collect\n"
                "def pytest_configure(config):\n"
                "    def collect(self):\n"
                "        items = list(original(self))\n"
                "        if self.name == 'test_cases.py':\n"
                "            Path('children.txt').write_text(\n"
                "                '\\n'.join(item.nodeid for item in items))\n"
                "        return items\n"
                "    Collector.collect = collect\n"
                "def pytest_unconfigure(config):\n"
                "    Collector.collect = original\n"
            )
        project.write("conftest.py", observer)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            *arguments,
        )
        result.expect(0)
        observations[engine] = (project.root / "children.txt").read_text()
    assert (
        observations["pytest-native"]
        == observations["pytest"]
        == "\n".join(f"test_cases.py::test_case[{index}]" for index in range(3))
    )


PARAMETER_SOURCE = """\
import pytest
@pytest.mark.parametrize('value', range(3))
def test_case(value):
    assert value in range(3)
"""


@pytest.mark.parametrize("selected_count", [1, 10])
def test_explicit_parameter_allocation_is_bounded_by_selected_cases(
    tmp_path: Path, selected_count: int
) -> None:
    allocated = {}
    observations = {}
    constructor = pytest.Function.from_parent.__func__.__code__
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("src/__init__.py", "")
        project.write("src/sample.py", "def measured(value):\n    return value\n")
        project.write(
            "test_cases.py",
            "import pytest\nfrom src.sample import measured\n"
            "@pytest.mark.parametrize('value', range(64))\n"
            "def test_case(value):\n    assert measured(value) == value\n",
        )
        profile_directory = project.root / "profile"
        profile_directory.mkdir()
        targets = [f"test_cases.py::test_case[{index}]" for index in range(selected_count)]
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--source=src",
            "--cov-report=json:coverage.json",
            "--junitxml=junit.xml",
            "-q",
            *targets,
            env={"VFY_PROFILE_DIR": str(profile_directory)},
        )
        result.expect(0)
        assert f"{selected_count} passed" in result.stdout
        assert project.lines("src/sample.py") == [1, 2]
        assert (project.root / "junit.xml").exists()
        cases = [
            (
                case.get("classname"),
                case.get("name"),
                [(child.tag, child.get("type"), child.get("message")) for child in case],
            )
            for case in ET.parse(project.root / "junit.xml").iter("testcase")
        ]
        report = json.loads(
            (project.root / "coverage.json").read_text().replace(str(project.root), "ROOT")
        )
        report["meta"].pop("timestamp")
        observations[engine] = (cases, report)
        # The lone test definition shares Function.from_parent with its parameter items.
        allocated[engine] = (
            profile_call_count(profile_directory / "controller.pstats", constructor) - 1
        )
    assert observations["pytest-native"] == observations["pytest"]
    assert allocated["pytest"] == 64
    expected = 64 if tuple(map(int, pytest.version_tuple[:2])) >= (9, 2) else selected_count
    assert allocated["pytest-native"] == expected


def test_selected_parameter_preserves_failed_collector_cache_transition(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_cases.py", PARAMETER_SOURCE)
        cache = project.write(
            ".pytest_cache/v/cache/lastfailed", json.dumps({"test_cases.py": True})
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "test_cases.py::test_case[1]",
        )
        result.expect(0)
        observations[engine] = json.loads(cache.read_text())
    assert observations["pytest-native"] == observations["pytest"]


@pytest.mark.parametrize(
    ("ids", "selected"),
    [(["plain", "second"], "plain"), (["a[b]", "a::b"], "a::b")],
)
def test_inherited_parameter_method_preserves_class_and_unusual_ids(
    tmp_path: Path, ids: list[str], selected: str
) -> None:
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_cases.py",
            "from pathlib import Path\nimport pytest\n"
            "class Base:\n"
            f"    @pytest.mark.parametrize('value', range(2), ids={ids!r})\n"
            "    def test_case(self, value, request):\n"
            "        Path('executed.txt').write_text(request.node.nodeid)\n"
            "class TestInherited(Base):\n    pass\n",
        )
        target = f"test_cases.py::TestInherited::test_case[{selected}]"
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "-q", target
        )
        result.expect(0)
        observations[engine] = (project.root / "executed.txt").read_text()
    assert observations["pytest-native"] == observations["pytest"] == target
