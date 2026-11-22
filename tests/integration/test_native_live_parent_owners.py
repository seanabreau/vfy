"""Live parent replacement preserves module fixture and finalizer ownership."""

from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = """\
from pathlib import Path
import pytest

owners = []
future = None
replacement = None

def record(event):
    with Path('events.txt').open('a', encoding='utf-8') as stream:
        stream.write(event + '\\n')

@pytest.fixture(scope='module')
def resource(request):
    owner = request.node
    for previous in owners:
        assert previous is not owner
        with pytest.raises(AssertionError):
            previous.addfinalizer(lambda: record('inactive finalizer'))
    owners.append(owner)
    label = owner.owner_label
    def node_finalizer():
        assert request.node is owner
        record('node finalizer ' + label)
    owner.addfinalizer(node_finalizer)
    record('setup ' + label)
    yield label
    assert request.node is owner
    record('teardown ' + label)

def test_reparent(request):
    global future, replacement
    original = request.node.parent
    original.owner_label = 'original'
    first = pytest.Module.from_parent(original.parent, path=original.path)
    first.owner_label = 'first'
    replacement = pytest.Module.from_parent(original.parent, path=original.path)
    replacement.owner_label = 'replacement'
    assert first is not replacement and first.nodeid == replacement.nodeid == original.nodeid
    items = {item.name: item for item in request.session.items}
    items['test_first'].parent = first
    items['test_same_parent'].parent = first
    future = items['test_replacement']
    record('parents prepared')

def test_first(resource):
    assert resource == 'first'
    record('call first')

def test_same_parent(resource):
    assert resource == 'first'
    future.parent = replacement
    record('call same parent')

def test_replacement(resource):
    assert resource == 'replacement'
    record('call replacement')

def test_original(resource):
    assert resource == 'original'
    record('call original')
"""


def test_live_parent_identity_controls_module_fixture_teardown(tmp_path: Path) -> None:
    expected = [
        "parents prepared",
        "setup first",
        "call first",
        "call same parent",
        "teardown first",
        "node finalizer first",
        "setup replacement",
        "call replacement",
        "teardown replacement",
        "node finalizer replacement",
        "setup original",
        "call original",
        "teardown original",
        "node finalizer original",
    ]
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", _SOURCE)
        environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = (
            project.run_python("-m", "pytest", "-q", env=environment)
            if engine == "pytest"
            # The proof reparents installed-pytest Module objects at runtime.
            else project.run_vfy(
                "test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q", env=environment
            )
        )
        transcript = result.stdout + result.stderr
        assert result.returncode == 0, transcript
        assert "5 passed" in result.stdout, transcript
        observations[engine] = (project.root / "events.txt").read_text().splitlines()
    assert observations["pytest-native"] == observations["pytest"] == expected
