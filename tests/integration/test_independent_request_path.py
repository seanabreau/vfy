"""Fixture requests expose the live collected test path at supported scopes."""

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_request_path_follows_item_path_and_scope(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

events = []
items = []

def pytest_runtest_setup(item):
    items.append(item)

def observe(request):
    item = items[-1]
    original = item.path
    replacement = original.with_name('replacement.py')
    before = request.path is original
    try:
        item.path = replacement
        after = request.path is replacement
    finally:
        item.path = original
    events.append([request.scope, before, after, original.name])

@pytest.fixture(scope='session')
def session_path(request):
    with pytest.raises(AttributeError, match='^path not available in session-scoped context$'):
        request.path
    events.append(['session', 'unavailable'])

@pytest.fixture(scope='package')
def package_path(request):
    observe(request)

@pytest.fixture(scope='module')
def module_path(request):
    observe(request)

@pytest.fixture(scope='class')
def class_path(request):
    observe(request)

@pytest.fixture
def function_path(request):
    observe(request)

def pytest_sessionfinish():
    Path('paths.json').write_text(json.dumps(events))
""",
    )
    project.write("pkg/__init__.py", "")
    project.write(
        "pkg/test_path.py",
        """class TestPath:
    def test_path(
        self, session_path, package_path, module_path, class_path, function_path, request
    ):
        assert request.path is request.node.path
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "paths.json").read_text())
    assert expected == [
        ["session", "unavailable"],
        ["package", True, True, "test_path.py"],
        ["module", True, True, "test_path.py"],
        ["class", True, True, "test_path.py"],
        ["function", True, True, "test_path.py"],
    ]
    (tmp_path / "paths.json").unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    candidate.expect(0)
    assert json.loads((tmp_path / "paths.json").read_text()) == expected
