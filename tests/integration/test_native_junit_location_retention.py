"""Public JUnit captures retain locations only when their consumers need them."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="fork workers require Linux")

_FORK = {
    "VFY_WORKER_START": "fork",
    "VFY_NATIVE_SCHEDULING": "cases",
    "VFY_WORKER_COLLECTION": None,
    "PYTEST_NATIVE_RESULT": None,
}


def _cases(project: VfyProject) -> list[str]:
    root = ET.parse(project.root / "result.xml").getroot()
    result = []
    for case in root.iter("testcase"):
        case.attrib.pop("time", None)
        result.append(ET.tostring(case, encoding="unicode").replace(str(project.root), "<project>"))
    return sorted(result)


@pytest.mark.parametrize("family", ["xunit1", "xunit2"])
@pytest.mark.parametrize("reportchars", ["", "P", "X"])
def test_fork_junit_captures_preserve_location_and_terminal_contract(
    tmp_path: Path, family: str, reportchars: str
) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("stock", "native")}
    conftest = """
import json, os
from pathlib import Path
import pytest

@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session):
    role = os.getenv('VFY_NATIVE_PARALLEL_INDEX', 'controller')
    Path(f'collected-{role}').touch()
    Path('location-reads.jsonl').touch()
    original = pytest.Item.location
    def observed(item):
        role = os.getenv('VFY_NATIVE_PARALLEL_INDEX', 'controller')
        with Path('location-reads.jsonl').open('a') as destination:
            destination.write(json.dumps([role, item.nodeid]) + '\\n')
        return original.__get__(item, type(item))
    pytest.Item.location = property(observed)
"""
    source = """
import pytest

def test_pass():
    print('passing-capture-anchor')

@pytest.mark.xfail(reason='xpass-anchor', strict=False)
def test_xpass():
    print('xpass-capture-anchor')

def test_skip():
    pytest.skip('skip-location-anchor')

class TestDomain:
    @pytest.mark.parametrize('value', [0, 1])
    def test_method(self, value):
        print(f'domain-capture-{value}')
"""
    for project in projects.values():
        project.write("conftest.py", conftest)
        project.write("test_report.py", source)
    arguments = (
        "-q",
        "--capture=sys",
        "--junitxml=result.xml",
        "-o",
        f"junit_family={family}",
        "-o",
        "junit_logging=all",
        "-o",
        "junit_log_passing_tests=true",
        *((f"-r{reportchars}",) if reportchars else ()),
    )
    stock = projects["stock"].run_pytest("-n", "2", *arguments, env={"PYTEST_NATIVE_RESULT": None})
    native = projects["native"].run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "--no-cov",
        *arguments,
        env=_FORK,
    )
    for result in (stock, native):
        assert result.returncode == 0, result.stdout + result.stderr
    assert _cases(projects["native"]) == _cases(projects["stock"])
    assert sorted(path.name for path in projects["native"].root.glob("collected-*")) == [
        "collected-controller"
    ]
    cases = list(ET.parse(projects["native"].root / "result.xml").getroot().iter("testcase"))
    assert len(cases) == 5
    assert "passing-capture-anchor" in (projects["native"].root / "result.xml").read_text()
    assert "domain-capture-0" in (projects["native"].root / "result.xml").read_text()
    assert all(("file" in case.attrib) == (family == "xunit1") for case in cases)
    if family == "xunit1":
        assert all("line" in case.attrib for case in cases)
    if reportchars == "P":
        assert "passing-capture-anchor" in native.stdout
    elif reportchars == "X":
        assert "xpass-capture-anchor" in native.stdout
    reads = [
        json.loads(line)
        for line in (projects["native"].root / "location-reads.jsonl").read_text().splitlines()
    ]
    worker_reads = [nodeid for role, nodeid in reads if role != "controller"]
    if family == "xunit2" and not reportchars:
        assert worker_reads == []
    else:
        assert "test_report.py::test_pass" in worker_reads
        assert "test_report.py::TestDomain::test_method[0]" in worker_reads


@pytest.mark.parametrize("family", ["xunit1", "xunit2"])
def test_fork_junit_preserves_no_traceback_skip_fallback(tmp_path: Path, family: str) -> None:
    # Compare the native no-Python-frame fallback between its serial and fork routes.
    projects = {name: VfyProject(tmp_path / name) for name in ("stock", "serial", "fork")}
    # A built-in warning call raises the skip subtype without a Python body frame.
    # ctypes can retain an interpreter-owned executable descriptor on standalone builds.
    conftest = """
import _warnings, functools
from pathlib import Path
import pytest

class NativeSkipWarning(pytest.skip.Exception, Warning):
    pass

def pytest_collection_modifyitems(items):
    for item in items:
        if item.name == 'test_skip_without_python_frame':
            item.add_marker(pytest.mark.filterwarnings('error'))
            item.obj = functools.partial(
                _warnings.warn, NativeSkipWarning('no-traceback-skip-anchor')
            )
    Path('c-callable-installed').touch()
"""
    source = """
def test_skip_without_python_frame():
    raise AssertionError('original body must be replaced')

def test_pass():
    print('fallback-sibling-capture')
"""
    for project in projects.values():
        project.write("conftest.py", conftest)
        project.write("test_report.py", source)
    arguments = (
        "--no-replay",
        "--no-cov",
        "-q",
        "--capture=sys",
        "--junitxml=result.xml",
        "-o",
        f"junit_family={family}",
        "-o",
        "junit_logging=all",
        "-o",
        "junit_log_passing_tests=true",
    )
    stock = projects["stock"].run_pytest(
        *arguments[2:], load_xdist=False, env={"PYTEST_NATIVE_RESULT": None}
    )
    stock.expect(0)
    serial = projects["serial"].run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=1",
        *arguments,
        env={**_FORK, "VFY_WORKER_START": None},
    )
    fork = projects["fork"].run_vfy(
        "test", "--engine=pytest-native", "--workers=2", *arguments, env=_FORK
    )
    for name, result in (("serial", serial), ("fork", fork)):
        assert result.returncode == 0, result.stdout + result.stderr
        assert (projects[name].root / "c-callable-installed").exists()
        assert "1 passed, 1 skipped" in result.stdout
        skipped = ET.parse(projects[name].root / "result.xml").find(".//skipped")
        assert skipped is not None
        assert skipped.text == ".:-1: NativeSkipWarning: no-traceback-skip-anchor"
    for project in projects.values():
        cases = ET.parse(project.root / "result.xml").getroot().iter("testcase")
        skipped_case = next(
            case for case in cases if case.attrib["name"] == "test_skip_without_python_frame"
        )
        if family == "xunit1":
            assert skipped_case.attrib["file"] == "."
            assert skipped_case.attrib["line"] == "-1"
        else:
            assert "file" not in skipped_case.attrib
            assert "line" not in skipped_case.attrib
    # The native serial result is the fallback oracle: stock calls the C function
    # through a Python frame and consequently reports a different crash location.
    assert _cases(projects["fork"]) == _cases(projects["serial"])
