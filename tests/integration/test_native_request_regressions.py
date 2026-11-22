"""Compare reentrant requests, dynamic fixture access, and finalizers with pytest."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


_RECORD_SOURCE = """from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a', encoding='utf-8') as stream:
        stream.write(event + '\\n')

"""


def _run_case(
    root: Path,
    engine: str,
    source: str,
    *,
    conftest_source: str | None = None,
    timeout: float = 120,
) -> tuple[VfyProject, CommandResult]:
    project = VfyProject(root / engine, timeout=timeout)
    project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope = function\n")
    project.write("test_case.py", _RECORD_SOURCE + source)
    if conftest_source is not None:
        project.write("conftest.py", conftest_source)
    try:
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "-rx",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
    except RuntimeError as error:
        if not isinstance(error.__cause__, subprocess.TimeoutExpired):
            raise
        events_path = project.root / "events.txt"
        events = events_path.read_text(encoding="utf-8") if events_path.exists() else ""
        pytest.fail(f"{engine} request timed out after events:\n{events}\n{error}")
    return project, result


def _events(project: VfyProject) -> list[str]:
    return (project.root / "events.txt").read_text(encoding="utf-8").splitlines()


def test_doctest_fixture_request_instance_is_none(tmp_path: Path) -> None:
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """import pytest

@pytest.fixture
def resource(request):
    assert request.instance is None
    return 42
""",
        )
        project.write("example.txt", ">>> getfixture('resource')\n42\n")
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--doctest-glob=*.txt",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        transcript = result.stdout + result.stderr
        assert result.returncode == 0, transcript
        assert "1 passed" in transcript, transcript


def test_cached_fixture_request_can_reenter_from_destructor(tmp_path: Path) -> None:
    source = """@pytest.fixture(scope='session')
def shared():
    return 42

class Resource:
    def __init__(self, request):
        self.request = request

    def __del__(self):
        record('destructor entered')
        try:
            self.request.getfixturevalue('shared')
        except Exception:
            pass
        record('destructor finished')

@pytest.fixture
def resource(request, shared):
    return Resource(request)

def test_one(resource):
    pass
"""
    for engine in ("pytest", "pytest-native"):
        project, result = _run_case(tmp_path, engine, source, timeout=10)
        assert (result.returncode, _events(project)) == (
            0,
            ["destructor entered", "destructor finished"],
        ), result.stdout + result.stderr


def test_cached_exception_can_reenter_request_from_with_traceback(tmp_path: Path) -> None:
    source = """class CustomError(Exception):
    def __init__(self, request):
        super().__init__('cached fixture failure')
        self.request = request

    def with_traceback(self, traceback):
        record('error entered')
        value = self.request.getfixturevalue('shared')
        record(f'error value {value}')
        return super().with_traceback(traceback)

@pytest.fixture(scope='session')
def shared():
    return 42

@pytest.fixture(scope='module')
def resource(request, shared):
    record('resource setup')
    raise CustomError(request)

def test_one(resource):
    pass

def test_two(resource):
    pass
"""
    observations: dict[str, list[str]] = {}
    for engine in ("pytest", "pytest-native"):
        project, result = _run_case(tmp_path, engine, source, timeout=10)
        transcript = result.stdout + result.stderr
        assert result.returncode == 1, transcript
        observations[engine] = _events(project)
        assert "CustomError: cached fixture failure" in transcript, transcript
        assert "2 errors" in transcript, transcript
    assert observations["pytest"] in (
        ["resource setup"],
        ["resource setup", "error entered", "error value 42"],
    )
    assert observations["pytest-native"] == observations["pytest"]


@pytest.mark.parametrize("before_setup", [True, False], ids=["before-setup", "after-setup"])
def test_test_finalizer_preserves_order_with_dynamic_fixture_setup(
    tmp_path: Path, before_setup: bool
) -> None:
    finalizer = "    request.addfinalizer(lambda: record('test finalizer'))\n"
    lookup = "    request.getfixturevalue('resource')\n"
    source = (
        "@pytest.fixture\n"
        "def resource():\n"
        "    record('resource setup')\n"
        "    yield\n"
        "    record('resource teardown')\n"
        "\n"
        "def test_one(request):\n" + (finalizer + lookup if before_setup else lookup + finalizer)
    )
    finalizers = ["resource teardown", "test finalizer"]
    if not before_setup:
        finalizers.reverse()
    for engine in ("pytest", "pytest-native"):
        project, result = _run_case(tmp_path, engine, source)
        assert (result.returncode, _events(project)) == (
            0,
            ["resource setup", *finalizers],
        ), result.stdout + result.stderr


def test_saved_base_request_retrieves_completed_override(tmp_path: Path) -> None:
    conftest_source = (
        _RECORD_SOURCE
        + """saved = None

@pytest.fixture
def resource(request):
    global saved
    saved = request
    record('base setup')
    yield 'base'
    record('base teardown')
"""
    )
    source = """import conftest

@pytest.fixture
def resource(resource):
    record('override setup')
    yield 'override-' + resource
    record('override teardown')

def test_one(resource):
    value = conftest.saved.getfixturevalue('resource')
    record(f'saved lookup {value}')
    assert value == resource
"""
    for engine in ("pytest", "pytest-native"):
        project, result = _run_case(tmp_path, engine, source, conftest_source=conftest_source)
        assert (result.returncode, _events(project)) == (
            0,
            [
                "base setup",
                "override setup",
                "saved lookup override-base",
                "override teardown",
                "base teardown",
            ],
        ), result.stdout + result.stderr


def test_saved_request_retains_dynamic_fixture_names_and_session_value(tmp_path: Path) -> None:
    source = """saved = None
saved_value = None

@pytest.fixture(scope='session')
def dynamic():
    record('dynamic setup')
    yield object()
    record('dynamic teardown')

def test_first(request):
    global saved, saved_value
    saved = request
    saved_value = request.getfixturevalue('dynamic')
    assert 'dynamic' in request.fixturenames

def test_second():
    assert 'dynamic' in saved.fixturenames
    record('saved fixture names retained')
    assert saved.getfixturevalue('dynamic') is saved_value
    record('saved session value retained')
"""
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project, result = _run_case(tmp_path, engine, source)
        transcript = result.stdout + result.stderr
        assert result.returncode == 0, transcript
        assert "2 passed" in transcript, transcript
        observations[engine] = _events(project)
    assert (
        observations["pytest-native"]
        == observations["pytest"]
        == [
            "dynamic setup",
            "saved fixture names retained",
            "saved session value retained",
            "dynamic teardown",
        ]
    )


def test_empty_generator_fixture_matches_valueerror_xfail(tmp_path: Path) -> None:
    source = """@pytest.fixture
def resource():
    record('resource setup')
    if False:
        yield

@pytest.mark.xfail(raises=ValueError, reason='fixture did not yield')
def test_one(resource):
    record('test body')
"""
    for engine in ("pytest", "pytest-native"):
        project, result = _run_case(tmp_path, engine, source)
        transcript = result.stdout + result.stderr
        assert (result.returncode, _events(project)) == (0, ["resource setup"]), transcript
        assert "1 xfailed" in transcript, transcript


def test_first_function_fixture_request_during_teardown_matches_pytest(tmp_path: Path) -> None:
    source = """import json

@pytest.fixture
def resource():
    record('resource setup')
    yield 42
    record('resource teardown')

@pytest.fixture
def outer(request):
    yield
    record('outer teardown')
    try:
        request.getfixturevalue('resource')
    except BaseException as error:
        Path('exception.json').write_text(
            json.dumps([type(error).__name__, getattr(error, 'msg', None)]),
            encoding='utf-8',
        )
        raise

def test_one(outer):
    pass
"""
    observations: dict[str, tuple[int, list[str], list[object], bool]] = {}
    for engine in ("pytest", "pytest-native"):
        project, result = _run_case(tmp_path, engine, source)
        transcript = result.stdout + result.stderr
        assert result.returncode == 1, transcript
        exception = json.loads((project.root / "exception.json").read_text(encoding="utf-8"))
        observations[engine] = (
            result.returncode,
            _events(project),
            exception,
            'Calling request.getfixturevalue("resource") during teardown is deprecated.'
            in transcript,
        )

    stock_exception = observations["pytest"][2]
    if stock_exception[0] == "FixtureLookupError":
        assert observations["pytest"][1] == ["outer teardown"]
        assert observations["pytest"][3]
        assert "not available during teardown because it was not previously requested" in str(
            stock_exception[1]
        )
    else:
        assert stock_exception == ["AssertionError", None]
        assert observations["pytest"][1] == ["outer teardown", "resource setup"]
        assert not observations["pytest"][3]
    assert observations["pytest-native"] == observations["pytest"]


def test_first_session_fixture_request_during_function_teardown_is_finalized(
    tmp_path: Path,
) -> None:
    source = """@pytest.fixture(scope='session')
def resource():
    record('resource setup')
    yield 42
    record('resource teardown')

@pytest.fixture
def outer(request):
    yield
    record('outer teardown')
    value = request.getfixturevalue('resource')
    record(f'value {value}')

def test_one(outer):
    pass
"""
    warnings: dict[str, bool] = {}
    for engine in ("pytest", "pytest-native"):
        project, result = _run_case(tmp_path, engine, source)
        transcript = result.stdout + result.stderr
        assert (result.returncode, _events(project)) == (
            0,
            ["outer teardown", "resource setup", "value 42", "resource teardown"],
        ), transcript
        warnings[engine] = (
            'Calling request.getfixturevalue("resource") during teardown is deprecated.'
            in transcript
        )
    assert warnings["pytest-native"] == warnings["pytest"]


def test_function_fixture_reuse_after_teardown_acquisition_failure(tmp_path: Path) -> None:
    source = """@pytest.fixture
def resource():
    record('resource setup')
    yield 42
    record('resource teardown')

@pytest.fixture
def outer(request):
    yield
    record('outer teardown')
    try:
        request.getfixturevalue('resource')
    except Exception as error:
        record(type(error).__name__)

def test_one(outer):
    pass

def test_two(resource):
    record(f'value {resource}')
"""
    observations: dict[str, tuple[list[str], bool]] = {}
    for engine in ("pytest", "pytest-native"):
        project, result = _run_case(tmp_path, engine, source)
        transcript = result.stdout + result.stderr
        assert result.returncode == 0, transcript
        assert "2 passed" in transcript, transcript
        observations[engine] = (
            _events(project),
            'Calling request.getfixturevalue("resource") during teardown is deprecated.'
            in transcript,
        )
    assert observations["pytest"] in (
        (
            [
                "outer teardown",
                "resource setup",
                "AssertionError",
                "value 42",
                "resource teardown",
            ],
            False,
        ),
        (["outer teardown", "resource setup", "AssertionError", "value 42"], False),
        (
            [
                "outer teardown",
                "FixtureLookupError",
                "resource setup",
                "value 42",
                "resource teardown",
            ],
            True,
        ),
    )
    assert observations["pytest-native"] == observations["pytest"]


def test_getfixturevalue_accepts_pytest_argname_keyword(tmp_path: Path) -> None:
    source = """@pytest.fixture
def resource():
    return 42

def test_case(request):
    assert request.getfixturevalue(argname='resource') == 42
    record('keyword lookup')
"""
    for engine in ("pytest", "pytest-native"):
        project, result = _run_case(tmp_path, engine, source)
        assert (result.returncode, _events(project)) == (0, ["keyword lookup"]), (
            result.stdout + result.stderr
        )


@pytest.mark.parametrize("scope", ["function", "module"])
def test_escaped_fixture_request_adds_finalizer_to_current_instance(
    tmp_path: Path, scope: str
) -> None:
    declaration = "" if scope == "function" else ", params=[1, 2]"
    source = f"""saved = []

@pytest.fixture(scope='{scope}'{declaration})
def resource(request):
    saved.append(request)
    record('setup')
    yield 42
    record('teardown')

def test_first(resource):
    pass

def test_second(resource):
    saved[0].addfinalizer(lambda: record('escaped finalizer'))
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project, result = _run_case(tmp_path, engine, source)
        assert result.returncode == 0, result.stdout + result.stderr
        observed[engine] = _events(project)
    assert observed["pytest-native"] == observed["pytest"]
    assert observed["pytest-native"].count("escaped finalizer") == (1 if scope == "function" else 2)


def test_compact_indirect_parameters_survive_saved_request_lookup(tmp_path: Path) -> None:
    source = """saved = []

@pytest.fixture(scope='module')
def resource(request):
    value = request.param
    record(f'setup {value!r}')
    yield value
    record(f'teardown {value!r}')

@pytest.mark.parametrize(
    'resource,other', [(None, 'first'), ('second', 'last')], indirect=['resource']
)
def test_parameter(resource, other, request):
    assert resource is request.node.callspec.params['resource']
    saved.append(request)
    record(f'case {resource!r} {other}')
    if len(saved) == 2:
        assert saved[0].node.callspec.params['resource'] is None
        assert saved[1].node.callspec.params['resource'] == 'second'
        assert saved[0].getfixturevalue('resource') == 'second'
        record('saved lookup')
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project, result = _run_case(tmp_path, engine, source)
        assert result.returncode == 0, result.stdout + result.stderr
        observed[engine] = _events(project)
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == [
            "setup None",
            "case None first",
            "teardown None",
            "setup 'second'",
            "case 'second' last",
            "saved lookup",
            "teardown 'second'",
        ]
    )
