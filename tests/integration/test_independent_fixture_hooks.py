"""Real fixture hooks share the live binding cache and request lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import ENGINES, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CONFT = """import json, sys, pytest
from pathlib import Path
MODE = {mode!r}
events=[]
def describe(cache):
    if cache is None: return None
    return [cache[0], cache[1], None if cache[2] is None else type(cache[2][0]).__name__]

@pytest.hookimpl(tryfirst=True, specname='pytest_fixture_setup')
def pytest_override(fixturedef, request):
    if fixturedef.argname == 'provider' and MODE.startswith('short_'):
        events.append(['short', fixturedef.cached_result is None])
        if MODE == 'short_cached':
            fixturedef.cached_result = (17, fixturedef.cache_key(request), None)
        return 23

cached_tuple = None
@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    global cached_tuple
    if fixturedef.argname != 'provider':
        return (yield)
    events.append(['before', fixturedef.scope, request.scope,
        request._get_fixturestack()[-1] is fixturedef, describe(fixturedef.cached_result)])
    try:
        value = yield
    except BaseException as error:
        cache = fixturedef.cached_result
        cached_tuple = cache
        events.append(['after_error', type(error).__name__, describe(cache),
            cache is not None and cache[2][0] is error])
        raise
    cached_tuple = fixturedef.cached_result
    events.append(['after', value, describe(cached_tuple)])
    return 29 if MODE == 'wrapped_result' else value

def pytest_fixture_post_finalizer(fixturedef, request):
    if fixturedef.argname == 'provider':
        events.append(['post', request._get_fixturestack()[-1] is fixturedef,
            describe(fixturedef.cached_result), fixturedef.cached_result is cached_tuple,
            len(fixturedef._finalizers)])
        if MODE == 'post_error': raise LookupError('post-boom')

@pytest.fixture(scope='session')
def provider(request):
    events.append(['body'])
    if MODE == 'setup_error': raise RuntimeError('setup-boom')
    request.addfinalizer(lambda: events.append(['user_finalizer']))
    yield 11
    events.append(['generator_finalizer'])

def pytest_sessionfinish():
    Path('events.json').write_text(json.dumps(events))
"""
_BODY = """from conftest import events

def test_first(provider):
    events.append(['test_first', provider])
def test_second(provider):
    events.append(['test_second', provider])
"""
_EXPECTED = {
    "normal": [
        ["before", "session", "session", True, None],
        ["body"],
        ["after", 11, [11, None, None]],
        ["test_first", 11],
        ["test_second", 11],
        ["generator_finalizer"],
        ["user_finalizer"],
        ["post", True, [11, None, None], True, 0],
    ],
    "wrapped_result": [
        ["before", "session", "session", True, None],
        ["body"],
        ["after", 11, [11, None, None]],
        ["test_first", 11],
        ["test_second", 11],
        ["generator_finalizer"],
        ["user_finalizer"],
        ["post", True, [11, None, None], True, 0],
    ],
    "short_cached": [
        ["before", "session", "session", True, None],
        ["short", True],
        ["after", 23, [17, None, None]],
        ["test_first", 17],
        ["test_second", 17],
        ["post", True, [17, None, None], True, 0],
    ],
    "short_uncached": [
        ["before", "session", "session", True, None],
        ["short", True],
        ["after", 23, None],
    ],
    "setup_error": [
        ["before", "session", "session", True, None],
        ["body"],
        ["after_error", "RuntimeError", [None, None, "RuntimeError"], True],
        ["post", True, [None, None, "RuntimeError"], True, 0],
    ],
    "post_error": [
        ["before", "session", "session", True, None],
        ["body"],
        ["after", 11, [11, None, None]],
        ["test_first", 11],
        ["test_second", 11],
        ["generator_finalizer"],
        ["user_finalizer"],
        ["post", True, [11, None, None], True, 0],
    ],
}


@ENGINES
@pytest.mark.parametrize("mode", list(_EXPECTED))
def test_fixture_hooks_observe_actual_binding_cache_and_request(
    tmp_path: Path, engine: str, mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", _CONFT.format(mode=mode))
    project.write("test_hooks.py", _BODY)
    result = project.run_engine(engine, "-q")
    assert result.returncode == (
        1 if mode in {"short_uncached", "setup_error", "post_error"} else 0
    ), result.stdout + result.stderr
    assert json.loads((tmp_path / "events.json").read_text()) == _EXPECTED[mode]


@ENGINES
def test_setup_wrapper_reentry_reads_the_live_cached_result(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest
@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    value = yield
    if fixturedef.argname == 'value':
        top_request = request._pyfuncitem._request
        assert top_request is not request
        assert top_request.getfixturevalue('value') is value
        assert request._get_fixturestack()[-1] is fixturedef
        assert fixturedef.cached_result[0] is value
    return value
@pytest.fixture
def value():
    return object()
""",
    )
    project.write(
        "test_hooks.py",
        """def test_value(value, request):
    assert request.getfixturevalue('value') is value
""",
    )
    result = project.run_engine(engine, "-q")
    result.expect(0)


@ENGINES
def test_class_and_direct_parameter_bindings_dispatch_real_setup_hooks(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path
events=[]
@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    value = yield
    if fixturedef.argname in {'value', 'direct'}:
        assert request._get_fixturestack()[-1] is fixturedef
        assert fixturedef.cached_result[0] is value
        events.append([fixturedef.argname, value])
    return value
def pytest_sessionfinish():
    Path('events.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_hooks.py",
        """import pytest
class TestHooks:
    marker = 31
    @pytest.fixture
    def value(self):
        return self.marker
    @pytest.mark.parametrize('direct', [7])
    def test_value(self, value, direct):
        assert (value, direct) == (31, 7)
""",
    )
    result = project.run_engine(engine, "-q")
    result.expect(0)
    assert sorted(json.loads((tmp_path / "events.json").read_text())) == [
        ["direct", 7],
        ["value", 31],
    ]


@ENGINES
def test_fixture_hooks_use_the_actual_fixture_scope_conftest_subset(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest
@pytest.fixture(scope='session')
def provider():
    return 17
""",
    )
    project.write(
        "child/conftest.py",
        """import json, pytest
from pathlib import Path
events=[]
def pytest_fixture_setup(fixturedef, request):
    assert fixturedef.argname != 'provider'
    if fixturedef.argname == 'local':
        assert request.node is request._pyfuncitem
        events.append(['setup', fixturedef.argname])
def pytest_fixture_post_finalizer(fixturedef, request):
    assert fixturedef.argname != 'provider'
    if fixturedef.argname == 'local':
        events.append(['post', fixturedef.argname])
@pytest.fixture
def local():
    return 23
def pytest_sessionfinish():
    Path('events.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "child/test_hooks.py",
        """def test_value(provider, local):
    assert (provider, local) == (17, 23)
""",
    )
    result = project.run_engine(engine, "-q")
    result.expect(0)
    assert json.loads((tmp_path / "events.json").read_text()) == [
        ["setup", "local"],
        ["post", "local"],
    ]


@ENGINES
def test_default_setup_reads_dependency_cache_after_wrappers_resume(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest
binding = None
@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    global binding
    if fixturedef.argname == 'value':
        assert binding is not None
        previous = binding.cached_result
        assert previous is not None and previous[0] == 17
        binding.cached_result = (19, previous[1], None)
    value = yield
    if fixturedef.argname == 'dependency':
        binding = fixturedef
    return value
@pytest.fixture
def dependency():
    return 17
@pytest.fixture
def value(dependency):
    return dependency
""",
    )
    project.write(
        "test_hooks.py",
        """def test_value(value, request):
    assert value == 19
    assert request.getfixturevalue('dependency') == 19
""",
    )
    result = project.run_engine(engine, "-q")
    result.expect(0)


@ENGINES
@pytest.mark.parametrize("mode", ["suppress", "raise"])
def test_wrapper_exception_outcome_does_not_replace_the_core_cache(
    tmp_path: Path, engine: str, mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"""import json, pytest
from pathlib import Path
MODE = {mode!r}
events=[]
error = None
@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    global error
    if fixturedef.argname != 'provider':
        return (yield)
    events.append('hook')
    try:
        value = yield
    except RuntimeError as caught:
        error = caught
        assert fixturedef.cached_result[2][0] is caught
        return 29
    raise LookupError('wrapper-boom')
@pytest.fixture(scope='session')
def provider():
    events.append('body')
    if MODE == 'suppress':
        raise RuntimeError('core-boom')
    return 11
def pytest_fixture_post_finalizer(fixturedef, request):
    if fixturedef.argname == 'provider':
        cached = fixturedef.cached_result
        assert cached is not None
        events.append(['post', cached[0],
            cached[2] is not None and cached[2][0] is error])
def pytest_sessionfinish():
    Path('events.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_hooks.py",
        f"""from pathlib import Path
MODE = {mode!r}
def test_first(provider):
    assert MODE == 'suppress' and provider is None
    Path('first').touch()
def test_second(provider):
    assert MODE == 'raise' and provider == 11
    Path('second').touch()
""",
    )
    result = project.run_engine(engine, "-q")
    result.expect(1)
    assert (tmp_path / "first").exists() is (mode == "suppress")
    assert (tmp_path / "second").exists() is (mode == "raise")
    assert json.loads((tmp_path / "events.json").read_text()) == [
        "hook",
        "body",
        ["post", None if mode == "suppress" else 11, mode == "suppress"],
    ]


def test_fixture_definition_supports_live_cross_thread_access_and_gc(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import gc, json, threading, pytest
from pathlib import Path

events = []

@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup(fixturedef):
    if fixturedef.argname != 'resource':
        return
    def observe():
        running = False
        def during_gc(phase, info):
            nonlocal running
            if phase == 'start' and not running:
                running = True
                try:
                    events.append(['gc', fixturedef.argname, fixturedef.scope])
                finally:
                    running = False
        try:
            original_func = fixturedef.func
            original_cached = fixturedef.cached_result
            original_list = fixturedef._finalizers
            original_count = len(original_list)
            gc.callbacks.append(during_gc)
            try:
                gc.collect()
            finally:
                gc.callbacks.remove(during_gc)
            fixturedef.func = original_func
            fixturedef.cached_result = original_cached
            fixturedef.addfinalizer(lambda: events.append('finalizer'))
            events.append(['thread', fixturedef.argname, fixturedef.scope,
                           fixturedef.func is original_func,
                           fixturedef.cached_result is original_cached,
                           fixturedef._finalizers is original_list,
                           len(original_list) == original_count + 1])
        except BaseException as error:
            events.append(['error', type(error).__name__, str(error)])
    thread = threading.Thread(target=observe)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()

@pytest.fixture
def resource():
    return object()

def pytest_sessionfinish():
    Path('fixture-thread.json').write_text(json.dumps(events))
""",
    )
    project.write("test_thread.py", "def test_body(resource): pass\n")
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "fixture-thread.json").read_text())
    assert expected == [
        ["gc", "resource", "function"],
        ["thread", "resource", "function", True, True, True, True],
        "finalizer",
    ]
    (tmp_path / "fixture-thread.json").unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    candidate.expect(0)
    assert json.loads((tmp_path / "fixture-thread.json").read_text()) == expected


@ENGINES
@pytest.mark.parametrize("action", ["read", "replace"])
def test_cached_result_remains_authoritative_during_gc_reentry(
    tmp_path: Path, engine: str, action: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"ACTION = {action!r}\n"
        """import gc
import pytest

events = []
@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    value = yield
    if fixturedef.argname != 'subject':
        return value
    armed = False
    fired = False
    def on_gc(phase, info):
        nonlocal fired
        if phase != 'start' or not armed or fired:
            return
        fired = True
        nested = fixturedef.cached_result
        events.append(nested[0])
        if ACTION == 'replace':
            fixturedef.cached_result = ('replacement', nested[1], None)
    old_threshold = gc.get_threshold()
    gc.callbacks.append(on_gc)
    try:
        gc.set_threshold(1, 1, 1)
        armed = True
        # Low thresholds permit GC during first projection. Stock pytest may
        # not allocate on this read, so trigger the callback afterward if needed.
        observed = fixturedef.cached_result
        if not fired:
            gc.collect()
        current = fixturedef.cached_result
        assert current is fixturedef.cached_result
        if ACTION == 'read':
            assert observed is current
        events.append(current[0])
    finally:
        armed = False
        gc.set_threshold(*old_threshold)
        gc.callbacks.remove(on_gc)
    assert fired
    return value

@pytest.fixture
def subject():
    return 'original'
""",
    )
    project.write(
        "test_gc_fixture.py",
        f"""from conftest import events

def test_value(subject):
    assert subject == {"replacement" if action == "replace" else "original"!r}
    assert events == ['original', {"replacement" if action == "replace" else "original"!r}]
""",
    )
    result = project.run_engine(engine, "-q")
    result.expect(0)
