"""Callable dispatch preserves pytest items and live async metadata."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject


def _outcomes(project: VfyProject) -> list[tuple[str, str, tuple[str, ...]]]:
    return [
        (
            case.attrib["classname"],
            case.attrib["name"],
            tuple(child.tag for child in case if child.tag in {"failure", "error", "skipped"}),
        )
        for case in ET.parse(project.root / "junit.xml").iter("testcase")
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Unix")
@pytest.mark.parametrize("workers", [1, 2])
def test_function_method_and_hypothesis_dispatch_matches_pytest(
    tmp_path: Path, workers: int
) -> None:
    pytest.importorskip("hypothesis")
    source = """import asyncio
from pathlib import Path
import pytest
from hypothesis import given, settings, strategies as st

def record(name):
    Path(name + '.ran').touch()

@pytest.mark.parametrize('value', [0, 1, 2])
def test_sync(value):
    record(f'sync-{value}')

@pytest.mark.asyncio
@pytest.mark.parametrize('value', [0, 1, 2])
async def test_async(value):
    assert asyncio.get_running_loop().is_running()
    record(f'async-{value}')

class TestMethods:
    @pytest.mark.asyncio
    @pytest.mark.parametrize('value', [0, 1])
    async def test_bound(self, value):
        assert asyncio.get_running_loop().is_running()
        record(f'bound-{value}')

    @staticmethod
    @pytest.mark.asyncio
    async def test_static():
        assert asyncio.get_running_loop().is_running()
        record('static')

@settings(max_examples=3, derandomize=True)
@given(st.integers(min_value=0, max_value=2))
def test_hypothesis(value):
    assert 0 <= value <= 2
    record('hypothesis')

@pytest.mark.asyncio
async def test_async_generator():
    yield
"""
    observations = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope=function\n")
        project.write("test_case.py", source)
        result = (
            project.run_pytest(
                "-p", "pytest_asyncio.plugin", "-q", "--junitxml=junit.xml", load_xdist=False
            )
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                f"--workers={workers}",
                "--no-cov",
                "-q",
                "--junitxml=junit.xml",
            )
        )
        transcript = result.stdout + result.stderr
        assert result.returncode == 0, transcript
        assert "was never awaited" not in transcript
        observations.append(
            (_outcomes(project), sorted(path.name for path in project.root.glob("*.ran")))
        )
    assert observations[0] == observations[1]
    assert len(observations[0][0]) == 11
    assert len(observations[0][1]) == 10


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Unix")
@pytest.mark.parametrize("workers", [1, 2])
def test_fixture_changes_to_code_and_coroutine_marker_remain_live(
    tmp_path: Path, workers: int
) -> None:
    source = """import asyncio
import inspect
from pathlib import Path
import pytest

@pytest.fixture
def change_code(request, value):
    def sync(change_code, value):
        Path(f'code-{value}.ran').touch()
    async def coroutine(change_code, value):
        assert asyncio.get_running_loop().is_running()
        Path(f'code-{value}.ran').touch()
    replacement = coroutine if value else sync
    request.function.__code__ = replacement.__code__

@pytest.mark.asyncio
@pytest.mark.parametrize('value', [0, 1])
async def test_code(change_code, value):
    raise AssertionError('stale code executed')

@pytest.fixture
def change_marker(request):
    inspect.markcoroutinefunction(request.function)

@pytest.mark.asyncio
def test_marker(change_marker):
    async def inner():
        assert asyncio.get_running_loop().is_running()
        Path('marker.ran').touch()
    return inner()
"""
    observations = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope=function\n")
        project.write("test_case.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            f"--workers={workers if engine == 'pytest-native' else 1}",
            "--no-cov",
            "-q",
            "--junitxml=junit.xml",
        )
        transcript = result.stdout + result.stderr
        assert result.returncode == 0, transcript
        assert "was never awaited" not in transcript
        observations.append(
            (_outcomes(project), sorted(path.name for path in project.root.glob("*.ran")))
        )
    assert observations[0] == observations[1]
    assert observations[0][1] == ["code-0.ran", "code-1.ran", "marker.ran"]


def test_custom_items_and_adapter_subclasses_keep_their_collection_behavior(tmp_path: Path) -> None:
    conftest = """from pathlib import Path
import pytest

class CustomFunction(pytest.Function):
    @property
    def obj(self):
        Path('custom-descriptor.ran').touch()
        return super().obj

    @obj.setter
    def obj(self, function):
        self._obj = function

@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makeitem(collector, name, obj):
    if name == 'test_custom':
        return CustomFunction.from_parent(collector, name=name, callobj=obj)
"""
    source = """import asyncio
from pathlib import Path
import pytest

def test_plain():
    Path('plain.ran').touch()

@pytest.mark.asyncio
async def test_custom():
    assert asyncio.get_running_loop().is_running()
    Path('custom.ran').touch()
"""
    observations = []
    for engine in ("upstream", "vfy"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope=function\n")
        project.write("conftest.py", conftest)
        project.write("test_case.py", source)
        result = project.run_pytest(
            "-p",
            "pytest_asyncio.plugin" if engine == "upstream" else "vfy.asyncio_plugin",
            "-q",
            "--junitxml=junit.xml",
            load_xdist=False,
        )
        result.expect(0)
        observations.append(
            (
                _outcomes(project),
                sorted(path.name for path in project.root.glob("*.ran")),
            )
        )
    assert observations[0] == observations[1]
    assert observations[0][1] == [
        "custom-descriptor.ran",
        "custom.ran",
        "plain.ran",
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Unix")
@pytest.mark.parametrize("engine", ["pytest", "pytest-native"])
def test_fixture_replacement_of_inspect_classifier_remains_live(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope=function\n")
    project.write(
        "test_case.py",
        "import inspect\n"
        "from pathlib import Path\n"
        "import pytest\n\n"
        "@pytest.fixture\n"
        "def replace_classifier(monkeypatch, request):\n"
        "    original = inspect.iscoroutinefunction\n"
        "    target = request.function\n"
        "    def classify(function):\n"
        "        if function is target:\n"
        "            Path('live-inspection').touch()\n"
        "        return original(function)\n"
        "    monkeypatch.setattr(inspect, 'iscoroutinefunction', classify)\n\n"
        "@pytest.mark.asyncio\n"
        "async def test_live_classifier(replace_classifier):\n"
        "    assert Path('live-inspection').exists()\n",
    )
    result = project.run_vfy("test", f"--engine={engine}", "-j1", "--no-cov", "-q")
    result.expect(0)
    assert "1 passed" in result.stdout
