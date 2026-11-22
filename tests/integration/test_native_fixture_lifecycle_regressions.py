"""Compare finalized fixture access and failure cleanup with pytest execution."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

# Pytest 8.0 keeps the first fixture finalizer error; 8.1 groups all errors.
_GROUPS_FIXTURE_ERRORS = tuple(int(part) for part in pytest.version_tuple[:2]) >= (8, 1)


@pytest.mark.parametrize("workers", [1, 2])
def test_fixture_finalizer_drops_return_before_draining_new_finalizers(
    tmp_path: Path, workers: int
) -> None:
    source = """from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.fixture
def value(request):
    class Result:
        def __del__(self):
            record('result dropped')
            request.addfinalizer(lambda: record('destructor finalizer'))
    def cleanup():
        record('cleanup')
        return Result()
    request.addfinalizer(cleanup)

def test_case(value):
    pass
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy(
            "test", f"--engine={engine}", f"--workers={workers}", "--no-cov", "-q"
        )
        result.expect(0)
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == [
            "cleanup",
            "result dropped",
            "destructor finalizer",
        ]
    )


def test_node_finalizer_interrupt_preserves_pytest_formatting_callbacks(tmp_path: Path) -> None:
    source = """from pathlib import Path
import pytest

class Interrupted(KeyboardInterrupt):
    def __str__(self):
        with Path('events.txt').open('a') as stream:
            stream.write('stringified\\n')
        return 'cleanup interrupted'

def cleanup():
    raise Interrupted()

def test_case(request):
    request.addfinalizer(cleanup)
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        transcript = result.stdout + result.stderr
        assert result.returncode == 2, transcript
        assert "1 passed" in transcript, transcript
        assert "test_case.Interrupted: cleanup interrupted" in transcript, transcript
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert observed["pytest-native"] == observed["pytest"] == ["stringified", "stringified"]


@pytest.mark.parametrize("dynamic", [False, True], ids=["declared", "dynamic"])
def test_module_fixture_parameter_switch_preserves_dependency_lifetime(
    tmp_path: Path, dynamic: bool
) -> None:
    capture_arguments = "request" if dynamic else "value"
    capture_value = "request.getfixturevalue('value')" if dynamic else "value"
    expected_value = "1" if dynamic else "value"
    source = f"""from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.fixture(scope='module', params=[1, 2])
def value(request):
    record(f'value setup {{request.param}}')
    yield request.param
    record(f'value teardown {{request.param}}')

@pytest.fixture(scope='module')
def capture({capture_arguments}):
    captured = {capture_value}
    record(f'capture setup {{captured}}')
    yield captured
    record(f'capture teardown {{captured}}')

def test_case(value, capture):
    assert capture == {expected_value}
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        transcript = result.stdout + result.stderr
        assert result.returncode == 0, transcript
        assert "2 passed" in transcript, transcript
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert observed["pytest-native"] == observed["pytest"]
    assert observed["pytest-native"].count("capture setup 1") == 1
    assert ("capture setup 2" in observed["pytest-native"]) == (not dynamic)


@pytest.mark.parametrize("xfail", [False, True], ids=["error", "xfail"])
def test_parameter_switch_teardown_preserves_exception_and_traceback(
    tmp_path: Path, xfail: bool
) -> None:
    marker = (
        "@pytest.mark.xfail(raises=ValueError, reason='expected parameter switch teardown')"
        if xfail
        else ""
    )
    source = f"""from pathlib import Path
import pytest

@pytest.fixture(scope='module', params=[1, 2])
def value(request):
    yield request.param
    if request.param == 1:
        Path('finalizer-reached.txt').write_text('reached')
        raise ValueError('parameter switch teardown')

{marker}
def test_case(value):
    assert value == 1
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-rxX")
        transcript = result.stdout + result.stderr
        assert (project.root / "finalizer-reached.txt").exists(), transcript
        assert (project.root / "finalizer-reached.txt").read_text() == "reached", transcript
        summary = "1 xfailed, 1 xpassed" if xfail else "1 passed, 1 error"
        assert summary in transcript, transcript
        if not xfail:
            assert "ValueError: parameter switch teardown" in transcript, transcript
            assert "raise ValueError('parameter switch teardown')" in transcript, transcript
            assert "RuntimeError:" not in transcript, transcript
        observed[engine] = result.returncode
    assert observed["pytest-native"] == observed["pytest"] == (0 if xfail else 1)


def test_finalized_fixture_lookup_fails_without_recreating_fixture(tmp_path: Path) -> None:
    source = """from pathlib import Path
import pytest
saved = None
saved_value = None

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.fixture(scope='session')
def shared():
    return object()

@pytest.fixture
def reader(request, shared):
    global saved
    saved = request
    yield
    assert request.getfixturevalue('shared') is shared
    with pytest.raises(AssertionError, match='already been torn down'):
        request.getfixturevalue('value')
    record('lookup rejected')

@pytest.fixture
def value(request):
    global saved_value
    saved_value = request
    record('setup value')
    yield object()
    record('teardown value')

def test_case(reader, value):
    pass

def test_escaped_request(shared):
    assert saved.getfixturevalue('shared') is shared
    with pytest.raises(AssertionError, match='already been torn down'):
        saved.getfixturevalue('value')
    with pytest.raises(AssertionError, match='already been torn down'):
        saved_value.getfixturevalue('value')
    record('escaped lookup rejected')
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == [
            "setup value",
            "teardown value",
            "lookup rejected",
            "escaped lookup rejected",
        ]
    )


@pytest.mark.parametrize("generator", [False, True], ids=["function", "generator"])
def test_explicit_async_fixture_keeps_loop_scope_for_sync_callable(
    tmp_path: Path, generator: bool
) -> None:
    body = (
        "    yield loop\n"
        "    assert asyncio.get_event_loop() is loop\n"
        "    assert not loop.is_closed()\n"
        "    Path('finalized.txt').write_text('finalized')\n"
        if generator
        else "    return loop\n"
    )
    source = f"""import asyncio
from pathlib import Path
import vfy

@vfy.async_fixture(loop_scope='module')
def value():
    loop = asyncio.get_event_loop()
{body}
def test_case(value, request):
    runner = request.getfixturevalue('_module_scoped_runner')
    assert value is runner.get_loop()
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        observed[engine] = (project.root / "finalized.txt").exists()
    assert observed["pytest-native"] == observed["pytest"] == generator


@pytest.mark.parametrize("raises", ["Exception", "ExceptionGroup"])
def test_grouped_fixture_teardown_matches_xfail_exception_type(tmp_path: Path, raises: str) -> None:
    source = f"""from pathlib import Path
import pytest

@pytest.fixture
def resource(request):
    def cleanup():
        with Path('events.txt').open('a') as stream:
            stream.write('cleanup\\n')
        raise ValueError('cleanup error')
    request.addfinalizer(cleanup)
    request.addfinalizer(cleanup)
    return 1

@pytest.mark.xfail(raises={raises}, reason='cleanup failures')
def test_case(resource):
    assert resource == 1
"""
    matches = raises == "Exception" or _GROUPS_FIXTURE_ERRORS
    summary = "1 xfailed, 1 xpassed" if matches else "1 xpassed, 1 error"
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "-q", "-rxX"
        )
        transcript = result.stdout + result.stderr
        assert summary in transcript, transcript
        observed[engine] = (
            result.returncode,
            (project.root / "events.txt").read_text().splitlines(),
        )
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == (
            0 if matches else 1,
            ["cleanup", "cleanup"],
        )
    )


def test_grouped_fixture_keyboard_interrupt_is_an_error_and_continues(tmp_path: Path) -> None:
    source = """from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.fixture
def resource(request):
    def ordinary():
        record('ordinary cleanup')
        raise ValueError('ordinary cleanup error')
    def interrupted():
        record('interrupted cleanup')
        raise KeyboardInterrupt('interrupt cleanup error')
    request.addfinalizer(ordinary)
    request.addfinalizer(interrupted)
    return 1

def test_first(resource):
    assert resource == 1

def test_second():
    record('second test ran')
"""
    summary = "2 passed, 1 error" if _GROUPS_FIXTURE_ERRORS else "1 passed"
    events = ["interrupted cleanup", "ordinary cleanup"]
    if _GROUPS_FIXTURE_ERRORS:
        events.append("second test ran")
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        transcript = result.stdout + result.stderr
        assert summary in transcript, transcript
        observed[engine] = (
            result.returncode,
            (project.root / "events.txt").read_text().splitlines(),
        )
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == (1 if _GROUPS_FIXTURE_ERRORS else 2, events)
    )


@pytest.mark.parametrize("junit", [False, True], ids=["terminal", "junit"])
def test_direct_fixture_keyboard_interrupt_across_scopes_interrupts_session(
    tmp_path: Path, junit: bool
) -> None:
    source = """from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.fixture(scope='session')
def shared():
    yield
    record('session cleanup')
    raise KeyboardInterrupt('session control')

@pytest.fixture
def resource(shared):
    yield
    record('function cleanup')
    raise ValueError('function error')

def test_case(resource):
    record('test body')
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        arguments = ("--junitxml=results.xml",) if junit else ("-q",)
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", *arguments
        )
        transcript = result.stdout + result.stderr
        assert result.returncode == 2, transcript
        assert "1 passed" in transcript, transcript
        if junit:
            assert transcript.count("generated xml file:") == 1, transcript
            report = ET.parse(project.root / "results.xml")
            cases = list(report.iter("testcase"))
            assert [(case.get("classname"), case.get("name")) for case in cases] == [
                ("test_case", "test_case")
            ]
            assert not [
                child
                for case in cases
                for child in case
                if child.tag in {"failure", "error", "skipped"}
            ]
            suite = next(report.iter("testsuite"))
            assert {key: suite.get(key) for key in ("tests", "errors", "failures", "skipped")} == {
                "tests": "1",
                "errors": "0",
                "failures": "0",
                "skipped": "0",
            }
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == ["test body", "function cleanup", "session cleanup"]
    )


def test_grouped_fixture_interrupt_skips_remaining_node_finalizers(tmp_path: Path) -> None:
    source = """from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.fixture
def ordinary():
    yield
    record('ordinary fixture cleanup')

@pytest.fixture
def grouped(request):
    def interrupted():
        record('interrupt cleanup')
        raise KeyboardInterrupt('grouped interrupt')
    def failed():
        record('value cleanup')
        raise ValueError('grouped value')
    request.addfinalizer(failed)
    request.addfinalizer(interrupted)
    return 1

def test_first(ordinary, grouped):
    pass

def test_second():
    record('second test')
"""
    status = 1 if _GROUPS_FIXTURE_ERRORS else 2
    summary = "2 passed, 1 error" if _GROUPS_FIXTURE_ERRORS else "1 passed"
    events = ["interrupt cleanup", "value cleanup"]
    if _GROUPS_FIXTURE_ERRORS:
        events.append("second test")
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        transcript = result.stdout + result.stderr
        assert result.returncode == status, transcript
        assert summary in transcript, transcript
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert observed["pytest-native"] == observed["pytest"] == events


@pytest.mark.skipif(not hasattr(pytest, "RaisesExc"), reason="requires pytest RaisesExc matcher")
def test_raising_teardown_xfail_matcher_completes_session_cleanup(tmp_path: Path) -> None:
    source = """from pathlib import Path
import warnings
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

def check(error):
    record('matcher called')
    raise RuntimeError('matcher failed')

@pytest.fixture(scope='session')
def shared():
    yield
    record('session finalized')
    warnings.warn('session cleanup warning', UserWarning)

@pytest.fixture
def value(shared):
    yield
    record('value finalized')
    raise ValueError('fixture failed')

@pytest.mark.filterwarnings('ignore:session cleanup warning')
@pytest.mark.xfail(raises=pytest.RaisesExc(ValueError, check=check), reason='expected teardown')
def test_first(value):
    record('first called')

def test_second(shared):
    record('second called')
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        project.write(
            "run_case.py",
            f"""from pathlib import Path
import runpy
import sys
import warnings
filters = list(warnings.filters)
sys.argv = ['vfy', 'test', '--engine={engine}', '--workers=1', '--no-cov', '-q']
try:
    runpy.run_module('vfy', run_name='__main__', alter_sys=True)
except SystemExit as error:
    status = error.code
assert warnings.filters == filters, 'warning filters were not restored'
Path('warnings-restored.txt').write_text('restored')
raise SystemExit(status)
""",
        )
        result = project.run_python("run_case.py")
        transcript = result.stdout + result.stderr
        assert result.returncode == 3, transcript
        assert "matcher failed" in transcript, transcript
        assert (project.root / "warnings-restored.txt").read_text() == "restored"
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == [
            "first called",
            "value finalized",
            "matcher called",
            "session finalized",
        ]
    )


def test_escaped_fixture_requests_observe_current_cached_instance(tmp_path: Path) -> None:
    source = """import pytest
saved = own_saved = None
previous = None

@pytest.fixture
def value(request):
    global own_saved
    if own_saved is None:
        own_saved = request
    return object()

@pytest.fixture
def observer(request):
    global saved
    saved = request
    return request.getfixturevalue('value')

def test_first(observer, value):
    global previous
    previous = value
    assert observer is value

def test_second(value):
    assert value is not previous
    assert saved.getfixturevalue('value') is value
    assert own_saved.getfixturevalue('value') is value
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        assert "2 passed" in result.stdout


def test_cached_own_fixture_lookup_checks_declared_scope(tmp_path: Path) -> None:
    source = """import pytest
from pathlib import Path

@pytest.fixture
def value(request):
    yield 42
    Path('lookup-reached.txt').touch()
    assert request.getfixturevalue('value') == 42

@pytest.mark.parametrize('value', [1], indirect=True, scope='module')
def test_case(value):
    pass
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        transcript = result.stdout + result.stderr
        assert (project.root / "lookup-reached.txt").exists(), transcript
        observed[engine] = (result.returncode, "ScopeMismatch" in transcript)
    assert observed["pytest-native"] == observed["pytest"]


@pytest.mark.parametrize("generator", [False, True], ids=["function", "generator"])
def test_explicit_sync_async_fixture_rejects_narrower_loop_scope(
    tmp_path: Path, generator: bool
) -> None:
    body = "    yield 42\n" if generator else "    return 42\n"
    source = f"""import vfy

@vfy.async_fixture(scope='module', loop_scope='function')
def value():
{body}
def test_case(value):
    pass
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(1)
        assert "ScopeMismatch" in result.stdout + result.stderr


@pytest.mark.parametrize("replacement", ["callable", "generator-code", "function-code"])
def test_dependency_changes_live_fixture_callable_and_cleanup(
    tmp_path: Path, replacement: str
) -> None:
    initial = "    yield 1\n" if replacement == "function-code" else "    return 1\n"
    replacement_body = (
        "        yield 42\n        record('cleanup')\n"
        if replacement == "generator-code"
        else "        return 42\n"
    )
    update = (
        "    node = request.node.nodeid if pytest.version_tuple[:2] == (8, 0) else request.node\n"
        "    definition = request._fixturemanager.getfixturedefs('value', node)[-1]\n"
        "    definition.func = replacement\n"
        if replacement == "callable"
        else "    value.__wrapped__.__code__ = replacement.__code__\n"
    )
    source = f"""from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.fixture
def changer(request):
    def replacement(changer):
{replacement_body}{update}
@pytest.fixture
def value(changer):
{initial}
def test_case(value):
    assert value == 42
    record('call')
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == (["call", "cleanup"] if replacement == "generator-code" else ["call"])
    )


@pytest.mark.parametrize("becomes_async", [False, True], ids=["becomes-sync", "becomes-async"])
def test_dependency_changes_async_fixture_code(tmp_path: Path, becomes_async: bool) -> None:
    initial_kind = "" if becomes_async else "async "
    replacement_kind = "async " if becomes_async else ""
    source = f"""import pytest
import vfy

@pytest.fixture
def changer():
    {replacement_kind}def replacement(changer):
        return 42
    value.__wrapped__.__code__ = replacement.__code__

@vfy.async_fixture
{initial_kind}def value(changer):
    return 1

def test_case(value):
    assert value == 42
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)


def test_dependency_changes_fixture_loop_scope_before_runner_acquisition(tmp_path: Path) -> None:
    source = """import asyncio
import pytest
import vfy

@pytest.fixture
def changer():
    value.__wrapped__._loop_scope = 'module'

@pytest.fixture(name='_function_scoped_runner')
def old_runner():
    pytest.fail('old loop scope requested')

@vfy.async_fixture(loop_scope='function')
async def value(changer):
    return asyncio.get_running_loop()

def test_case(value, request):
    assert value is request.getfixturevalue('_module_scoped_runner').get_loop()
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)


def test_cached_fixture_does_not_validate_changed_loop_scope(tmp_path: Path) -> None:
    source = """import vfy

@vfy.async_fixture(scope='module', loop_scope='module')
async def shared():
    return 42

def test_first(shared):
    assert shared == 42
    globals()['shared'].__wrapped__._loop_scope = 'invalid'

def test_second(shared):
    assert shared == 42
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        assert "2 passed" in result.stdout, result.stdout + result.stderr


def test_async_fixture_finishes_before_implicit_runner_dependency_changes(tmp_path: Path) -> None:
    source = """import asyncio
from pathlib import Path
import pytest
import vfy

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.fixture(scope='module', params=[1, 2])
def token(request):
    return request.param

@pytest.fixture(scope='module', name='_module_scoped_runner')
def runner(token):
    with asyncio.Runner() as current:
        record(f'runner setup {token}')
        yield current
        record(f'runner teardown {token}')

@vfy.async_fixture(scope='module', loop_scope='module')
async def value():
    loop = asyncio.get_running_loop()
    record('value setup')
    yield loop
    assert not loop.is_closed()
    record('value teardown')

def test_case(_module_scoped_runner, value):
    assert value is _module_scoped_runner.get_loop()
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        assert "2 passed" in result.stdout, result.stdout + result.stderr
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == [
            "runner setup 1",
            "value setup",
            "value teardown",
            "runner teardown 1",
            "runner setup 2",
            "value setup",
            "value teardown",
            "runner teardown 2",
        ]
    )


def test_dependency_marks_sync_fixture_as_coroutine_without_changing_its_code(
    tmp_path: Path,
) -> None:
    source = """import inspect
import pytest
becomes_async = False

@pytest.fixture
def changer(request):
    global becomes_async
    if request.node.name == 'test_marked':
        becomes_async = True
        inspect.markcoroutinefunction(value.__wrapped__)

@pytest.fixture
def value(changer):
    if not becomes_async:
        return 42
    async def result():
        return 42
    return result()

def test_initial(value):
    assert value == 42

def test_marked(value):
    assert value == 42
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_mode = auto\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)


def test_marked_sync_test_returning_coroutine_runs_through_synchronous_adapter(
    tmp_path: Path,
) -> None:
    source = """import inspect
from pathlib import Path
import pytest

@pytest.mark.asyncio
@inspect.markcoroutinefunction
def test_case():
    async def result():
        Path('called.txt').write_text('awaited')
    return result()
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_mode = auto\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        assert (project.root / "called.txt").read_text() == "awaited"
