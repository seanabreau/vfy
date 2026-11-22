"""Compare public fixture bindings and early-stop cleanup with pytest."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_function_funcargs_include_eager_fixtures_during_setup_and_call(tmp_path: Path) -> None:
    source = """import pytest
@pytest.fixture(autouse=True)
def automatic():
    return 'automatic'
@pytest.fixture
def value():
    return 42
@pytest.fixture
def observer(request):
    assert request.node.funcargs['value'] == 42
    assert request.node.funcargs['automatic'] == 'automatic'
def test_case(value, observer, request):
    assert request.node.funcargs['value'] == value
    assert request.node.funcargs['request'] is request
    request.addfinalizer(lambda: request.node.funcargs.__getitem__('value'))
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope=function\n")
        project.write("test_case.py", source)
        result = (
            project.run_pytest("-p", "vfy.asyncio_plugin", "-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--workers=1", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("expected_failure", [False, True], ids=["grouped-error", "xfail"])
@pytest.mark.parametrize("workers", [1, 2])
def test_call_failure_finalizes_all_scopes_before_classifying_teardown(
    tmp_path: Path, expected_failure: bool, workers: int
) -> None:
    marker = "@pytest.mark.xfail(strict=True, reason='known')\n" if expected_failure else ""
    body = "pass" if expected_failure else "assert False"
    release = tuple(int(part) for part in pytest.__version__.split(".")[:2])
    deferred_cleanup = (8, 1) <= release < (8, 3)
    source = f"""from pathlib import Path
import pytest
@pytest.fixture(scope='session')
def shared():
    yield
    raise ValueError('session failure')
@pytest.fixture
def local():
    yield
    raise ValueError('function failure')
{marker}def test_first(shared, local):
    {body}
def test_second(shared):
    Path('second-ran.txt').touch()
    pytest.fail('must not run')
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope=function\n")
        project.write("test_case.py", source)
        if workers == 2:
            project.write("test_other.py", "def test_other():\n    pass\n")
        options = ("-q", "-x", "--junitxml=result.xml")
        result = (
            project.run_pytest("-p", "vfy.asyncio_plugin", *options, load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                f"--workers={workers}",
                "--native-scheduling=file",
                "--no-cov",
                *options,
            )
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert not (project.root / "second-ran.txt").exists(), result.stdout + result.stderr
        junit_cases = list(ET.parse(project.root / "result.xml").iter("testcase"))
        assert all(case.get("name") != "test_second" for case in junit_cases)
        observed[engine] = [
            [
                (child.tag, child.get("message"))
                for child in case
                if child.tag in {"failure", "error", "skipped"}
            ]
            for case in junit_cases
            if case.get("name") == "test_first"
        ]
        assert observed[engine], result.stdout + result.stderr
        if not expected_failure and (engine == "pytest-native" or not deferred_cleanup):
            assert "function failure" in result.stdout
            assert "session failure" in result.stdout
    assert [tag for case in observed["pytest-native"] for tag, _message in case] == [
        "failure",
        "skipped" if expected_failure else "error",
    ]
    if not expected_failure:
        error_messages = [
            message for case in observed["pytest-native"] for tag, message in case if tag == "error"
        ]
        assert len(error_messages) == 1
        assert error_messages[0] is not None
        assert "ExceptionGroup" in error_messages[0]
        assert "2 sub-exceptions" in error_messages[0]
    if deferred_cleanup:
        # Pytest 8.1-8.2 report function teardown before session cleanup. Native
        # retains both teardown errors in one group; keep that bounded difference visible.
        if expected_failure:
            expected = [[("failure", "[XPASS(strict)] known"), ("skipped", "known")]]
            assert observed["pytest"] == expected
            assert observed["pytest-native"] == expected
        else:
            assert observed["pytest"] == [
                [("failure", "assert False")],
                [("error", 'failed on teardown with "ValueError: function failure"')],
            ]
            assert observed["pytest-native"] == [
                [("failure", "assert False")],
                [
                    (
                        "error",
                        'failed on teardown with "ExceptionGroup: errors during test teardown '
                        '(2 sub-exceptions)"',
                    )
                ],
            ]
    else:
        assert observed["pytest-native"] == observed["pytest"]


def test_package_owner_callback_can_reenter_cached_request(tmp_path: Path) -> None:
    # Exercise installed pytest Node.listchain while the native fixture owner resolves it.
    source = """from pathlib import Path
import pytest
@pytest.fixture
def ready():
    return 42
@pytest.fixture(scope='package')
def package_resource():
    return 9
def test_case(ready, request):
    original = request.node.listchain
    def listchain():
        Path('callback-entered.txt').touch()
        assert request.getfixturevalue('ready') == ready
        return original()
    request.node.listchain = listchain
    try:
        assert request.getfixturevalue('package_resource') == 9
    finally:
        request.node.listchain = original
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine, timeout=10)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope=function\n")
        project.write("test_case.py", source)
        result = (
            project.run_pytest("-p", "vfy.asyncio_plugin", "-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if engine == "pytest-native":
            assert (project.root / "callback-entered.txt").exists()


def test_replaced_direct_parameter_fixture_receives_request(tmp_path: Path) -> None:
    # Replace installed pytest private _fixtureinfo.name2fixturedefs before native invocation.
    source = """import pytest
@pytest.fixture(autouse=True)
def changer(request):
    def replacement(request):
        return request.param + 1
    request.node._fixtureinfo.name2fixturedefs['value'][-1].func = replacement
@pytest.mark.parametrize('value, expected', [(1, 2), (2, 3)])
def test_case(value, expected):
    assert value == expected
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope=function\n")
        project.write("test_case.py", source)
        result = (
            project.run_pytest("-p", "vfy.asyncio_plugin", "-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_teardown_failure_stopping_the_run_still_finalizes_broader_fixtures(
    tmp_path: Path,
) -> None:
    source = """from pathlib import Path
import pytest
def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')
@pytest.fixture(scope='session')
def shared():
    yield
    record('session teardown')
@pytest.fixture
def local():
    yield
    record('function teardown')
    raise ValueError('function failure')
def test_first(shared, local):
    pass
def test_second(shared):
    record('must not run')
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope=function\n")
        project.write("test_case.py", source)
        result = (
            project.run_pytest("-p", "vfy.asyncio_plugin", "-q", "-x", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--workers=1", "--no-cov", "-q", "-x")
        )
        assert result.returncode == 1, result.stdout + result.stderr
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert (
        observed["pytest-native"] == observed["pytest"] == ["function teardown", "session teardown"]
    )
