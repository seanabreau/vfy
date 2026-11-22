"""Fixture lookup exceptions retain their actual requesting runtime objects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_OBSERVER = """import json
from pathlib import Path
import pytest

records = []
def describe(error, item=None, expected_request=None):
    request = getattr(error, 'request', None)
    result = {
        'type': type(error).__name__,
        'lookup_base': isinstance(error, LookupError),
        'public_identity': type(error) is getattr(pytest, 'FixtureLookupError', None),
        'argname': getattr(error, 'argname', None),
        'msg': getattr(error, 'msg', None),
        'args': ['request' if value is request else value for value in error.args]
            if request is not None else [str(value) for value in error.args],
    }
    if request is not None:
        actual_item = request._pyfuncitem
        result['request'] = {
            'scope': request.scope,
            'name': request.fixturename,
            'actual_item': item is None or actual_item is item,
            'actual_function': actual_item.obj is request._pyfuncitem.obj,
            'same_request': expected_request is None or request is expected_request,
            'same_top_request': request is getattr(actual_item, '_request', None),
        }
        result['stack'] = [[definition.argname, definition.scope, definition.func.__name__]
                           for definition in error.fixturestack]
        current = request._get_fixturestack()
        result['stack_identity'] = len(current) == len(error.fixturestack) and all(
            left is right for left, right in zip(current, error.fixturestack))
        representation = error.formatrepr()
        result['representation'] = {
            'type': type(representation).__name__,
            'filename': str(representation.filename),
            'firstlineno': representation.firstlineno,
            'tblines': list(representation.tblines),
            'errorstring': representation.errorstring,
            'argname': representation.argname,
            'text': str(representation),
        }
        class Writer:
            def __init__(self): self.lines = []
            def line(self, text='', **markup): self.lines.append([text, markup])
        writer = Writer()
        representation.toterminal(writer)
        result['terminal_lines'] = writer.lines
    return result

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    observation = None
    if call.excinfo is not None:
        observation = describe(call.excinfo.value, item)
    report = yield
    records.append({'when': report.when, 'outcome': report.outcome,
                    'exception': observation, 'longrepr_type': type(report.longrepr).__name__,
                    'longrepr': str(report.longrepr)})
    return report

def pytest_sessionfinish():
    Path('lookup-observations.json').write_text(json.dumps(records, indent=2))
"""


def _project(tmp_path: Path, source: str, *, fixtures: str = "") -> VfyProject:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", _OBSERVER + fixtures)
    project.write("test_lookup.py", source)
    return project


def _run(project: VfyProject, *, arguments: tuple[str, ...] = ()) -> tuple[object, object]:
    # Lookup diagnostics list available fixtures, so both runs use the same provider inventory.
    providers = ("-p", "no:asyncio", "-p", "vfy.pytest_reporting")
    stock = project.run_python("-m", "pytest", "-q", "-p", "pytest_cov", *providers, *arguments)
    project.write("stock.stdout", stock.stdout)
    project.write("stock.stderr", stock.stderr)
    observations = project.root / "lookup-observations.json"
    expected = json.loads(observations.read_text()) if observations.exists() else None
    project.write("stock-observations.json", json.dumps(expected, indent=2))
    caught = project.root / "caught.json"
    if caught.exists():
        project.write("stock-caught.json", caught.read_text())
        caught.unlink()
    observations.unlink(missing_ok=True)
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-q",
        "-p",
        "vfy.independent.outcomes",
        *providers,
        *arguments,
    )
    project.write("candidate.stdout", candidate.stdout)
    project.write("candidate.stderr", candidate.stderr)
    actual = json.loads(observations.read_text()) if observations.exists() else None
    project.write("candidate-observations.json", json.dumps(actual, indent=2))
    if caught.exists():
        project.write("candidate-caught.json", caught.read_text())
    project.write("statuses.json", json.dumps([stock.returncode, candidate.returncode]))
    assert isinstance(expected, list), stock.stdout + stock.stderr
    assert isinstance(actual, list), candidate.stdout + candidate.stderr
    return expected, actual


@pytest.mark.parametrize("scenario", ["static", "dynamic", "nested", "exhausted", "disabled"])
def test_fixture_lookup_records_real_request_and_source_stack(
    tmp_path: Path, scenario: str
) -> None:
    sources = {
        "static": "def test_body(missing_fixture): pass\n",
        "dynamic": "def test_body(request): request.getfixturevalue('missing_fixture')\n",
        "nested": "import pytest\n@pytest.fixture\n"
        "def outer(inner): return inner\n@pytest.fixture\n"
        "def inner(request): return request.getfixturevalue('missing_fixture')\n"
        "def test_body(outer): pass\n",
        "exhausted": "import pytest\n@pytest.fixture\n"
        "def exhausted(exhausted): return exhausted\n"
        "def test_body(exhausted): pass\n",
        "disabled": "def test_body(capsys): pass\n",
    }
    project = _project(tmp_path, sources[scenario])
    expected, actual = _run(
        project, arguments=("-p", "no:capture") if scenario == "disabled" else ()
    )
    assert actual == expected
    assert json.loads((tmp_path / "statuses.json").read_text()) == [1, 1]
    assert isinstance(actual, list)
    errors = [record["exception"] for record in actual if record["exception"] is not None]
    assert errors
    assert all(error["type"] == "FixtureLookupError" for error in errors)
    assert all(error["public_identity"] for error in errors)


def test_fixture_lookup_constructor_catching_and_custom_message_protocol(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        """import json
from pathlib import Path
import pytest
from conftest import describe

def test_body(request):
    from _pytest.fixtures import FixtureLookupError
    assert FixtureLookupError is pytest.FixtureLookupError
    try:
        request.getfixturevalue('missing_fixture')
    except FixtureLookupError as error:
        observation = describe(error, expected_request=request)
        assert error.request is request
        assert error.args[1] is request
        error.msg = 'caller-selected message\\nsecond line'
        observation['mutated'] = describe(error, expected_request=request)
    else:
        raise AssertionError('lookup succeeded')
    direct = FixtureLookupError(None, request, 'constructed message')
    observation['constructed'] = describe(direct, expected_request=request)
    Path('caught.json').write_text(json.dumps(observation, indent=2))
""",
    )
    expected, actual = _run(project)
    assert actual == expected
    assert json.loads((tmp_path / "statuses.json").read_text()) == [0, 0]
    assert json.loads((tmp_path / "candidate-caught.json").read_text()) == json.loads(
        (tmp_path / "stock-caught.json").read_text()
    )


def test_fixture_raiseerror_retains_stack_but_formats_requesting_side(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        """import pytest
@pytest.fixture
def outer(inner): return inner
@pytest.fixture
def inner(request): request.raiseerror('fixture-selected message')
def test_body(outer): pass
""",
    )
    expected, actual = _run(project)
    assert actual == expected
    assert json.loads((tmp_path / "statuses.json").read_text()) == [1, 1]


def test_fixture_raiseerror_accepts_none_and_retains_the_actual_request(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        """import json
from pathlib import Path
import pytest

def test_body(request):
    try:
        request.raiseerror(None)
    except pytest.FixtureLookupError as error:
        Path('caught.json').write_text(json.dumps({
            'identity': type(error) is pytest.FixtureLookupError,
            'request': error.request is request,
            'args': [error.args[0], error.args[1] is request, error.args[2]],
            'argname': error.argname, 'msg': error.msg,
            'stack': [definition.argname for definition in error.fixturestack],
        }))
    else:
        raise AssertionError('raiseerror returned')
""",
    )
    expected, actual = _run(project)
    assert actual == expected
    assert json.loads((tmp_path / "statuses.json").read_text()) == [0, 0]
    assert (
        json.loads((tmp_path / "candidate-caught.json").read_text())
        == json.loads((tmp_path / "stock-caught.json").read_text())
        == {
            "identity": True,
            "request": True,
            "args": [None, True, None],
            "argname": None,
            "msg": None,
            "stack": [],
        }
    )


@pytest.mark.parametrize("caller", ["direct", "helper", "fixture", "outside_root"])
def test_missing_parameter_failure_preserves_real_request_and_definition_locations(
    tmp_path: Path, caller: str
) -> None:
    project = _project(tmp_path, "")
    project.write(
        "parameter_definitions.py",
        "import pytest\n@pytest.fixture(params=[1, 2])\n"
        "def parameter(request): return request.param\n",
    )
    project.write(
        "helpers.py",
        "def request_parameter(request):\n    request.getfixturevalue('parameter')\n",
    )
    observer = """import json
from pathlib import Path
import pytest

def record(error):
    Path('caught.json').write_text(json.dumps({
        'type': type(error).__name__, 'identity': type(error) is pytest.fail.Exception,
        'lookup': isinstance(error, LookupError), 'msg': error.msg,
        'args': list(error.args), 'pytrace': error.pytrace,
    }))
"""
    operation = (
        "request.getfixturevalue('parameter')"
        if caller in {"direct", "fixture"}
        else "request_parameter(request)"
    )
    consumer = (
        "@pytest.fixture\ndef consumer(request):\n"
        if caller == "fixture"
        else "def test_body(request):\n"
    )
    source = (
        observer
        + "from parameter_definitions import parameter\n"
        + "from helpers import request_parameter\n"
        + consumer
        + f"    try:\n        {operation}\n"
        + "    except pytest.fail.Exception as error:\n        record(error)\n"
        + "    else:\n        raise AssertionError('parameter lookup succeeded')\n"
    )
    if caller == "fixture":
        source += "def test_body(consumer): pass\n"
    target = "tests/test_request.py" if caller == "outside_root" else "test_lookup.py"
    if caller == "outside_root":
        (tmp_path / "test_lookup.py").unlink()
    project.write(target, source)
    arguments = ("--rootdir=tests", "tests") if caller == "outside_root" else ()
    expected, actual = _run(project, arguments=arguments)
    assert actual == expected
    assert json.loads((tmp_path / "statuses.json").read_text()) == [0, 0]
    observed = json.loads((tmp_path / "candidate-caught.json").read_text())
    assert observed == json.loads((tmp_path / "stock-caught.json").read_text())
    assert observed["identity"] is True
    assert observed["lookup"] is False
    assert observed["pytrace"] is False
    assert observed["args"] == [observed["msg"]]
    assert "Requested fixture 'parameter' defined in:" in observed["msg"]
    assert "parameter_definitions.py:3" in observed["msg"]


@pytest.mark.parametrize("scenario", ["missing_parameter", "invalid_parameter_values"])
def test_parameter_failures_remain_distinct_from_fixture_lookup(
    tmp_path: Path, scenario: str
) -> None:
    source = (
        "import pytest\n@pytest.fixture(params=[1, 2])\n"
        "def parameter(request): return request.param\n"
        "def test_body(request): request.getfixturevalue('parameter')\n"
        if scenario == "missing_parameter"
        else "import pytest\n@pytest.mark.parametrize('left,right', [(1,)])\n"
        "def test_body(left, right): pass\n"
    )
    project = _project(tmp_path, source)
    expected, actual = _run(project)
    assert actual == expected
    assert json.loads((tmp_path / "statuses.json").read_text()) == (
        [1, 1] if scenario == "missing_parameter" else [2, 2]
    )


def test_fixture_lookup_visibility_across_repeated_directory_contexts(tmp_path: Path) -> None:
    project = _project(tmp_path, "")
    source = """import pytest

@pytest.mark.parametrize('value', [1, 2])
def test_missing(request, value):
    request.getfixturevalue('missing_fixture')
"""
    project.write("a/conftest.py", "import pytest\n@pytest.fixture\ndef visible_a(): pass\n")
    project.write("ab/conftest.py", "import pytest\n@pytest.fixture\ndef visible_ab(): pass\n")
    project.write(
        "a/nested/conftest.py", "import pytest\n@pytest.fixture\ndef visible_nested(): pass\n"
    )
    project.write("a/test_first.py", source)
    project.write("ab/test_second.py", source)
    project.write("a/nested/test_third.py", source)
    expected, actual = _run(
        project,
        arguments=("a/test_first.py", "ab/test_second.py", "a/nested/test_third.py"),
    )
    assert actual == expected
    assert json.loads((tmp_path / "statuses.json").read_text()) == [1, 1]
    assert isinstance(actual, list)
    errors = [record["exception"] for record in actual if record["exception"] is not None]
    assert len(errors) == 6
    available = []
    for error in errors:
        diagnostic = error["representation"]["errorstring"]
        fixture_line = next(
            line for line in diagnostic.splitlines() if "available fixtures:" in line
        )
        available.append(set(fixture_line.split("available fixtures:", 1)[1].strip().split(", ")))
    visible = {"visible_a", "visible_ab", "visible_nested"}
    assert [names & visible for names in available] == [
        {"visible_a"},
        {"visible_a"},
        {"visible_ab"},
        {"visible_ab"},
        {"visible_a", "visible_nested"},
        {"visible_a", "visible_nested"},
    ]


def test_retained_fixture_stack_keeps_identity_during_gc_and_cross_thread_read(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import gc, json, threading, pytest
from pathlib import Path

definitions = {}
saved = []

def pytest_fixture_setup(fixturedef):
    if fixturedef.argname in ('outer', 'inner'):
        definitions[fixturedef.argname] = fixturedef

@pytest.fixture
def outer(request):
    return request.getfixturevalue('inner')

@pytest.fixture
def inner(request):
    saved.append(request)
    return object()

def pytest_sessionfinish():
    observed, errors = [], []
    def read():
        running = False
        def during_gc(phase, info):
            nonlocal running
            if phase == 'start' and not running:
                running = True
                try:
                    stack = saved[0]._get_fixturestack()
                    observed.append(['gc', [row.argname for row in stack]])
                finally:
                    running = False
        try:
            gc.callbacks.append(during_gc)
            try:
                gc.collect()
            finally:
                gc.callbacks.remove(during_gc)
            first = saved[0]._get_fixturestack()
            second = saved[0]._get_fixturestack()
            names = [definition.argname for definition in second]
            same = all(definition is definitions[definition.argname] for definition in second)
            distinct = first is not second
            first.clear()
            observed.append(['read', names, same, distinct, len(second)])
        except BaseException as error:
            errors.append(type(error).__name__ + ': ' + str(error))
    thread = threading.Thread(target=read)
    thread.start()
    thread.join(10)
    Path('retained-stack.json').write_text(json.dumps([not thread.is_alive(), errors, observed]))
""",
    )
    project.write("test_stack.py", "def test_stack(outer): pass\n")
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "retained-stack.json").read_text())
    assert expected == [
        True,
        [],
        [["gc", ["outer", "inner"]], ["read", ["outer", "inner"], True, True, 2]],
    ]
    (tmp_path / "retained-stack.json").unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    candidate.expect(0)
    assert json.loads((tmp_path / "retained-stack.json").read_text()) == expected


def test_resolved_fixture_definitions_survive_worker_thread_gc(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_thread_gc.py",
        """import gc, sys, threading
import pytest

events = []

class Sentinel:
    def __del__(self):
        events.append(threading.current_thread().name)

@pytest.fixture
def resource():
    return object()

def test_leave_request_cycle(request, resource):
    gc.disable()
    cycle = [request, Sentinel()]
    cycle.append(cycle)

def test_collect_on_worker(request, resource):
    errors = []
    unraisables = []
    old_hook = sys.unraisablehook
    sys.unraisablehook = lambda event: unraisables.append(str(event.exc_value))
    registry = request._fixture_defs
    def collect():
        try:
            gc.collect()
            definition = registry['resource']
            assert registry.get('resource') is definition
            assert 'resource' in registry and definition.argname == 'resource'
            assert any(name == 'resource' and value is definition
                       for name, value in registry.items())
        except BaseException as error:
            errors.append(f'{type(error).__name__}: {error}')
    worker = threading.Thread(target=collect, name='fixture-gc-worker')
    try:
        worker.start()
        worker.join(10)
        assert not worker.is_alive()
        assert events == ['fixture-gc-worker']
        assert not unraisables
        assert not errors
    finally:
        sys.unraisablehook = old_hook
        gc.enable()
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    candidate.expect(0)
