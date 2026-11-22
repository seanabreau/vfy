"""Public native JUnit reporting parity through the vfy command."""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from stat import S_IMODE

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = requires_native_pytest

JUnitCaseShape = tuple[str, str, tuple[str, ...]]
JUnitShape = tuple[tuple[JUnitCaseShape, ...], dict[str, str]]
JUnitCapture = list[tuple[str, str, tuple[tuple[str, str], ...]]]


def _pytest_version() -> tuple[int, int]:
    return int(pytest.version_tuple[0]), int(pytest.version_tuple[1])


def _junit_source() -> str:
    return r"""
import logging
import sys

import pytest

@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown <&")

def test_pass(record_property, record_testsuite_property):
    print("captured out <&\x01")
    print("captured err >\x02", file=sys.stderr)
    logging.warning("captured log &")
    record_property("owner", "Research <&\x03")
    record_testsuite_property("build", "nightly <&\x04")

def test_fail():
    print("failure output <&\x05")
    assert False, "assertion <&\x06"

def test_skip():
    pytest.skip("deferred <&")

def test_call_and_teardown_failure(broken_teardown):
    assert False, "call failure"
"""


def _run_report(project: VfyProject, engine: str, family: str, output: Path) -> CommandResult:
    return project.run_vfy(
        "test",
        f"--engine={engine}",
        "--workers=1",
        "--no-cov",
        "-q",
        f"--junitxml={output}",
        "-o",
        f"junit_family={family}",
        "-o",
        "junit_logging=all",
        "-o",
        "junit_duration_report=call",
    )


def _run_with_umask(project: VfyProject, *arguments: str) -> CommandResult:
    # Set the child process umask before exec without mutating pytest's process state.
    launcher = project.write(
        "_run_with_umask.py",
        "import os, sys\n"
        "os.umask(0o022)\n"
        "os.execv(sys.executable, [sys.executable, '-m', 'vfy', *sys.argv[1:]])\n",
    )
    return project.run_python(str(launcher), *arguments)


def _suite(path: Path) -> ET.Element:
    root = ET.parse(path).getroot()
    suite = root.find("testsuite")
    assert suite is not None
    return suite


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
@pytest.mark.parametrize(
    "message",
    [
        pytest.param("x" * 501, id="long-line"),
        pytest.param("first\n" + "second\n" * 90, id="many-lines"),
        pytest.param("😀" * 501, id="unicode-line"),
        pytest.param("x" * 486 + "\n\n", id="empty-line-boundary"),
        pytest.param(("word" * 25 + "\x85") * 6, id="unicode-line-break"),
    ],
)
def test_failure_message_attributes_match_installed_pytest(
    tmp_path: Path, phase: str, message: str
) -> None:
    source = f"""import pytest

@pytest.fixture
def resource():
    if {phase!r} == 'setup':
        raise RuntimeError({message!r})
    yield
    if {phase!r} == 'teardown':
        raise RuntimeError({message!r})

def test_value(resource):
    if {phase!r} == 'call':
        raise RuntimeError({message!r})
"""
    reports = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_value.py", source)
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "-q", "--junitxml=results.xml"
        )
        result.expect(1)
        outcome = _suite(project.root / "results.xml").find(
            f"testcase/{'failure' if phase == 'call' else 'error'}"
        )
        assert outcome is not None, result.stdout + result.stderr
        assert outcome.text
        assert "RuntimeError" in outcome.text
        reports[engine] = outcome.attrib, outcome.text.replace(str(project.root), "<project>")
    assert reports["pytest-native"] == reports["pytest"]


@pytest.mark.parametrize("family", ["xunit1", "xunit2"])
@pytest.mark.parametrize("workers", [1, 2])
def test_recorded_xml_attributes_match_pytest(tmp_path: Path, family: str, workers: int) -> None:
    source = r"""import pytest

@pytest.fixture
def attributes(request, record_xml_attribute):
    record_xml_attribute("name", request.node.name + " setup")
    record_xml_attribute("classname", "custom <&\x01")
    record_xml_attribute("file", "custom-file.py")
    record_xml_attribute("line", "42")
    record_xml_attribute("url", "https://example.test/<&")
    record_xml_attribute("assertions", "7")
    record_xml_attribute("custom", "line1\nline2\ttab\rend")
    record_xml_attribute("time", "not-a-duration")
    yield
    record_xml_attribute("name", request.node.name + " teardown")

@pytest.fixture
def broken_teardown(attributes):
    yield
    raise RuntimeError("teardown sentinel")

def test_pass(attributes):
    pass

def test_split(broken_teardown):
    assert False, "call sentinel"

def test_plain():
    pass
"""
    evidence = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        for module in ("test_a.py", "test_b.py"):
            project.write(module, source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            f"--workers={workers if engine == 'pytest-native' else 1}",
            "--no-cov",
            "-q",
            "--junitxml=results.xml",
            "-o",
            f"junit_family={family}",
        )
        result.expect(1)
        cases = _suite(project.root / "results.xml").findall("testcase")
        evidence[engine] = [
            (
                {key: value for key, value in case.attrib.items() if key != "time"},
                tuple(child.tag for child in case),
            )
            for case in cases
        ]
        assert all(float(case.attrib["time"]) >= 0 for case in cases)
    assert evidence["pytest-native"] == evidence["pytest"]
    customized = [
        attrs for attrs, _ in evidence["pytest-native"] if attrs["classname"] == "custom <&#x01"
    ]
    assert len(customized) == 4
    assert all(attrs["name"].endswith(" teardown") for attrs in customized)
    if family == "xunit1":
        assert all(attrs["assertions"] == "7" for attrs in customized)
        assert all(
            attrs["file"] == "custom-file.py" and attrs["line"] == "42" for attrs in customized
        )
        assert all(attrs["custom"] == "line1\nline2\ttab\rend" for attrs in customized)
    else:
        assert all(set(attrs) == {"classname", "name"} for attrs in customized)


def test_recorded_xml_attributes_survive_early_teardown_failure(tmp_path: Path) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_attributes.py",
            """import pytest
@pytest.fixture(scope="session", autouse=True)
def resource():
    yield
@pytest.fixture
def attributes(record_xml_attribute):
    record_xml_attribute("assertions", "7")
    yield
    record_xml_attribute("name", "custom teardown")
    raise RuntimeError("teardown sentinel")
def test_first(attributes):
    pass
def test_unreached():
    assert False
""",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "-x",
            "--junitxml=result.xml",
            "-o",
            "junit_family=xunit1",
        )
        result.expect(1)
        observed[engine] = [
            (case.get("name"), case.get("assertions"))
            for case in _suite(project.root / "result.xml").findall("testcase")
        ]
    assert observed["pytest"] == [("custom teardown", "7")]
    assert observed["pytest-native"] == observed["pytest"]


def _shape(
    suite: ET.Element,
) -> JUnitShape:
    cases = tuple(
        (
            case.attrib["classname"],
            case.attrib["name"],
            tuple(child.tag for child in case),
        )
        for case in suite.findall("testcase")
    )
    counts = {key: suite.attrib[key] for key in ("tests", "errors", "failures", "skipped")}
    return cases, counts


def _captured_output(suite: ET.Element) -> JUnitCapture:
    return [
        (
            case.attrib["classname"],
            case.attrib["name"],
            tuple(
                (child.tag, child.text or "")
                for child in case
                if child.tag in {"system-out", "system-err"}
            ),
        )
        for case in suite.findall("testcase")
    ]


@pytest.mark.parametrize("skip_phase", ["setup", "call", "teardown"])
@pytest.mark.parametrize("log_passing_tests", [False, True])
@pytest.mark.parametrize("logging", ["log", "system-out", "system-err", "all"])
def test_skipped_junit_capture_matches_stock_phase_prefixes(
    tmp_path: Path, skip_phase: str, log_passing_tests: bool, logging: str
) -> None:
    source = f"""import logging
import sys
import pytest

def emit(phase):
    print(phase + '-out')
    print(phase + '-err', file=sys.stderr)
    logging.warning(phase + '-log')

@pytest.fixture
def resource():
    emit('setup')
    if {skip_phase!r} == 'setup':
        pytest.skip('deferred')
    yield
    emit('teardown')
    if {skip_phase!r} == 'teardown':
        pytest.skip('deferred')

def test_skip(resource):
    emit('call')
    if {skip_phase!r} == 'call':
        pytest.skip('deferred')
"""
    captures = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_capture.py", source)
        # Exercise normal output pruning: PYTEST_NATIVE_RESULT retains every
        # phase and would hide the skipped-test retention regression.
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--junitxml=result.xml",
            "-o",
            f"junit_logging={logging}",
            "-o",
            f"junit_log_passing_tests={str(log_passing_tests).lower()}",
        )
        result.expect(0)
        captures[engine] = _captured_output(_suite(project.root / "result.xml"))
    assert captures["pytest-native"] == captures["pytest"]
    assert any("setup-" in text for _, _, sections in captures["pytest"] for _, text in sections)


@pytest.mark.parametrize("log_passing_tests", [False, True])
def test_junit_capture_matches_stock_for_call_and_teardown_failures(
    tmp_path: Path, log_passing_tests: bool
) -> None:
    captures = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_capture.py",
            "import pytest\n"
            "@pytest.fixture\n"
            "def resource():\n"
            "    print('setup-out')\n"
            "    yield\n"
            "    print('teardown-out')\n"
            "    raise RuntimeError('teardown-token')\n"
            "def test_failure(resource):\n"
            "    print('call-out')\n"
            "    assert False, 'call-token'\n",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--junitxml=result.xml",
            "-o",
            "junit_logging=all",
            "-o",
            f"junit_log_passing_tests={str(log_passing_tests).lower()}",
        )
        result.expect(1)
        captures[engine] = _captured_output(_suite(project.root / "result.xml"))
    assert captures["pytest-native"] == captures["pytest"]


def _locations(suite: ET.Element, project_root: Path) -> list[tuple[str, str, str, str | None]]:
    locations = []
    root_prefix = str(project_root)
    for case in suite.findall("testcase"):
        file = case.attrib["file"]
        if file.startswith(root_prefix):
            file = f"<ROOT>{file[len(root_prefix) :]}"
        locations.append(
            (
                case.attrib["classname"],
                case.attrib["name"],
                file,
                case.attrib.get("line"),
            )
        )
    return locations


@pytest.mark.parametrize("family", ["xunit1", "xunit2"])
def test_native_junit_matches_stock_vfy(tmp_path: Path, family: str) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("pytest-native", "stock")}
    for project in projects.values():
        project.write("test_report.py", _junit_source())
    paths = {name: project.root / "junit.xml" for name, project in projects.items()}

    native = _run_report(projects["pytest-native"], "pytest-native", family, paths["pytest-native"])
    stock = _run_report(projects["stock"], "pytest", family, paths["stock"])
    assert native.returncode == stock.returncode == 1, (
        native.stdout + native.stderr + stock.stdout + stock.stderr
    )
    native_suite = _suite(paths["pytest-native"])
    stock_suite = _suite(paths["stock"])
    assert _shape(native_suite) == _shape(stock_suite)
    expected_test_count = "5" if _pytest_version() >= (9, 1) else "4"
    assert native_suite.attrib["tests"] == expected_test_count
    assert _captured_output(native_suite) == _captured_output(stock_suite)
    if family == "xunit1":
        assert _locations(native_suite, projects["pytest-native"].root) == _locations(
            stock_suite, projects["stock"].root
        )

    native_cases = native_suite.findall("testcase")
    passing = next(case for case in native_cases if case.attrib["name"] == "test_pass")
    assert {
        (node.attrib["name"], node.attrib["value"])
        for node in passing.findall("properties/property")
    } == {("owner", "Research <&#x03")}
    assert {
        (node.attrib["name"], node.attrib["value"])
        for node in native_suite.findall("properties/property")
    } == {("build", "nightly <&#x04")}

    native_xml = paths["pytest-native"].read_text(encoding="utf-8")
    stock_xml = paths["stock"].read_text(encoding="utf-8")
    for marker in ("#x01", "#x02", "#x03", "#x04", "#x05", "#x06"):
        assert marker in native_xml
        assert marker in stock_xml
    assert "captured out &lt;&amp;#x01" in native_xml
    assert "captured err &gt;#x02" in native_xml
    assert "captured log &amp;" in native_xml


def test_native_junit_duration_modes_match_stock_vfy(tmp_path: Path) -> None:
    source = """
import time
import pytest

@pytest.fixture
def timed_phases():
    time.sleep(0.02)
    yield
    time.sleep(0.02)

def test_duration(timed_phases):
    assert True
"""
    observed: dict[str, dict[str, JUnitShape]] = {"pytest-native": {}, "stock": {}}
    for name, engine in (("pytest-native", "pytest-native"), ("stock", "pytest")):
        project = VfyProject(tmp_path / name)
        project.write("test_durations.py", source)
        for mode in ("call", "total"):
            output = project.root / f"{mode}.xml"
            result_path = project.root / f"{mode}-native-result.json"
            result = project.run_vfy(
                "test",
                f"--engine={engine}",
                "--workers=1",
                "--no-cov",
                "-q",
                f"--junitxml={output}",
                "-o",
                f"junit_duration_report={mode}",
                env={"PYTEST_NATIVE_RESULT": str(result_path)}
                if engine == "pytest-native"
                else None,
            )
            result.expect(0)
            suite = _suite(output)
            testcase = suite.find("testcase")
            assert testcase is not None
            assert float(testcase.attrib["time"]) >= 0
            observed[name][mode] = _shape(suite)

            if engine == "pytest-native":
                raw = json.loads(result_path.read_text(encoding="utf-8"))
                assert raw["engine"] == "pytest-runner-native-v1"
                case = next(
                    case
                    for case in raw["cases"]
                    if case["nodeid"].endswith("test_durations.py::test_duration")
                )
                phase_durations = {phase["phase"]: phase["duration_ms"] for phase in case["phases"]}
                assert phase_durations["setup"] >= 15
                assert phase_durations["teardown"] >= 15
                expected_ms = (
                    phase_durations["call"] if mode == "call" else sum(phase_durations.values())
                )
                assert float(testcase.attrib["time"]) == pytest.approx(
                    expected_ms / 1_000, abs=0.0005
                )

    assert observed["pytest-native"] == observed["stock"]


def test_native_junit_subtests_match_stock_vfy(tmp_path: Path) -> None:
    source = """\
from unittest import TestCase

class TestSubtests(TestCase):
    def test_mixed(self):
        with self.subTest(value="passing"):
            self.assertEqual(1, 1)
        with self.subTest(value="failing"):
            self.assertEqual(1, 2)
        with self.subTest(value="skipped"):
            self.skipTest("deferred")
"""
    projects = {name: VfyProject(tmp_path / name) for name in ("pytest-native", "stock")}
    outputs = {name: project.root / "junit.xml" for name, project in projects.items()}
    for project in projects.values():
        project.write("test_subtests.py", source)

    native = projects["pytest-native"].run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=1",
        "--no-cov",
        "-q",
        f"--junitxml={outputs['pytest-native']}",
    )
    stock = projects["stock"].run_vfy(
        "test",
        "--engine=pytest",
        "--workers=1",
        "--no-cov",
        "-q",
        f"--junitxml={outputs['stock']}",
    )
    assert native.returncode == stock.returncode == 1, (
        native.stdout + native.stderr + stock.stdout + stock.stderr
    )
    native_suite = _suite(outputs["pytest-native"])
    stock_suite = _suite(outputs["stock"])
    assert _shape(native_suite) == _shape(stock_suite)
    if _pytest_version() >= (9, 0):
        expected_tests = "4"
        expected_skipped = "1"
        expected_subtest_tags = ["failure", "skipped"]
    else:
        expected_tests = "1"
        expected_skipped = "0"
        expected_subtest_tags = ["failure"]
    assert native_suite.attrib["tests"] == expected_tests
    assert native_suite.attrib["failures"] == "1"
    assert native_suite.attrib["skipped"] == expected_skipped
    native_case = native_suite.find("testcase")
    assert native_case is not None
    assert [child.tag for child in native_case] == expected_subtest_tags


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("teardown_error", [False, True])
@pytest.mark.parametrize("log_passing_tests", [False, True])
def test_native_junit_failed_subtests_preserve_grouping_and_capture(
    tmp_path: Path, workers: int, teardown_error: bool, log_passing_tests: bool
) -> None:
    if int(pytest.version_tuple[0]) < 9:
        pytest.skip("pytest 9 owns subtest reports")
    source = f"""import logging
import sys
from unittest import TestCase
import pytest

def emit(phase):
    print(phase + '-stdout-sentinel')
    print(phase + '-stderr-sentinel', file=sys.stderr)
    logging.warning(phase + '-log-sentinel')

@pytest.fixture(autouse=True)
def resource():
    emit('setup')
    yield
    emit('teardown')
    if {teardown_error!r}:
        raise RuntimeError('teardown failure sentinel')

class TestSubtests(TestCase):
    def test_failures(self):
        emit('call')
        for value in (1, 2, 3):
            with self.subTest(value=value):
                if value == 3:
                    self.skipTest('subtest skip sentinel')
                self.fail('subtest failure sentinel')
"""
    evidence: dict[str, tuple[JUnitShape, JUnitCapture]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_subtests.py", source)
        project.write("test_other.py", "def test_other(): pass\n")
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            f"--workers={workers if engine == 'pytest-native' else 1}",
            "--no-cov",
            "-q",
            "--junitxml=result.xml",
            "-o",
            "junit_logging=all",
            "-o",
            f"junit_log_passing_tests={str(log_passing_tests).lower()}",
        )
        result.expect(1)
        suite = _suite(project.root / "result.xml")
        evidence[engine] = _shape(suite), _captured_output(suite)
        captured = "".join(child.text or "" for child in suite.iter("system-out"))
        assert "setup-stdout-sentinel" in captured
        if not log_passing_tests and not teardown_error:
            assert "call-stdout-sentinel" not in captured
            assert "teardown-stdout-sentinel" not in captured
    assert evidence["pytest-native"] == evidence["pytest"]


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("kind", ["xfail", "subtest-failure", "subtest-skip", "subtest-xfail"])
def test_native_junit_outcome_diagnostics_match_pytest(
    tmp_path: Path, workers: int, kind: str
) -> None:
    if kind != "xfail" and int(pytest.version_tuple[0]) < 9:
        pytest.skip("pytest 9 owns subtest reports")
    source = "import pytest\n"
    if kind == "xfail":
        source += (
            "@pytest.mark.xfail(reason='later', strict=False)\n"
            "def test_outcome():\n"
            "    assert False, 'diagnostic sentinel'\n"
        )
    else:
        source += "from unittest import TestCase\n"
        if kind == "subtest-xfail":
            source += "@pytest.mark.xfail(reason='later', strict=False)\n"
        source += (
            "class TestOutcome(TestCase):\n"
            "    def test_outcome(self):\n"
            "        with self.subTest(value=1):\n"
        )
        source += (
            "            self.skipTest('deferred')\n"
            if kind == "subtest-skip"
            else "            self.fail('diagnostic sentinel')\n"
        )
    diagnostics = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_outcome.py", source)
        project.write("test_other.py", "def test_other(): pass\n")
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            f"--workers={workers if engine == 'pytest-native' else 1}",
            "--no-cov",
            "-q",
            "--junitxml=results.xml",
        )
        assert result.returncode == int(kind == "subtest-failure"), result.stdout + result.stderr
        diagnostics[engine] = [
            (
                child.tag,
                child.attrib,
                (child.text or "").replace(str(project.root), "<project>"),
            )
            for child in _suite(project.root / "results.xml").iter()
            if child.tag in {"failure", "skipped"}
        ]
    assert diagnostics["pytest"], diagnostics
    assert diagnostics["pytest-native"] == diagnostics["pytest"]


def test_native_junit_collection_reports_match_stock_vfy(tmp_path: Path) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("pytest-native", "pytest")}
    for project in projects.values():
        project.write("test_broken.py", "def test_broken(:\n    pass\n")
        project.write(
            "test_skipped.py",
            "import pytest\npytest.skip('not on this platform', allow_module_level=True)\n",
        )
        project.write("test_good.py", "def test_good():\n    assert True\n")
    outputs = {name: project.root / "junit.xml" for name, project in projects.items()}

    results = {
        name: projects[name].run_vfy(
            "test",
            f"--engine={name}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--continue-on-collection-errors",
            f"--junitxml={outputs[name]}",
        )
        for name in projects
    }
    native = results["pytest-native"]
    stock = results["pytest"]
    assert native.returncode == stock.returncode == 1, (
        native.stdout + native.stderr + stock.stdout + stock.stderr
    )
    native_suite = _suite(outputs["pytest-native"])
    stock_suite = _suite(outputs["pytest"])
    assert _shape(native_suite) == _shape(stock_suite)
    assert native_suite.attrib["tests"] == "3"
    assert native_suite.attrib["errors"] == "1"
    assert native_suite.attrib["skipped"] == "1"


@pytest.mark.parametrize("family", ["xunit1", "xunit2"])
@pytest.mark.parametrize("workers", [1, 2])
def test_independent_junit_groups_repeated_collection_reports(
    tmp_path: Path, family: str, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = --doctest-modules\n")
    project.write(
        "test_skipped.py",
        "import pytest\npytest.skip('first\\nsecond   ', allow_module_level=True)\n",
    )
    project.write("test_good.py", "def test_good():\n    pass\n")

    def read_result() -> tuple[
        list[tuple[dict[str, str], list[tuple[str, dict[str, str], str | None]]]], dict[str, str]
    ]:
        suite = _suite(tmp_path / "result.xml")
        cases = []
        for case in suite.findall("testcase"):
            attributes = {key: value for key, value in case.attrib.items() if key != "time"}
            children = [(child.tag, child.attrib, child.text) for child in case]
            cases.append((attributes, children))
        return cases, {key: suite.attrib[key] for key in ("tests", "errors", "failures", "skipped")}

    # Independent workers retain one worker's complete collection reports.
    stock = project.run_pytest(
        "-q", "--junitxml=result.xml", "-o", f"junit_family={family}", load_xdist=False
    )
    stock.expect(0)
    expected = read_result()
    assert len(expected[0]) == 2
    assert [child[0] for child in expected[0][0][1]] == ["skipped", "skipped"]
    assert expected[1] == {"tests": "3", "errors": "0", "failures": "0", "skipped": "2"}

    candidate = project.run_vfy(
        "test",
        "--engine=native",
        f"--workers={workers}",
        "--no-cov",
        "-q",
        "--junitxml=result.xml",
        "-o",
        f"junit_family={family}",
    )
    candidate.expect(0)
    assert read_result() == expected


def test_native_junit_write_failure_preserves_parent_file(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "native")
    blocker = project.write("blocked", "preserve this file")
    project.write(
        "test_report.py",
        "from pathlib import Path\n"
        "def test_body_runs():\n"
        "    Path('body-ran.txt').write_text('yes', encoding='utf-8')\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=1",
        "--no-cov",
        "-q",
        "--junitxml=blocked/report.xml",
    )
    assert result.returncode != 0
    assert (project.root / "body-ran.txt").read_text(encoding="utf-8") == "yes"
    assert blocker.read_text(encoding="utf-8") == "preserve this file"
    assert not (project.root / "blocked" / "report.xml").exists()


def test_native_junit_write_failure_preserves_existing_report(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "native")
    project.write(
        "test_report.py",
        "from pathlib import Path\n"
        "def test_body_runs():\n"
        "    Path('body-ran.txt').write_text('yes', encoding='utf-8')\n",
    )
    report_directory = project.root / "reports"
    report_directory.mkdir()
    output = report_directory / "junit.xml"
    output.write_text("previous report", encoding="utf-8")
    original_mode = S_IMODE(report_directory.stat().st_mode)
    report_directory.chmod(0o555)
    try:
        probe = report_directory / "write-access-probe"
        try:
            descriptor = os.open(
                probe,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except PermissionError:
            pass
        else:
            try:
                os.close(descriptor)
            finally:
                probe.unlink(missing_ok=True)
            pytest.skip("the current user can create files in the mode-0555 report directory")

        result = project.run_vfy(
            "test",
            "--engine=pytest-native",
            "--workers=1",
            "--no-cov",
            "-q",
            f"--junitxml={output}",
        )
    finally:
        report_directory.chmod(original_mode)

    assert result.returncode != 0
    assert (project.root / "body-ran.txt").read_text(encoding="utf-8") == "yes"
    assert output.read_text(encoding="utf-8") == "previous report"


@pytest.mark.parametrize("existing_mode", [0o640, 0o600])
def test_native_junit_file_permissions_match_stock_vfy(
    tmp_path: Path,
    existing_mode: int,
) -> None:
    observed: dict[str, list[int]] = {}
    for name, engine in (("pytest-native", "pytest-native"), ("stock", "pytest")):
        project = VfyProject(tmp_path / name)
        project.write("test_report.py", "def test_pass():\n    assert True\n")
        output = project.root / "junit.xml"
        arguments = (
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            f"--junitxml={output}",
        )

        created = _run_with_umask(project, *arguments)
        created.expect(0)
        created_mode = S_IMODE(output.stat().st_mode)
        output.chmod(existing_mode)

        replaced = _run_with_umask(project, *arguments)
        replaced.expect(0)
        replaced_mode = S_IMODE(output.stat().st_mode)
        observed[name] = [created_mode, replaced_mode]

    assert observed["stock"] == [0o644, existing_mode]
    assert observed["pytest-native"] == observed["stock"]


@pytest.mark.parametrize(
    "destination_kind",
    [
        "existing",
        "chain",
        "dangling",
        "missing-target-parent",
        "parent-link",
        "directory-target",
        "loop",
    ],
)
def test_native_junit_symlink_destinations_match_stock_vfy(
    tmp_path: Path,
    destination_kind: str,
) -> None:
    observed: dict[
        str, tuple[bool, bool, str | None, bool, bool, bool, str | None, object | None]
    ] = {}
    for name, engine in (("pytest-native", "pytest-native"), ("stock", "pytest")):
        root = tmp_path / name
        project = VfyProject(root)
        project.write("test_report.py", "def test_pass():\n    assert True\n")
        output = root / "junit.xml"
        target = root / "report-target.xml"
        second_link = root / "report-chain.xml"
        parent_link: Path | None = None

        if destination_kind == "existing":
            target.write_text("previous report", encoding="utf-8")
            output.symlink_to(target.name)
        elif destination_kind == "chain":
            target.write_text("previous report", encoding="utf-8")
            second_link.symlink_to(target.name)
            output.symlink_to(second_link.name)
        elif destination_kind == "dangling":
            output.symlink_to(target.name)
        elif destination_kind == "missing-target-parent":
            target = root / "absent" / "report-target.xml"
            output.symlink_to(Path("absent") / target.name)
        elif destination_kind == "parent-link":
            actual_reports = root / "actual-reports"
            actual_reports.mkdir()
            parent_link = root / "reports-link"
            parent_link.symlink_to(actual_reports.name, target_is_directory=True)
            output = parent_link / "junit.xml"
            target = actual_reports / "junit.xml"
        elif destination_kind == "directory-target":
            target.mkdir()
            output.symlink_to(target.name, target_is_directory=True)
        else:
            second_link.symlink_to(output.name)
            output.symlink_to(second_link.name)

        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            f"--junitxml={output}",
        )
        xml_shape = (
            _shape(_suite(target))
            if target.is_file() and target.read_text(encoding="utf-8").startswith("<?xml")
            else None
        )
        observed[name] = (
            result.returncode == 0,
            output.is_symlink(),
            os.readlink(output) if output.is_symlink() else None,
            target.exists(),
            parent_link.is_symlink() if parent_link is not None else False,
            second_link.is_symlink(),
            os.readlink(second_link) if second_link.is_symlink() else None,
            xml_shape,
        )

    assert observed["pytest-native"] == observed["stock"]
    if destination_kind in {"existing", "chain", "dangling", "parent-link"}:
        assert observed["stock"][0]
        assert observed["stock"][3]
        assert observed["stock"][7] is not None
    else:
        assert not observed["stock"][0]
        assert observed["stock"][1]
        assert observed["stock"][7] is None
    if destination_kind in {"existing", "dangling", "missing-target-parent"}:
        assert observed["stock"][1]
    if destination_kind == "chain":
        assert observed["stock"][1]
        assert observed["stock"][5]
    if destination_kind == "parent-link":
        assert observed["stock"][4]
    if destination_kind == "missing-target-parent":
        assert not (tmp_path / "stock" / "absent").exists()


def test_native_junit_readonly_report_write_access_matches_stock_vfy(
    tmp_path: Path,
) -> None:
    observed: dict[str, tuple[bool, str]] = {}
    for name, engine in (("pytest-native", "pytest-native"), ("stock", "pytest")):
        project = VfyProject(tmp_path / name)
        project.write("test_report.py", "def test_pass():\n    assert True\n")
        output = project.root / "junit.xml"
        output.write_text("previous report", encoding="utf-8")
        output.chmod(0o400)
        try:
            descriptor = os.open(output, os.O_WRONLY | os.O_APPEND)
        except PermissionError:
            pass
        else:
            os.close(descriptor)
            pytest.skip("the current user can write the mode-0400 report")

        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            f"--junitxml={output}",
        )
        observed[name] = (result.returncode == 0, output.read_text(encoding="utf-8"))

    assert observed["stock"] == (False, "previous report")
    assert observed["pytest-native"] == observed["stock"]


@pytest.mark.parametrize(
    ("selection", "native_workers"),
    [((), 1), (("-k", "test_property"), 1), ((), 2)],
)
def test_parameter_properties_survive_case_teardown(
    tmp_path: Path, selection: tuple[str, ...], native_workers: int
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_properties.py",
            """import pytest
@pytest.fixture
def resource(record_property):
    record_property("phase", "setup")
    yield
    record_property("phase", "teardown")
@pytest.mark.parametrize("value", [1, 2])
def test_property(value, resource, record_property):
    record_property("value", value)
""",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            f"--workers={native_workers if engine == 'pytest-native' else 1}",
            "--no-cov",
            "-q",
            "--junitxml=result.xml",
            "-o",
            "junit_family=xunit1",
            *selection,
        )
        result.expect(0)
        observed[engine] = [
            (case.get("name"), [prop.attrib for prop in case.findall("properties/property")])
            for case in ET.parse(project.root / "result.xml").iter("testcase")
        ]
    assert observed["pytest"] == [
        (
            f"test_property[{value}]",
            [
                {"name": "phase", "value": "setup"},
                {"name": "value", "value": str(value)},
                {"name": "phase", "value": "teardown"},
            ],
        )
        for value in (1, 2)
    ]
    assert observed["pytest-native"] == observed["pytest"]


def test_early_stop_retains_properties_from_final_teardown(tmp_path: Path) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_properties.py",
            """import pytest
@pytest.fixture(scope="session")
def resource():
    yield
@pytest.fixture
def final_property(record_property, resource):
    record_property("phase", "setup")
    yield
    record_property("phase", "teardown")
@pytest.mark.parametrize("value", [1, 2])
def test_property(value, final_property, record_property):
    record_property("value", value)
    assert False
""",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "-x",
            "--junitxml=result.xml",
            "-o",
            "junit_family=xunit1",
        )
        result.expect(1)
        observed[engine] = [
            (case.get("name"), [prop.attrib for prop in case.findall("properties/property")])
            for case in ET.parse(project.root / "result.xml").iter("testcase")
        ]
    assert observed["pytest"] == [
        (
            "test_property[1]",
            [
                {"name": "phase", "value": "setup"},
                {"name": "value", "value": "1"},
                {"name": "phase", "value": "teardown"},
            ],
        )
    ]
    assert observed["pytest-native"] == observed["pytest"]
