"""Exercise unittest setup, calls, and teardown through the vfy CLI."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native runner requires Unix",
)


def _pytest_version() -> tuple[int, int]:
    return int(pytest.version_tuple[0]), int(pytest.version_tuple[1])


def _unittest_project_source() -> str:
    return """\
import json
from pathlib import Path
from unittest import TestCase, expectedFailure

import pytest

events = []
next_instance = 0

@pytest.fixture(scope="module", autouse=True)
def module_resource():
    events.append(["module-setup"])
    yield
    events.append(["module-finalizer"])
    Path("unittest-events.json").write_text(json.dumps(events), encoding="utf-8")

@pytest.fixture(scope="class", autouse=True)
def class_resource(request):
    if pytest.version_tuple[:2] < (8, 2):
        assert request.instance is not None
    else:
        assert request.instance is None
    events.append(["class-setup"])
    yield
    events.append(["class-finalizer"])

@pytest.fixture(autouse=True)
def instance_resource(request):
    global next_instance
    instance = request.instance
    assert instance is not None
    next_instance += 1
    instance._vfy_instance_token = next_instance
    events.append(["fixture-setup", next_instance])
    yield
    assert request.instance is instance
    events.append(["fixture-finalizer", next_instance])

class TestLifecycle(TestCase):
    def setUp(self):
        events.append(["setUp", self._testMethodName])
        self.addCleanup(self.record_cleanup)
        if self._testMethodName == "test_setup_and_cleanup_failures":
            self.addCleanup(self.fail_cleanup)
            raise RuntimeError("unittest setup failure")

    def tearDown(self):
        events.append(["tearDown", self._testMethodName])
        if self._testMethodName == "test_teardown_failure":
            raise RuntimeError("unittest teardown failure")

    def record_cleanup(self):
        events.append(["cleanup", self._testMethodName])

    def fail_cleanup(self):
        events.append(["cleanup-failure", self._testMethodName])
        raise RuntimeError("unittest cleanup failure")

    def test_passes_and_preserves_instance(self):
        assert self._vfy_instance_token
        events.append(["test", self._testMethodName, self._vfy_instance_token])

    def test_setup_and_cleanup_failures(self):
        raise AssertionError("setUp failure should prevent the test method")

    def test_teardown_failure(self):
        events.append(["test", self._testMethodName, self._vfy_instance_token])

    def test_cleanup_failure(self):
        self.addCleanup(self.fail_cleanup)
        events.append(["test", self._testMethodName, self._vfy_instance_token])

    def test_skip(self):
        self.skipTest("unittest skip")

    @expectedFailure
    def test_expected_failure(self):
        self.fail("expected unittest failure")

class TestClassLifecycle(TestCase):
    @classmethod
    def setUpClass(cls):
        events.append(["setUpClass", cls.__name__])
        cls.addClassCleanup(cls.fail_class_cleanup)

    @classmethod
    def tearDownClass(cls):
        events.append(["tearDownClass", cls.__name__])
        raise RuntimeError("unittest class teardown failure")

    @classmethod
    def fail_class_cleanup(cls):
        events.append(["class-cleanup-failure", cls.__name__])
        raise RuntimeError("unittest class cleanup failure")

    def test_class_teardown_and_cleanup_failures(self):
        events.append(["test", self._testMethodName, self._vfy_instance_token])

class TestClassSetupFailure(TestCase):
    @classmethod
    def setUpClass(cls):
        events.append(["setUpClass", cls.__name__])
        cls.addClassCleanup(cls.fail_class_cleanup)
        raise RuntimeError("unittest class setup failure")

    @classmethod
    def fail_class_cleanup(cls):
        events.append(["class-cleanup-failure", cls.__name__])
        raise RuntimeError("unittest class setup cleanup failure")

    def test_class_setup_failure(self):
        raise AssertionError("setUpClass failure should prevent the test method")
"""


def _normalized_summary(output: str) -> str:
    summaries = re.findall(r"(?m)^[= ]*((?:\d+ .+|no tests ran) in [0-9.]+s)[= ]*$", output)
    assert summaries, output
    summary = re.sub(r" in [0-9.]+s$", "", summaries[-1])
    assert isinstance(summary, str)
    return summary


def test_native_unittest_lifecycle_matches_stock_vfy(tmp_path: Path) -> None:
    projects: dict[str, VfyProject] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_unittest.py", _unittest_project_source())
        projects[engine] = project

    results = {
        engine: project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--tb=no",
        )
        for engine, project in projects.items()
    }
    stock = results["pytest"]
    native = results["pytest-native"]
    assert stock.returncode == native.returncode == 1, (
        stock.stdout + stock.stderr + native.stdout + native.stderr
    )
    assert _normalized_summary(stock.stdout) == _normalized_summary(native.stdout)

    traces = {
        engine: json.loads((project.root / "unittest-events.json").read_text(encoding="utf-8"))
        for engine, project in projects.items()
    }
    assert traces["pytest-native"] == traces["pytest"]
    assert [event[0] for event in traces["pytest-native"]].count("fixture-finalizer") == 7


@pytest.mark.parametrize(
    ("verbosity", "capture"),
    [("-q", True), ("-vv", True), ("-q", False), ("-vv", False)],
)
def test_native_unittest_subtests_match_stock_vfy(
    tmp_path: Path, verbosity: str, capture: bool
) -> None:
    source = """\
import json
from pathlib import Path
from unittest import TestCase

class TestSubtests(TestCase):
    def test_subtests(self):
        outcomes = []
        Path("subtest-body-ran.txt").write_text("ran", encoding="utf-8")
        with self.subTest(value="passing"):
            self.assertEqual(1, 1)
        outcomes.append("passing")
        with self.subTest(value="failing"):
            self.assertEqual(1, 2)
        outcomes.append("failing")
        with self.subTest(value="skipped"):
            self.skipTest("subtest skipped")
        outcomes.append("skipped")
        Path("subtest-events.json").write_text(json.dumps(outcomes), encoding="utf-8")
"""
    stock = VfyProject(tmp_path / "stock")
    native = VfyProject(tmp_path / "native")
    for project in (stock, native):
        project.write("test_subtest.py", source)

    output_options = [verbosity, "--tb=no"]
    if not capture:
        output_options.append("-s")
    stock_result = stock.run_vfy(
        "test", "--engine=pytest", "--workers=1", "--no-cov", *output_options
    )
    result_path = native.root / "native-result.json"
    native_result = native.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=1",
        "--no-cov",
        *output_options,
        env={"PYTEST_NATIVE_RESULT": str(result_path)},
    )
    stock_result.expect(1)
    assert native_result.returncode == stock_result.returncode, (
        stock_result.stdout + stock_result.stderr + native_result.stdout + native_result.stderr
    )
    assert _normalized_summary(native_result.stdout) == _normalized_summary(stock_result.stdout)
    assert _unittest_subtest_progress(
        native_result.stdout, verbosity
    ) == _unittest_subtest_progress(stock_result.stdout, verbosity)
    if _pytest_version() < (9, 0):
        for project in (stock, native):
            assert (project.root / "subtest-body-ran.txt").is_file()
            assert not (project.root / "subtest-events.json").exists()
        for status in ("SUBPASSED", "SUBFAILED", "SUBSKIPPED"):
            assert stock_result.stdout.count(status) == 0
            assert native_result.stdout.count(status) == 0
        raw = json.loads(result_path.read_text(encoding="utf-8"))
        case = next(case for case in raw["cases"] if case["nodeid"].endswith("test_subtests"))
        assert case["phases"][-2]["outcome"] == "failed"
        assert case["subtests"] == []
        assert raw["counts"]["skipped"] == 0
        return

    for project in (stock, native):
        assert (project.root / "subtest-body-ran.txt").is_file()
        assert json.loads((project.root / "subtest-events.json").read_text(encoding="utf-8")) == [
            "passing",
            "failing",
            "skipped",
        ]
    for status in ("SUBPASSED", "SUBFAILED", "SUBSKIPPED"):
        stock_count = stock_result.stdout.count(status)
        assert native_result.stdout.count(status) == stock_count, native_result.stdout
    if not capture and verbosity == "-vv":
        assert stock_result.stdout.count("SUBPASSED") >= 1
        assert stock_result.stdout.count("SUBFAILED") >= 2
        assert stock_result.stdout.count("SUBSKIPPED") >= 1
    else:
        # Captured terminal output is hidden for passed/skipped subtests; the
        # failure remains visible in pytest's short summary.
        assert stock_result.stdout.count("SUBFAILED") >= 1
        assert stock_result.stdout.count("SUBPASSED") == 0
        assert stock_result.stdout.count("SUBSKIPPED") == 0

    raw = json.loads(result_path.read_text(encoding="utf-8"))
    assert raw["engine"] == "pytest-runner-native-v1"
    case = next(case for case in raw["cases"] if case["nodeid"].endswith("test_subtests"))
    assert case["phases"][-1]["outcome"] == "passed"
    assert [(subtest["outcome"], subtest["context"]) for subtest in case["subtests"]] == [
        ("passed", "(value='passing')"),
        ("failed", "(value='failing')"),
        ("skipped", "(value='skipped')"),
    ]
    assert case["subtests"][1]["exception"]
    assert "AssertionError" in case["subtests"][1]["detail"]
    assert raw["counts"]["skipped"] == 1


def test_native_unittest_subtest_progress_counts_completed_cases(tmp_path: Path) -> None:
    prior_source = """\
def test_first():
    pass

def test_second():
    pass

def test_third():
    pass
"""
    unittest_source = """\
from unittest import TestCase

class TestSubtests(TestCase):
    def test_subtests(self):
        for value in range(100):
            with self.subTest(value=value):
                self.assertEqual(value, value)
"""
    stock = VfyProject(tmp_path / "stock")
    native = VfyProject(tmp_path / "native")
    for project in (stock, native):
        project.write("test_a_prior.py", prior_source)
        project.write("test_z_unittest.py", unittest_source)

    stock_result = stock.run_vfy(
        "test", "--engine=pytest", "--workers=1", "--no-cov", "-q", "--tb=no"
    )
    result_path = native.root / "native-result.json"
    native_result = native.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=1",
        "--no-cov",
        "-q",
        "--tb=no",
        env={"PYTEST_NATIVE_RESULT": str(result_path)},
    )
    assert stock_result.returncode == native_result.returncode == 0, (
        stock_result.stdout + stock_result.stderr + native_result.stdout + native_result.stderr
    )
    assert (
        re.findall(r"\[\s*(\d+)%\]", native_result.stdout)
        == re.findall(r"\[\s*(\d+)%\]", stock_result.stdout)
        == ["100"]
    )

    raw = json.loads(result_path.read_text(encoding="utf-8"))
    assert raw["collected"] == 4
    case = next(case for case in raw["cases"] if case["nodeid"].endswith("test_subtests"))
    call_stdout = next(phase["stdout"] for phase in case["phases"] if phase["phase"] == "call")
    # The subtest hook adds this last item to pytest's progress set; the 100%
    # marker belongs to the terminal output after the native execution loop.
    assert re.search(r"\[\s*\d+%\]", call_stdout) is None


def _unittest_subtest_progress(output: str, verbosity: str) -> str:
    lines = output.splitlines()
    if _pytest_version() < (9, 0):
        if verbosity == "-q":
            return lines[0]
        nodeid = "test_subtest.py::TestSubtests::test_subtests"
        return next(line for line in lines if line.startswith(nodeid))
    if verbosity == "-q":
        start = next(index for index, line in enumerate(lines) if line.startswith((".", "uu-")))
    else:
        nodeid = "test_subtest.py::TestSubtests::test_subtests"
        start = next(index for index, line in enumerate(lines) if line.startswith(nodeid))
    end = next(
        index
        for index, line in enumerate(lines[start:], start)
        if line.startswith("=") and "short test summary info" in line
    )
    return "\n".join(lines[start:end])


@pytest.mark.skipif(_pytest_version() < (9, 0), reason="pytest 9 reports unittest subtests")
@pytest.mark.parametrize("workers", ["1", "2"])
@pytest.mark.parametrize(
    ("strict", "raises", "subtest_body", "runxfail", "expected_outcome", "subtest_verbosity"),
    [
        (False, "None", 'self.fail("expected subtest failure")', False, "xfailed", "1"),
        (True, "None", 'self.fail("expected subtest failure")', False, "xfailed", "1"),
        (False, "ValueError", 'raise ValueError("expected error")', False, "xfailed", "1"),
        (False, "ValueError", 'raise TypeError("unexpected error")', False, "failed", "1"),
        (False, "None", "pass", False, "xpassed", "1"),
        (True, "None", "pass", False, "failed", "1"),
        (False, "None", 'self.fail("ordinary failure")', True, "failed", "1"),
        (True, "None", "pass", True, "passed", "1"),
        (True, "None", 'self.fail("expected subtest failure")', False, "xfailed", "0"),
        (False, "None", "pass", False, "xpassed", "0"),
        (True, "None", "pass", False, "failed", "0"),
    ],
    ids=[
        "expected-failure",
        "strict-expected-failure",
        "matching-raises",
        "nonmatching-raises",
        "unexpected-success",
        "strict-unexpected-success",
        "runxfail-failure",
        "runxfail-success",
        "quiet-expected-failure",
        "quiet-unexpected-success",
        "quiet-strict-unexpected-success",
    ],
)
def test_unittest_subtests_apply_the_parent_xfail_policy(
    tmp_path: Path,
    workers: str,
    strict: bool,
    raises: str,
    subtest_body: str,
    runxfail: bool,
    expected_outcome: str,
    subtest_verbosity: str,
) -> None:
    source = f"""import pytest
from unittest import TestCase

@pytest.mark.xfail(reason="parent xfail reason", strict={strict}, raises={raises})
class TestSubtests(TestCase):
    def test_case(self):
        with self.subTest(value="case"):
            {subtest_body}
"""
    results = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_subtest.py", source)
        project.write("test_other.py", "def test_other(): pass\n")
        options = ["-q", "--tb=no", "-rfxX", "-o", f"verbosity_subtests={subtest_verbosity}"]
        if runxfail:
            options.append("--runxfail")
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            f"--workers={workers if engine == 'pytest-native' else '1'}",
            "--no-cov",
            *options,
            env={"PYTEST_NATIVE_RESULT": str(project.root / "result.json")}
            if engine == "pytest-native"
            else None,
        )
        results[engine] = result
    stock, native = results["pytest"], results["pytest-native"]
    assert native.returncode == stock.returncode, stock.stdout + native.stdout + native.stderr
    assert _normalized_summary(native.stdout) == _normalized_summary(stock.stdout)
    short_lines = r"(?m)^(?:SUBFAILED\S*|SUBXFAIL\S*|XFAIL|XPASS|FAILED) .+$"
    assert sorted(re.findall(short_lines, native.stdout)) == sorted(
        re.findall(short_lines, stock.stdout)
    ), stock.stdout + native.stdout
    record = json.loads((tmp_path / "pytest-native/result.json").read_text())
    case = next(
        case for case in record["cases"] if case["nodeid"].endswith("TestSubtests::test_case")
    )
    subtest = case["subtests"][0]
    assert subtest["outcome"] == expected_outcome, subtest
    if expected_outcome == "xfailed":
        assert subtest["exception"] == "parent xfail reason"
    if strict and subtest_body == "pass" and not runxfail:
        assert subtest["exception"] == "[XPASS(strict)] parent xfail reason"


@pytest.mark.skipif(_pytest_version() < (9, 0), reason="pytest 9 adds unittest subtests")
@pytest.mark.parametrize("workers", ["1", "2"])
@pytest.mark.parametrize("matcher", ["matching", "alternating", "metaclass", "custom", "invalid"])
def test_unittest_xfail_checks_run_once_per_report(
    tmp_path: Path, workers: str, matcher: str
) -> None:
    source = """\
import json
from pathlib import Path
from unittest import TestCase
import pytest

calls = []

@pytest.fixture(scope="session", autouse=True)
def finalizer():
    yield
    Path("calls.json").write_text(json.dumps(calls))

def check(exception):
    calls.append(str(exception))
    CHECK_RESULT

class Meta(type):
    def __instancecheck__(cls, exception):
        return check(exception)

class Expected(Exception, metaclass=Meta):
    pass

from _pytest.raises import AbstractRaises
class CustomMatcher(AbstractRaises):
    def __init__(self):
        super().__init__(match=None, check=None)

    @property
    def fail_reason(self):
        raise AssertionError("diagnostic property must not be read")

    def matches(self, exception):
        return check(exception)

@pytest.mark.xfail(reason="expected", raises=MATCHER)
class TestSubtests(TestCase):
    def test_case(self):
        error = ValueError("same exception")
        for value in range(2):
            with self.subTest(value=value):
                raise error
        raise ValueError("parent exception")
"""
    source = source.replace(
        "CHECK_RESULT", "return len(calls) % 2 == 1" if matcher == "alternating" else "return True"
    ).replace(
        "MATCHER",
        {"metaclass": "Expected", "custom": "CustomMatcher()", "invalid": "42"}.get(
            matcher, "pytest.RaisesExc(ValueError, check=check)"
        ),
    )
    results = {}
    traces = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_subtest.py", source)
        project.write("test_other.py", "def test_other(): pass\n")
        results[engine] = project.run_vfy(
            "test",
            f"--engine={engine}",
            f"--workers={workers if engine == 'pytest-native' else '1'}",
            "--no-cov",
            "-q",
            "--tb=no",
            env={"PYTEST_NATIVE_RESULT": str(project.root / "result.json")}
            if engine == "pytest-native"
            else None,
        )
        traces[engine] = json.loads((project.root / "calls.json").read_text())
    stock, native = results["pytest"], results["pytest-native"]
    assert stock.returncode == native.returncode == int(matcher in ("alternating", "invalid")), (
        stock.stdout + native.stdout + native.stderr
    )
    expected_calls = (
        [] if matcher == "invalid" else ["same exception", "same exception", "parent exception"]
    )
    assert traces["pytest-native"] == traces["pytest"] == expected_calls
    assert _normalized_summary(native.stdout) == _normalized_summary(stock.stdout)
    record = json.loads((tmp_path / "pytest-native/result.json").read_text())
    case = next(case for case in record["cases"] if case["nodeid"].endswith("::test_case"))
    expected_subtests = {
        "matching": ["xfailed", "xfailed"],
        "alternating": ["xfailed", "failed"],
        "metaclass": ["xfailed", "xfailed"],
        "custom": ["xfailed", "xfailed"],
        "invalid": ["failed", "failed"],
    }
    assert [subtest["outcome"] for subtest in case["subtests"]] == expected_subtests[matcher]
    assert case["phases"][1]["outcome"] == ("failed" if matcher == "invalid" else "xfailed")


@pytest.mark.skipif(_pytest_version() < (9, 0), reason="pytest 9 adds unittest subtests")
@pytest.mark.parametrize("callback_error", ["error", "interrupt", "exit"])
@pytest.mark.parametrize("matcher", ["check", "metaclass"])
def test_unittest_xfail_check_exceptions_preserve_cleanup(
    tmp_path: Path, callback_error: str, matcher: str
) -> None:
    action = {
        "error": 'raise RuntimeError("callback error")',
        "interrupt": 'raise KeyboardInterrupt("callback interrupt")',
        "exit": 'pytest.exit("callback exit")',
    }[callback_error]
    source = f"""import json
from pathlib import Path
from unittest import TestCase
import pytest

calls = []

@pytest.fixture(scope="session", autouse=True)
def finalizer():
    yield
    Path("calls.json").write_text(json.dumps(calls))

def check(exception):
    calls.append(str(exception))
    {action}

class Meta(type):
    def __instancecheck__(cls, exception):
        return check(exception)

class Expected(Exception, metaclass=Meta):
    pass

@pytest.mark.xfail(reason="expected", raises=MATCHER)
class TestSubtests(TestCase):
    def test_case(self):
        with self.subTest(value="case"):
            raise ValueError("subtest exception")
        Path("body-continued.txt").write_text("unexpected")
"""
    source = source.replace(
        "MATCHER",
        "Expected" if matcher == "metaclass" else "pytest.RaisesExc(ValueError, check=check)",
    )
    results = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_subtest.py", source)
        results[engine] = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "-q", "--tb=no"
        )
        expected_calls = ["subtest exception"]
        if matcher == "metaclass" and callback_error == "error":
            expected_calls.append("callback error")
        assert json.loads((project.root / "calls.json").read_text()) == expected_calls
        assert not (project.root / "body-continued.txt").exists()
        assert f"callback {callback_error}" in results[engine].stdout
    stock, native = results["pytest"], results["pytest-native"]
    expected_status = (3 if matcher == "metaclass" else 1) if callback_error == "error" else 2
    assert stock.returncode == native.returncode == expected_status, (
        stock.stdout + native.stdout + native.stderr
    )
    if callback_error != "error":
        assert _normalized_summary(native.stdout) == _normalized_summary(stock.stdout)


@pytest.mark.parametrize("workers", ["1", "2"])
def test_pytest_skip_markers_on_unittest_methods_skip_without_teardown(
    tmp_path: Path, workers: str
) -> None:
    source = """\
import os
import unittest
from pathlib import Path

import pytest


class TestMarked(unittest.TestCase):
    def tearDown(self):
        Path(f"teardown-{self._testMethodName}").touch()

    def test_runs(self):
        pass

    @pytest.mark.skipif(not os.getenv("VFY_UNSET_SKIP_FLAG"), reason="flag unset")
    def test_skipif(self):
        pass

    @pytest.mark.skip(reason="never")
    def test_skip(self):
        pass
"""
    outcomes = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_marked.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            f"--workers={workers}",
            "--no-cov",
            "-q",
            env={"VFY_UNSET_SKIP_FLAG": None},
        )
        result.expect(0)
        torn_down = sorted(path.name for path in project.root.glob("teardown-*"))
        outcomes[engine] = (torn_down, "1 passed, 2 skipped" in result.stdout)
    assert outcomes["pytest-native"] == outcomes["pytest"]
    assert outcomes["pytest-native"] == (["teardown-test_runs"], True)


@pytest.mark.parametrize(
    ("marker", "status", "summary"),
    [
        ("skip(reason='class skipped')", 0, "1 passed, 1 skipped"),
        ("xfail(run=False, reason='class not run')", 0, "1 passed, 1 xfailed"),
        ("skipif('unknown_condition', reason='invalid condition')", 1, "1 passed, 1 error"),
    ],
)
def test_unittest_pre_setup_outcomes_preserve_class_fixtures(
    tmp_path: Path, marker: str, status: int, summary: str
) -> None:
    source = """\
import json
from pathlib import Path
from unittest import TestCase

import pytest

events = []

@pytest.fixture(scope="class", autouse=True)
def resource(request):
    events.append(["setup", request.cls.__name__])
    yield
    events.append(["finalizer", request.cls.__name__])
    Path("events.json").write_text(json.dumps(events), encoding="utf-8")

@pytest.mark.MARKER
class TestNotRun(TestCase):
    @classmethod
    def setUpClass(cls):
        raise AssertionError("marked class setup must not run")

    def test_not_run(self):
        raise AssertionError("marked test must not run")

class TestFollowing(TestCase):
    @classmethod
    def setUpClass(cls):
        events.append(["setUpClass", cls.__name__])
        cls.addClassCleanup(events.append, ["class-cleanup", cls.__name__])

    @classmethod
    def tearDownClass(cls):
        events.append(["tearDownClass", cls.__name__])

    def test_runs(self):
        events.append(["test", self.__class__.__name__])
""".replace("MARKER", marker)
    outcomes: dict[str, tuple[str, object]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_marked.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        assert result.returncode == status, result.stdout + result.stderr
        assert summary in result.stdout, result.stdout + result.stderr
        if status:
            assert "unknown_condition" in result.stdout, result.stdout
        outcomes[engine] = (
            _normalized_summary(result.stdout),
            json.loads((project.root / "events.json").read_text(encoding="utf-8")),
        )
    assert outcomes["pytest-native"] == outcomes["pytest"]
    assert outcomes["pytest-native"][1] == [
        ["setup", "TestFollowing"],
        ["setUpClass", "TestFollowing"],
        ["test", "TestFollowing"],
        ["tearDownClass", "TestFollowing"],
        ["class-cleanup", "TestFollowing"],
        ["finalizer", "TestFollowing"],
    ]
