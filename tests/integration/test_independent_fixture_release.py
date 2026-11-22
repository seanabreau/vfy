"""Finished function fixtures release values before the following test."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("style", ["return", "yield", "cycle"])
def test_finished_fixture_values_release_before_the_next_test(tmp_path: Path, style: str) -> None:
    project = VfyProject(tmp_path)
    finish = "yield value" if style == "yield" else "return value"
    cycle = "value.cycle = value" if style == "cycle" else ""
    project.write(
        "test_release.py",
        f"""import gc
from pathlib import Path
import weakref
import pytest

references = []
def event(text):
    with Path('events').open('a') as output:
        output.write(text + '\\n')

class Resource:
    def __del__(self):
        assert 'resource' in self.request.fixturenames
        try:
            self.request.getfixturevalue('resource')
        except AssertionError:
            event('released after teardown')
        else:
            event('released before teardown')

@pytest.fixture
def resource(request):
    value = Resource()
    value.request = request
    {cycle}
    references.append(weakref.ref(value))
    event('setup')
    {finish}

def test_first(resource):
    assert references[0]() is resource
    event('first')

def test_second():
    gc.collect()
    assert references[0]() is None, 'finished fixture value retained'
    event('second')
""",
    )
    oracle = project.run_python("-m", "pytest", "-q")
    oracle_events = (project.root / "events").read_text()
    (project.root / "events").unlink()
    native = project.run_python(
        "-m",
        "vfy.independent.entry",
        env={"PYTHONPATH": isolated_vfy_path()},
    )
    assert oracle.returncode == native.returncode == 0, (
        oracle.stdout + oracle.stderr + native.stdout + native.stderr
    )
    assert (project.root / "events").read_text() == oracle_events
    assert oracle_events == "setup\nfirst\nreleased after teardown\nsecond\n"


def test_node_finalizer_aliases_lifetime_and_live_parent_owners(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

events = []
saved = {}
references = []

def attempt(label, method):
    try:
        method(lambda: events.append('unexpected ' + label))
    except BaseException as error:
        events.append([label, type(error).__name__])
    else:
        events.append([label, 'accepted'])

def pytest_collection_modifyitems(items):
    item = next(item for item in items if item.name == 'test_first')
    def override(callback):
        events.append('instance override')
        return type(item).addfinalizer(item, callback)
    item.addfinalizer = override

def pytest_sessionfinish():
    Path('node-finalizers.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_owners.py",
        """import weakref
from conftest import events, saved, references, attempt

class Finalizer:
    def __call__(self):
        events.append('callback')
    def __del__(self):
        events.append('callback released')

class TestOwners:
    def test_first(self, request):
        item = request.node
        saved['item'] = item.addfinalizer
        saved['class'] = item.parent.addfinalizer
        saved['module'] = item.parent.parent.addfinalizer
        saved['session'] = request.session.addfinalizer
        callback = Finalizer()
        references.append(weakref.ref(callback))
        saved['item'](callback)
        def finalizer():
            events.append('item finalizer')
            attempt('own during finalizer', saved['item'])
            for scope in ('class', 'module', 'session'):
                saved[scope](lambda scope=scope: events.append(scope + ' finalizer'))
        saved['item'](finalizer)
        events.append('first')

    def test_second(self):
        events.append(['callback released before second', references[0]() is None])
        attempt('item after teardown', saved['item'])
        saved['class'](lambda: events.append('class from second'))
        events.append('second')

def test_after_class():
    attempt('class after teardown', saved['class'])
    saved['module'](lambda: events.append('module from later'))
    saved['session'](lambda: events.append('session from later'))
    events.append('after class')
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((project.root / "node-finalizers.json").read_text())
    assert ["own during finalizer", "AssertionError"] in expected
    assert ["item after teardown", "AssertionError"] in expected
    assert ["class after teardown", "AssertionError"] in expected
    assert ["callback released before second", True] in expected
    assert "instance override" in expected
    assert not any(isinstance(event, str) and event.startswith("unexpected") for event in expected)
    (project.root / "node-finalizers.json").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((project.root / "node-finalizers.json").read_text()) == expected


def test_custom_item_addfinalizer_override_is_respected(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

events = []
class CaseItem(pytest.Item):
    def addfinalizer(self, callback):
        events.append('subclass override')
        return super().addfinalizer(callback)
    def runtest(self):
        self.addfinalizer(lambda: events.append('custom finalizer'))
        events.append('custom test')

class CaseFile(pytest.File):
    def collect(self):
        yield CaseItem.from_parent(self, name='custom')

def pytest_collect_file(file_path, parent):
    if file_path.suffix == '.nodefinalizer':
        return CaseFile.from_parent(parent, path=file_path)

def pytest_sessionfinish():
    Path('node-finalizers.json').write_text(json.dumps(events))
""",
    )
    project.write("test_custom.nodefinalizer", "")
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((project.root / "node-finalizers.json").read_text())
    assert expected == ["subclass override", "custom test", "custom finalizer"]
    (project.root / "node-finalizers.json").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((project.root / "node-finalizers.json").read_text()) == expected


def test_node_addfinalizer_can_register_from_another_thread(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

events = []
def pytest_sessionfinish():
    Path('node-finalizers.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_thread.py",
        """import threading
from conftest import events

def test_thread_registration(request):
    def register():
        try:
            request.node.addfinalizer(lambda: events.append('finalizer'))
        except BaseException as error:
            events.append(type(error).__name__)
        else:
            events.append('registered')
    thread = threading.Thread(target=register)
    thread.start()
    thread.join()
    events.append('body')
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((project.root / "node-finalizers.json").read_text())
    assert expected == ["registered", "body", "finalizer"]
    (project.root / "node-finalizers.json").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((project.root / "node-finalizers.json").read_text()) == expected


def test_fixture_teardown_uses_live_node_addfinalizer_override(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

events = []
@pytest.fixture
def resource():
    events.append('fixture setup')
    yield
    events.append('fixture teardown')

def pytest_collection_modifyitems(items):
    item = items[0]
    def override(callback):
        events.append('node addfinalizer override')
        return type(item).addfinalizer(item, callback)
    item.addfinalizer = override

def pytest_sessionfinish():
    Path('automatic-finalizers.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_fixture.py",
        """from conftest import events

def test_fixture(resource):
    events.append('test body')
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((project.root / "automatic-finalizers.json").read_text())
    assert expected == [
        "fixture setup",
        "node addfinalizer override",
        "test body",
        "fixture teardown",
    ]
    (project.root / "automatic-finalizers.json").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((project.root / "automatic-finalizers.json").read_text()) == expected


def test_top_request_finalizer_uses_live_node_and_rejects_late_registration(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

events = []
saved_requests = []
def pytest_collection_modifyitems(items):
    item = items[0]
    def override(callback):
        events.append('node override')
        return type(item).addfinalizer(item, callback)
    item.addfinalizer = override

def pytest_sessionfinish():
    Path('request-finalizers.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_top.py",
        """from conftest import events, saved_requests

def test_top(request):
    saved_requests.append(request)
    def finish():
        events.append('finalizer')
        try:
            request.addfinalizer(lambda: events.append('unexpected late callback'))
        except BaseException as error:
            events.append(['late', type(error).__name__])
        else:
            events.append(['late', 'accepted'])
    request.addfinalizer(finish)
    events.append('body')

def test_after_teardown():
    try:
        saved_requests[0].addfinalizer(lambda: events.append('unexpected after callback'))
    except BaseException as error:
        events.append(['after teardown', type(error).__name__])
    else:
        events.append(['after teardown', 'accepted'])
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((project.root / "request-finalizers.json").read_text())
    assert expected == [
        "node override",
        "body",
        "finalizer",
        "node override",
        ["late", "AssertionError"],
        "node override",
        ["after teardown", "AssertionError"],
    ]
    (project.root / "request-finalizers.json").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((project.root / "request-finalizers.json").read_text()) == expected


def test_fixture_request_finalizer_can_append_during_own_teardown(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

events = []
@pytest.fixture
def resource(request):
    def finish():
        events.append('fixture finalizer')
        request.addfinalizer(lambda: events.append('appended finalizer'))
    request.addfinalizer(finish)
    return object()

def pytest_sessionfinish():
    Path('request-finalizers.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_fixture.py",
        """from conftest import events

def test_fixture(resource):
    events.append('body')
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((project.root / "request-finalizers.json").read_text())
    assert expected == ["body", "fixture finalizer", "appended finalizer"]
    (project.root / "request-finalizers.json").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((project.root / "request-finalizers.json").read_text()) == expected


def test_top_request_finalizer_can_register_from_another_thread(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

events = []
def pytest_sessionfinish():
    Path('request-finalizers.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_thread.py",
        """import threading
from conftest import events

def test_thread(request):
    def register():
        try:
            request.addfinalizer(lambda: events.append('finalizer'))
        except BaseException as error:
            events.append(type(error).__name__)
        else:
            events.append('registered')
    thread = threading.Thread(target=register)
    thread.start()
    thread.join()
    events.append('body')
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((project.root / "request-finalizers.json").read_text())
    assert expected == ["registered", "body", "finalizer"]
    (project.root / "request-finalizers.json").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((project.root / "request-finalizers.json").read_text()) == expected


@pytest.mark.parametrize("setup_fails", [False, True])
def test_fixture_finalizer_registration_preserves_active_setup_exception(
    tmp_path: Path, setup_fails: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import sys
import pytest

class SetupError(Exception): pass
class RegistrationError(Exception): pass
original = SetupError('original')
registration = RegistrationError('registration')
events = []

@pytest.fixture
def resource():
    events.append('setup')
    if SETUP_FAILS:
        raise original
    return object()

def pytest_collection_modifyitems(items):
    def override(callback):
        active = sys.exception()
        events.append(['override', active is original, active is None])
        raise registration
    items[0].addfinalizer = override

def pytest_runtest_makereport(item, call):
    if call.when == 'setup' and call.excinfo is not None:
        error = call.excinfo.value
        events.append(['setup error', error is registration,
                       error.__context__ is original, error.__context__ is None])

def pytest_sessionfinish():
    Path('registration-context.json').write_text(json.dumps(events))
""".replace("SETUP_FAILS", repr(setup_fails)),
    )
    project.write("test_case.py", "def test_case(resource): pass\n")
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(1)
    expected = json.loads((project.root / "registration-context.json").read_text())
    assert expected == [
        "setup",
        ["override", setup_fails, not setup_fails],
        ["setup error", True, setup_fails, not setup_fails],
    ]
    (project.root / "registration-context.json").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    assert independent.returncode == stock.returncode, independent.stdout + independent.stderr
    assert json.loads((project.root / "registration-context.json").read_text()) == expected


def test_equal_distinct_custom_node_registers_on_active_owner(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

events = []
class CaseItem(pytest.Item):
    def __hash__(self):
        return hash(self.name)
    def __eq__(self, other):
        if not isinstance(other, CaseItem):
            return NotImplemented
        return self.name == other.name
    def runtest(self):
        equal = type(self).from_parent(self.parent, name=self.name)
        assert equal is not self and equal == self and hash(equal) == hash(self)
        equal.addfinalizer(lambda: events.append('finalizer'))
        events.append('body')

class CaseFile(pytest.File):
    def collect(self):
        yield CaseItem.from_parent(self, name='custom')

def pytest_collect_file(file_path, parent):
    if file_path.suffix == '.equalnode':
        return CaseFile.from_parent(parent, path=file_path)

def pytest_sessionfinish():
    Path('equal-node.json').write_text(json.dumps(events))
""",
    )
    project.write("test_custom.equalnode", "")
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "equal-node.json").read_text())
    assert expected == ["body", "finalizer"]
    (tmp_path / "equal-node.json").unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    candidate.expect(0)
    assert json.loads((tmp_path / "equal-node.json").read_text()) == expected


def test_node_finalizer_cycle_with_saved_request_is_collectible_after_teardown(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_cycle.py",
        """import gc, json, weakref
from pathlib import Path

references = []
events = []
class Finalizer:
    def __init__(self, request):
        self.request = request
        self.cycle = self
    def __call__(self):
        events.append('called')
        gc.collect()
        events.append(['alive while running', references[0]() is self])

def test_first(request):
    finalizer = Finalizer(request)
    references.append(weakref.ref(finalizer))
    request.node.addfinalizer(finalizer)

def test_second():
    gc.collect()
    events.append(['collected after teardown', references[0]() is None])
    Path('finalizer-cycle.json').write_text(json.dumps(events))
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "finalizer-cycle.json").read_text())
    assert expected == ["called", ["alive while running", True], ["collected after teardown", True]]
    (tmp_path / "finalizer-cycle.json").unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    candidate.expect(0)
    assert json.loads((tmp_path / "finalizer-cycle.json").read_text()) == expected
