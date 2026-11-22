"""Compare native outcome and request lifecycles with the installed pytest oracle."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _compare_outcomes(
    tmp_path: Path, source: str, expected_status: int | None, *options: str
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        arguments = ("-q", "--junitxml=result.xml", *options)
        result = (
            project.run_pytest(*arguments, load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=pytest-native", "--workers=1", "--no-cov", *arguments
            )
        )
        if expected_status is None:
            expected_status = result.returncode
        assert result.returncode == expected_status, result.stdout + result.stderr
        observed[engine] = [
            (
                case.get("name"),
                [child.tag for child in case if child.tag in {"failure", "error", "skipped"}],
            )
            for case in ET.parse(project.root / "result.xml").iter("testcase")
        ]
    assert observed["pytest-native"] == observed["pytest"]


def test_skip_conditions_observe_prior_test_execution(tmp_path: Path) -> None:
    _compare_outcomes(
        tmp_path,
        """import pytest
ready = False
def test_prepare():
    global ready
    ready = True
@pytest.mark.skipif("not ready", reason="not ready")
def test_must_fail():
    assert False
""",
        1,
    )


@pytest.mark.parametrize("phase", ["setup", "call"])
@pytest.mark.parametrize("run", [True, False])
def test_dynamic_xfail_marks_match_pytest(tmp_path: Path, phase: str, run: bool) -> None:
    marker = f'request.node.add_marker(pytest.mark.xfail(reason="expected", run={run}))'
    _compare_outcomes(
        tmp_path,
        f"""import pytest
from pathlib import Path
@pytest.fixture
def mark(request):
    {marker if phase == "setup" else "pass"}
def test_failure(mark, request):
    {marker if phase == "call" else "pass"}
    Path("called").touch()
    assert False
""",
        0,
    )
    assert (tmp_path / "pytest-native/called").exists() == (tmp_path / "pytest/called").exists()


def test_invalid_skip_condition_is_a_setup_error(tmp_path: Path) -> None:
    _compare_outcomes(
        tmp_path,
        """import pytest
def test_first():
    pass
@pytest.mark.skipif("unknown", reason="bad condition")
def test_second():
    pass
""",
        1,
    )


@pytest.mark.parametrize("exception", ["ValueError", "TypeError"])
def test_xfail_matches_teardown_exception(tmp_path: Path, exception: str) -> None:
    _compare_outcomes(
        tmp_path,
        f"""import pytest
@pytest.fixture
def resource():
    yield
    raise {exception}("teardown")
@pytest.mark.xfail(raises=ValueError, reason="expected teardown")
def test_case(resource):
    pass
""",
        0 if exception == "ValueError" else 1,
    )


def test_maxfail_preserves_session_finalizer_exit(tmp_path: Path) -> None:
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_case.py",
            """import pytest
@pytest.fixture(scope="session")
def resource():
    yield
    pytest.exit("finalizer exit", returncode=7)
def test_failure(resource):
    assert False
def test_unused(resource):
    raise AssertionError("must not run")
""",
        )
        result = (
            project.run_pytest("-q", "-x", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q", "-x"
            )
        )
        assert result.returncode == 7, result.stdout + result.stderr
        assert "finalizer exit" in result.stdout + result.stderr


def test_maxfail_retains_finalizer_output_and_warnings(tmp_path: Path) -> None:
    source = """import logging
import warnings
import pytest
@pytest.fixture(scope="session")
def resource():
    yield
    print("FINALIZER_STDOUT")
    logging.warning("FINALIZER_LOG")
    warnings.warn("FINALIZER_WARNING", UserWarning)
def test_failure(resource):
    assert False
def test_unused(resource):
    raise AssertionError("must not run")
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        args = ("-q", "-x", "--junitxml=result.xml", "-o", "junit_logging=all")
        result = (
            project.run_pytest(*args, load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", *args)
        )
        assert result.returncode == 1, result.stdout + result.stderr
        xml = (project.root / "result.xml").read_text()
        # Pytest 8.1/8.2 defer session teardown past the last report on maxfail;
        # 8.0 and 8.3+ clear nextitem before teardown and retain this capture.
        release = tuple(int(part) for part in pytest.__version__.split(".")[:2])
        retains_capture = engine == "pytest-native" or not (8, 1) <= release < (8, 3)
        assert ("FINALIZER_STDOUT" in xml) == retains_capture, xml
        assert ("FINALIZER_LOG" in xml) == retains_capture, xml
        assert "FINALIZER_WARNING" in result.stdout, result.stdout + result.stderr


def test_phase_failures_preserve_tracebacks_with_deprecations_as_errors(tmp_path: Path) -> None:
    source = """import pytest
@pytest.fixture
def setup_failure():
    raise ValueError("SETUP_FAILURE")
@pytest.fixture
def teardown_failure():
    yield
    raise ValueError("TEARDOWN_FAILURE")
def test_setup(setup_failure):
    pass
def test_call():
    raise ValueError("CALL_FAILURE")
def test_teardown(teardown_failure):
    pass
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope = function\n")
        project.write("test_case.py", source)
        args = ("-q", "-W", "error::DeprecationWarning")
        result = (
            project.run_pytest("-p", "vfy.asyncio_plugin", *args, load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", *args)
        )
        transcript = result.stdout + result.stderr
        assert result.returncode == 1, transcript
        assert "1 failed, 1 passed, 2 errors" in transcript, transcript
        for phase in ("SETUP", "CALL", "TEARDOWN"):
            assert f'raise ValueError("{phase}_FAILURE")' in transcript, transcript
            assert f"ValueError: {phase}_FAILURE" in transcript, transcript
        assert "signature of throw() is deprecated" not in transcript, transcript


def test_finalizer_can_retrieve_its_cached_fixture(tmp_path: Path) -> None:
    _compare_outcomes(
        tmp_path,
        """import pytest
@pytest.fixture
def value(request):
    result = object()
    yield result
    assert request.getfixturevalue("value") is result
def test_case(value):
    pass
""",
        0,
    )


def test_request_applymarker_preserves_node_markers(tmp_path: Path) -> None:
    _compare_outcomes(
        tmp_path,
        """import pytest
@pytest.fixture
def marked(request):
    request.applymarker(pytest.mark.skip(reason="fixture marker"))
    assert request.node.get_closest_marker("skip").kwargs["reason"] == "fixture marker"
def test_case(marked):
    pass
""",
        0,
    )


def test_dynamic_request_lookup_returns_request_identity(tmp_path: Path) -> None:
    _compare_outcomes(
        tmp_path,
        """import pytest
@pytest.fixture
def value(request):
    assert request.getfixturevalue("request") is request
    return 42
def test_case(value, request):
    assert request.getfixturevalue("request") is request
    assert value == 42
""",
        0,
    )


def test_marker_condition_output_is_captured_in_setup(tmp_path: Path) -> None:
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_case.py",
            """import pytest
@pytest.mark.skipif("print('CONDITION_OUTPUT') or False", reason="condition")
def test_case():
    pass
""",
        )
        arguments = ("-q", "--junitxml=result.xml", "-o", "junit_logging=all")
        result = (
            project.run_pytest(*arguments, load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=pytest-native", "--workers=1", "--no-cov", *arguments
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "CONDITION_OUTPUT" in (project.root / "result.xml").read_text()
        assert "CONDITION_OUTPUT" not in result.stdout


def test_runxfail_does_not_reclassify_teardown(tmp_path: Path) -> None:
    _compare_outcomes(
        tmp_path,
        """import pytest
@pytest.fixture
def resource():
    yield
    raise ValueError("teardown")
@pytest.mark.xfail(raises=ValueError, reason="expected")
def test_case(resource):
    pass
""",
        1,
        "--runxfail",
    )


def test_request_raiseerror_preserves_exception_and_fixture_diagnostic(tmp_path: Path) -> None:
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_request.py",
            """import pytest
@pytest.fixture
def unavailable(request):
    request.raiseerror("resource is unavailable")
def test_error(unavailable):
    pass
def test_request(request):
    with pytest.raises(pytest.FixtureLookupError) as caught:
        request.raiseerror(msg="custom message")
    assert caught.value.msg == "custom message"
    assert caught.value.request is request
    with pytest.raises(pytest.FixtureLookupError) as caught:
        request.raiseerror(None)
    assert caught.value.msg is None
""",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
        )
        result.expect(1)
        assert "1 passed, 1 error" in result.stdout
        assert "resource is unavailable" in result.stdout
        assert "AttributeError" not in result.stdout


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
@pytest.mark.parametrize("unittest_enabled", [True, False], ids=["unittest", "no-unittest"])
def test_unittest_skiptest_normalization_matches_builtin_policy(
    tmp_path: Path, phase: str, unittest_enabled: bool
) -> None:
    exception = 'raise SkipTest("optional library unavailable")'
    source = f"""import pytest
from unittest import SkipTest

@pytest.fixture
def resource():
    {exception if phase == "setup" else "pass"}
    yield
    {exception if phase == "teardown" else "pass"}

def test_case(resource):
    {exception if phase == "call" else "pass"}
"""
    options = ("-rs",) if unittest_enabled else ("-p", "no:unittest")
    _compare_outcomes(tmp_path, source, None if unittest_enabled else 1, *options)
    if unittest_enabled:
        messages = [
            [
                skip.get("message")
                for skip in ET.parse(tmp_path / engine / "result.xml").iter("skipped")
            ]
            for engine in ("pytest", "pytest-native")
        ]
        assert messages[0] == messages[1]
        if messages[0]:
            assert messages[0] == ["optional library unavailable"]


@pytest.mark.parametrize("parametrized", [False, True], ids=["ordinary", "compact-family"])
def test_fixture_replacing_test_callable_executes_the_live_item(
    tmp_path: Path, parametrized: bool
) -> None:
    source = f"""import pytest
from pathlib import Path

@pytest.fixture
def replace(request):
    def replacement(replace):
        Path("replacement-ran").touch()
        raise RuntimeError("fixture replacement failure")
    request.node.obj = replacement

{"@pytest.mark.parametrize('case', [1, 2])" if parametrized else ""}
def test_case(replace{", case" if parametrized else ""}):
    Path("stale-body-ran").touch()
"""
    # The replacement receives the original fixture arguments, as in pytest.
    if parametrized:
        source = source.replace("def replacement(replace):", "def replacement(replace, case):")
    _compare_outcomes(tmp_path, source, 1)
    for engine in ("pytest", "pytest-native"):
        project = tmp_path / engine
        assert (project / "replacement-ran").exists()
        assert not (project / "stale-body-ran").exists()


@pytest.mark.parametrize("strict", [False, True], ids=["xpass", "strict-xpass"])
def test_failed_tmp_path_retention_uses_the_pytest_call_outcome(
    tmp_path: Path, strict: bool
) -> None:
    source = f"""from pathlib import Path
import pytest

@pytest.mark.xfail(reason="legacy expectation", strict={strict})
def test_a_unexpected_success(tmp_path):
    Path("fixture-path.txt").write_text(str(tmp_path))

def test_b_inspects_fixture_cleanup():
    fixture_path = Path(Path("fixture-path.txt").read_text())
    assert fixture_path.exists() is {strict}
"""
    results = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\ntmp_path_retention_policy=failed\n")
        project.write("test_case.py", source)
        results[engine] = (
            project.run_pytest("-q", "--basetemp=temps", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=native", "--workers=1", "--no-cov", "-q", "--basetemp=temps"
            )
        )
    for result in results.values():
        assert result.returncode == (1 if strict else 0), result.stdout + result.stderr
        assert "1 passed" in result.stdout, result.stdout


@pytest.mark.parametrize("explicit_basetemp", [False, True], ids=["automatic", "explicit"])
@pytest.mark.parametrize("failed", [False, True], ids=["passed", "failed"])
def test_failed_tmp_path_policy_cleans_only_successful_automatic_session_bases(
    tmp_path: Path, explicit_basetemp: bool, failed: bool
) -> None:
    observations = []
    for engine in ("stock", "default"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\ntmp_path_retention_policy=failed\n")
        project.write("system-temp/.keep", "")
        project.write(
            "test_paths.py",
            "import json\nfrom pathlib import Path\n"
            "def test_result(tmp_path_factory, tmp_path):\n"
            "    Path('paths.json').write_text(json.dumps([\n"
            "        str(tmp_path_factory.getbasetemp()), str(tmp_path)]))\n"
            f"    assert {not failed}\n",
        )
        arguments = ("--basetemp=explicit-base",) if explicit_basetemp else ()
        environment = {"PYTEST_DEBUG_TEMPROOT": str(project.root / "system-temp")}
        result = (
            project.run_pytest("-q", *arguments, load_xdist=False, env=environment)
            if engine == "stock"
            else project.run_vfy(
                "test", "--engine=native", "--no-cov", "-q", *arguments, env=environment
            )
        )
        assert result.returncode == int(failed), result.stdout + result.stderr
        base, path = json.loads((project.root / "paths.json").read_text())
        observations.append((Path(base).exists(), Path(path).exists()))
    assert observations == [(explicit_basetemp or failed, failed)] * 2


def test_parallel_failed_tmp_path_retention_uses_the_assigned_case_origin(tmp_path: Path) -> None:
    observations = []
    for engine in ("stock", "default"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\ntmp_path_retention_policy=failed\n")
        for name, passed in (("a_pass", True), ("b_fail", False)):
            project.write(
                f"test_{name}.py",
                "import json, os\nfrom pathlib import Path\n"
                "def test_result(tmp_path):\n"
                f"    Path('{name}.json').write_text(json.dumps([str(tmp_path), os.getpid()]))\n"
                f"    assert {passed}\n",
            )
        result = (
            project.run_pytest("-n", "2", "--dist=loadfile", "--basetemp=temps", "-q")
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=2",
                "--no-cov",
                "--basetemp=temps",
                "--native-result-json=owned.json",
                "-q",
            )
        )
        assert result.returncode == 1, result.stdout + result.stderr
        records = [
            json.loads((project.root / f"{name}.json").read_text()) for name in ("a_pass", "b_fail")
        ]
        assert len({pid for _, pid in records}) == 2
        if engine == "default":
            assert (
                json.loads((project.root / "owned.json").read_text())["engine"]
                == "vfy-independent-v1"
            )
        observations.append([Path(path).exists() for path, _ in records])
    assert observations == [[False, True], [False, True]]


@pytest.mark.parametrize("control", ["interrupt", "exit"])
@pytest.mark.parametrize("prior_case", [False, True])
def test_control_exceptions_retain_partial_warnings(
    tmp_path: Path, control: str, prior_case: bool
) -> None:
    action = (
        'raise KeyboardInterrupt("test interrupt")'
        if control == "interrupt"
        else 'pytest.exit("test exit")'
    )
    source = f"""import warnings
from pathlib import Path
import pytest

@pytest.fixture(scope="session", autouse=True)
def resource():
    yield
    Path("finalized.txt").touch()
    warnings.warn("cleanup warning", UserWarning)

{"def test_a_prior(): warnings.warn('completed warning', UserWarning)" if prior_case else ""}

def test_z_control():
    warnings.warn("control warning", UserWarning)
    {action}
"""
    observed_warnings = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--tb=no",
            env={"PYTEST_NATIVE_RESULT": str(project.root / "result.json")}
            if engine == "pytest-native"
            else None,
        )
        result.expect(2)
        assert (project.root / "finalized.txt").exists()
        assert f"test {control}" in result.stdout
        observed_warnings[engine] = re.findall(
            r"UserWarning: (completed warning|control warning|cleanup warning)", result.stdout
        )
        assert f"{2 + int(prior_case)} warnings in" in result.stdout, result.stdout
    expected_warnings = (["completed warning"] if prior_case else []) + [
        "control warning",
        "cleanup warning",
    ]
    assert observed_warnings["pytest-native"] == observed_warnings["pytest"] == expected_warnings
    record = json.loads((tmp_path / "pytest-native/result.json").read_text())
    assert len(record["cases"]) == int(prior_case)
    assert record["counts"]["passed"] == int(prior_case)
    assert len(record["warnings"]) == 2 + int(prior_case)
    assert all(record["counts"][outcome] == 0 for outcome in ("failed", "errors"))


def test_phase_hooks_observe_mutation_registration_and_callback_destruction(tmp_path: Path) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """from pathlib import Path
from types import ModuleType

def record(event):
    with Path("events").open("a") as stream:
        stream.write(event + "\\n")

class HookPath(str):
    def __del__(self):
        record("drop:path")

class Callback:
    def __init__(self, name):
        self.name = name
    def __call__(self, *args):
        record(self.name + ":" + ",".join(item.name for item in args))
    def __del__(self):
        record("drop:" + self.name)

def pytest_configure(config):
    plugin = ModuleType("dynamic")
    plugin.__file__ = "dynamic/conftest.py"
    plugin.pytest_runtest_setup = lambda item: record("dynamic:" + item.name)
    config.pluginmanager.register(plugin, "dynamic")

def pytest_runtest_setup(item):
    record("original:" + item.name)

def pytest_runtest_teardown(item):
    record("teardown:" + item.name)
""",
        )
        project.write(
            "test_case.py",
            """import conftest

def current(request):
    return next(impl for impl in request.config.hook.pytest_runtest_setup.get_hookimpls()
                if impl.plugin is conftest)

def test_first(request):
    impl = current(request)
    impl.function = conftest.Callback("empty")
    impl.argnames = ()
    conftest.record("first")

def test_second(request):
    impl = current(request)
    impl.function = conftest.Callback("item")
    impl.argnames = ["item"]
    conftest.record("second")

def test_third(request):
    impl = current(request)
    impl.function = conftest.Callback("empty-again")
    impl.argnames.clear()
    plugin = request.config.pluginmanager.get_plugin("dynamic")
    plugin.__file__ = conftest.HookPath("dynamic/conftest.py")
    conftest.record("third")

def test_fourth(request):
    impl = current(request)
    impl.function = conftest.pytest_runtest_setup
    impl.argnames = ("item",)
    manager = request.config.pluginmanager
    manager.get_plugin("dynamic").__file__ = "dynamic/conftest.py"
    manager.unregister(manager.get_plugin("dynamic"))
    conftest.record("fourth")

def test_fifth():
    conftest.record("fifth")
""",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observed[engine] = (project.root / "events").read_text().splitlines()
    assert observed["pytest-native"] == observed["pytest"]
    assert "drop:empty" in observed["pytest"]
    assert "drop:item" in observed["pytest"]
    assert "drop:empty-again" in observed["pytest"]
    assert "drop:path" in observed["pytest"]
    assert "dynamic:test_fourth" in observed["pytest"]
    assert "dynamic:test_fifth" not in observed["pytest"]
