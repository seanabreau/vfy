"""Dynamic independent fixture requests preserve setup, teardown, and lifetimes."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()
_EVENTS = """from pathlib import Path
import pytest

def event(text):
    with Path('events').open('a') as output:
        output.write(text + '\\n')

"""


def _compare_events(project: VfyProject, *, returncode: int = 0) -> str:
    native = project.run_python("-m", "vfy.independent.entry", env={"PYTHONPATH": _SOURCE})
    assert native.returncode == returncode, (native.stdout, native.stderr)
    events = (project.root / "events").read_text()
    (project.root / "events").unlink()
    oracle = project.run_python("-m", "pytest", "-q")
    assert oracle.returncode == returncode, (oracle.stdout, oracle.stderr)
    assert events == (project.root / "events").read_text()
    return events


def test_nested_dynamic_requests_preserve_cache_and_finalizer_registration_order(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_dynamic.py",
        _EVENTS
        + """@pytest.fixture
def inner(request):
    event('inner setup')
    request.addfinalizer(lambda: event('inner callback'))
    yield object()
    event('inner teardown')

@pytest.fixture
def outer(request):
    event('outer setup')
    request.addfinalizer(lambda: event('outer first callback'))
    value = request.getfixturevalue('inner')
    request.addfinalizer(lambda: event('outer second callback'))
    yield value
    assert request.getfixturevalue('inner') is value
    event('outer teardown')

def test_dynamic(request):
    assert request.getfixturevalue('request') is request
    first = request.getfixturevalue('outer')
    assert request.getfixturevalue('outer') is first
    assert {'request', 'outer', 'inner'} <= set(request.fixturenames)
    request.addfinalizer(lambda: event('test callback'))
    event('test body')
""",
    )
    events = _compare_events(project)
    assert events.count("inner setup\n") == 1
    assert events.count("outer setup\n") == 1


def test_dynamic_lookup_after_fixture_setup_uses_its_original_request_context(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        _EVENTS
        + """@pytest.fixture
def value():
    event('base setup')
    yield 'base'
    event('base teardown')

@pytest.fixture
def late():
    event('late setup')
    yield 'late'
    event('late teardown')
""",
    )
    project.write(
        "test_override.py",
        _EVENTS
        + """@pytest.fixture
def value(request):
    base = request.getfixturevalue('value')
    event('override setup')
    yield base, request
    event('override teardown')

def test_override(value):
    base, request = value
    assert base == 'base'
    assert request.getfixturevalue('late') == 'late'
    request.addfinalizer(lambda: event('late callback'))
    event('test body')
""",
    )
    _compare_events(project)


def test_dynamic_setup_and_teardown_errors_run_all_registered_finalizers(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_errors.py",
        _EVENTS
        + """@pytest.fixture
def broken(request):
    event('broken setup')
    request.addfinalizer(lambda: event('broken cleanup'))
    raise ValueError('setup failure')

@pytest.fixture
def cleanup_error(request):
    request.addfinalizer(lambda: event('cleanup first'))
    def fail():
        event('cleanup failure')
        raise ValueError('teardown failure')
    request.addfinalizer(fail)
    request.addfinalizer(lambda: event('cleanup last'))
    return 'ready'

def test_errors(request):
    for _ in range(2):
        with pytest.raises(ValueError, match='setup failure'):
            request.getfixturevalue('broken')
    assert request.getfixturevalue('cleanup_error') == 'ready'
    event('test body')
""",
    )
    events = _compare_events(project, returncode=1)
    assert events.count("broken setup\n") == 1
    assert "cleanup last\ncleanup failure\ncleanup first\n" in events


def test_saved_requests_reject_values_after_fixture_and_run_finalization(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_lifetime.py",
        _EVENTS
        + """import atexit
saved = []

@pytest.fixture
def resource():
    event('resource setup')
    yield object()
    event('resource teardown')

def unavailable(request, label):
    assert 'resource' in request.fixturenames
    try:
        request.getfixturevalue('resource')
    except AssertionError:
        event(label + ' unavailable')
    else:
        raise AssertionError('a finalized fixture must not be recreated')

def test_first(request):
    saved.append(request)
    request.getfixturevalue('resource')
    atexit.register(unavailable, request, 'finished run')
    event('first test')

def test_second():
    unavailable(saved[0], 'finished test')
    assert saved[0].function.__name__ == 'test_first'
    event('second test')
""",
    )
    events = _compare_events(project)
    assert events.count("resource setup\n") == 1
    assert events.endswith("finished run unavailable\n")


def test_retained_request_metadata_reads_live_item_and_parent_objects(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_live_request.py",
        """import json
from pathlib import Path
import threading
import pytest

@pytest.fixture(scope='module')
def module_request(request):
    return request

@pytest.fixture(scope='class')
def class_request(request):
    return request

@pytest.fixture
def function_request(request):
    return request

class TestOwner:
    def test_live(self, request, function_request, class_request, module_request):
        item = request.node
        class_node = item.getparent(pytest.Class)
        module_node = item.getparent(pytest.Module)
        original_item, original_class, module = item.obj, class_node.obj, module_node.obj
        owner = type(item)
        absent = object()
        old_instance = owner.__dict__.get('instance', absent)
        def replacement(): pass
        class Replacement: pass
        instance = object()
        requests = (request, function_request)
        records = {}
        try:
            item.obj = replacement
            class_node.obj = Replacement
            owner.instance = property(lambda self: instance)
            records['function'] = [value.function is replacement for value in requests]
            records['class'] = [value.cls is Replacement
                                for value in (*requests, class_request)]
            records['module'] = [value.module is module
                                 for value in (*requests, class_request, module_request)]
            records['instance'] = [value.instance is instance for value in requests]
            records['broad_instance'] = [class_request.instance is None,
                                         module_request.instance is None]
            def read_in_thread():
                try:
                    records['thread'] = [request.function is replacement,
                                         request.cls is Replacement,
                                         request.instance is instance,
                                         request.module is module]
                except BaseException as error:
                    records['thread'] = type(error).__name__
            thread = threading.Thread(target=read_in_thread)
            thread.start()
            thread.join()
            try:
                module_request.cls
            except AttributeError:
                records['module_cls'] = 'AttributeError'
            else:
                records['module_cls'] = 'accepted'
        finally:
            item.obj = original_item
            class_node.obj = original_class
            if old_instance is absent:
                del owner.instance
            else:
                owner.instance = old_instance
        Path('live-request.json').write_text(json.dumps(records))
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((project.root / "live-request.json").read_text())
    assert expected == {
        "function": [True, True],
        "class": [True, True, True],
        "module": [True, True, True, True],
        "instance": [True, True],
        "broad_instance": [True, True],
        "thread": [True, True, True, True],
        "module_cls": "AttributeError",
    }
    (project.root / "live-request.json").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((project.root / "live-request.json").read_text()) == expected
