"""Public stash protocols retain arbitrary plugin state without runner policy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("failure", [False, True])
def test_config_and_node_stashes_hold_plugin_state_through_cleanup(
    tmp_path: Path, failure: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path
config_key = pytest.StashKey[object]()
node_key = pytest.StashKey[list]()
class State:
    def __init__(self):
        self.token = object()
        self.items = []
        self.events = []
def pytest_configure(config):
    assert config_key not in config.stash
    state = State()
    config.stash[config_key] = state
    def cleanup():
        assert config.stash[config_key] is state
        state.events.append('cleanup')
        Path('events.json').write_text(json.dumps(state.events))
    config.add_cleanup(cleanup)
def pytest_itemcollected(item):
    assert node_key not in item.stash
    assert item.stash is not item.config.stash
    assert item.parent is None or item.parent.stash is not item.stash
    item.stash[node_key] = ['collected']
    item.config.stash[config_key].items.append(item)
@pytest.fixture
def resource(request):
    request.node.stash[node_key].append('fixture setup')
    yield request.config.stash[config_key].token
    request.node.stash[node_key].append('fixture cleanup')
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    item.stash[node_key].append('call before')
    yield
    item.stash[node_key].append('call after')
def pytest_sessionfinish(session):
    state = session.config.stash[config_key]
    assert session.stash is not session.config.stash
    assert config_key not in session.stash
    assert len({id(item.stash) for item in state.items}) == len(state.items)
    state.events.append([[item.name, item.stash[node_key]] for item in state.items])
def pytest_unconfigure(config):
    config.stash[config_key].events.append('unconfigure')
""",
    )
    project.write(
        "test_state.py",
        f"""import pytest
from conftest import config_key, node_key
@pytest.mark.parametrize('value', [1, 2])
def test_body(resource, request, value):
    assert resource is request.config.stash[config_key].token
    assert request.node.stash[node_key] == ['collected', 'fixture setup', 'call before']
    request.node.stash[node_key].append('body')
    assert not ({failure!r} and value == 1), 'deliberate stash lifecycle failure'
""",
    )
    expected = None
    for route in ("stock", "owned"):
        (tmp_path / "events.json").unlink(missing_ok=True)
        result = (
            project.run_python("-m", "pytest", "-q")
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "--no-header")
        )
        assert result.returncode == int(failure), result.stdout + result.stderr
        actual = json.loads((tmp_path / "events.json").read_text())
        if route == "stock":
            expected = actual
        else:
            assert actual == expected


def test_installed_timeout_import_uses_public_stash_and_runs_like_pytest(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_body.py",
        """from pathlib import Path
import pytest, pytest_timeout

def test_body():
    stash = pytest.Stash()
    assert isinstance(pytest_timeout.SESSION_TIMEOUT_KEY, pytest.StashKey)
    assert isinstance(pytest_timeout.SESSION_EXPIRE_KEY, pytest.StashKey)
    stash[pytest_timeout.SESSION_TIMEOUT_KEY] = 3.0
    stash[pytest_timeout.SESSION_EXPIRE_KEY] = 9.0
    assert stash[pytest_timeout.SESSION_TIMEOUT_KEY] == 3.0
    assert stash[pytest_timeout.SESSION_EXPIRE_KEY] == 9.0
    Path('body.txt').write_text('ran')
""",
    )
    stock = project.run_python("-m", "pytest", "-q", "-p", "timeout")
    stock.expect(0)
    (tmp_path / "body.txt").unlink()
    # pytest-timeout's debugger hooks are admitted: only --pdb calls them, and vfy has no --pdb.
    actual = project.run_vfy("test", "--engine=native", "--no-cov", "-p", "timeout")
    assert actual.returncode == stock.returncode, actual.stdout + actual.stderr
    assert (tmp_path / "body.txt").read_text() == "ran"
