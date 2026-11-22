"""Compare cached fixture requests and parameter-comparison reentry with pytest."""

from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_cached_class_fixture_ignores_replaced_callable_until_teardown(tmp_path: Path) -> None:
    source = """from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

class ReplacedCallable:
    def __getattribute__(self, name):
        raise AssertionError('cached fixture inspected replacement: ' + name)

    def __call__(self, *args, **kwargs):
        raise AssertionError('cached fixture invoked replacement')

class TestCases:
    @pytest.fixture(scope='class')
    def shared(self, request):
        record('setup')
        yield 42
        record('teardown')

    def test_first(self, shared, request):
        assert shared == 42
        definition = request.node._fixtureinfo.name2fixturedefs['shared'][-1]
        definition.func = ReplacedCallable()
        record('first')

    def test_second(self, shared):
        assert shared == 42
        record('second')
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        assert "2 passed" in result.stdout
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert (
        observed["pytest-native"] == observed["pytest"] == ["setup", "first", "second", "teardown"]
    )


def test_cached_fixture_requests_preserve_comparison_and_owner_lifecycle(tmp_path: Path) -> None:
    source = """from pathlib import Path
import pytest

saved_request = None
current_dependency = None

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

class Parameter:
    def __init__(self, value, label):
        self.value = value
        self.label = label

    def __eq__(self, other):
        if saved_request is not None:
            assert saved_request.getfixturevalue('dependency') is current_dependency
            record('compare ' + self.label + ' ' + other.label)
        return self.value == other.value

@pytest.fixture(scope='module')
def dependency():
    global current_dependency
    current_dependency = object()
    record('dependency setup')
    yield current_dependency
    record('dependency teardown')

@pytest.fixture(scope='module', params=[
    Parameter(1, 'first'), Parameter(1, 'equal'), Parameter(2, 'changed')
], ids=['first', 'equal', 'changed'])
def shared(request, dependency):
    global saved_request
    saved_request = request
    parameter = request.param
    record('shared setup ' + parameter.label)
    yield parameter
    assert request.getfixturevalue('dependency') is dependency
    assert request.param is parameter
    record('shared teardown ' + parameter.label)

class TestCases:
    @pytest.fixture
    def value(self, request, shared):
        assert not hasattr(self, 'seen')
        self.seen = shared
        request.node.addfinalizer(lambda: record('node finalizer ' + request.node.name))
        yield shared
        assert self.checked
        assert request.getfixturevalue('shared') is shared
        record('value teardown ' + request.node.name)

    @pytest.mark.parametrize('case', [0, 1])
    def test_case(self, value, case, request):
        assert self.seen is value
        assert saved_request.param is value
        assert request.getfixturevalue('value') is value
        self.checked = True
        record('call ' + request.node.name)
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        assert "6 passed" in result.stdout
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert observed["pytest-native"] == observed["pytest"]
    assert [event for event in observed["pytest-native"] if event.startswith("shared setup ")] == [
        "shared setup first",
        "shared setup changed",
    ]
