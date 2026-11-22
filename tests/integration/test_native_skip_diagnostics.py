"""Skip diagnostics use pytest's compact representation without rendering failures."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Unix")
@pytest.mark.parametrize("workers", [1, 2])
def test_skip_diagnostics_match_pytest_without_failure_rendering(
    tmp_path: Path, workers: int
) -> None:
    source = r"""import unittest
import pytest

class CountingSkipped(pytest.skip.Exception):
    calls = 0
    def __str__(self):
        self.calls += 1
        return f'counting reason {self.calls}'

@pytest.fixture
def skipped_setup():
    pytest.skip('setup prerequisite')

@pytest.fixture
def skipped_teardown():
    yield
    pytest.skip('teardown prerequisite')

@pytest.fixture
def counting_setup():
    raise CountingSkipped('counting reason')

@pytest.fixture
def counting_teardown():
    yield
    raise CountingSkipped('counting reason')

@pytest.mark.skip(reason='marker reason\nsecond line')
def test_marker():
    pass

def test_setup(skipped_setup):
    pass

def test_call():
    pytest.skip('call reason')

def test_noted_chain():
    try:
        raise ValueError('inner cause')
    except ValueError as cause:
        error = pytest.skip.Exception('chained reason')
        error.add_note('skip note')
        raise error from cause

def test_unittest():
    raise unittest.SkipTest('unittest reason')

def test_counting_message():
    raise CountingSkipped('counting reason')

def test_counting_setup(counting_setup):
    pass

def test_counting_teardown(counting_teardown):
    pass

def test_teardown(skipped_teardown):
    pass

def test_failure():
    assert False, 'failure control'
"""
    instrumentation = """import json
from pathlib import Path
import pytest

original = pytest.Function.repr_failure
def repr_failure(item, excinfo, *args, **kwargs):
    with Path('failure-rendering.txt').open('a') as stream:
        stream.write(excinfo.typename + '\\n')
    return original(item, excinfo, *args, **kwargs)
pytest.Function.repr_failure = repr_failure
"""
    observer = """
rows = []
def pytest_runtest_logreport(report):
    if report.skipped:
        rows.append([report.nodeid, report.when, report.longrepr[2], report.longreprtext])

def pytest_sessionfinish():
    Path('skips.json').write_text(json.dumps(rows))
"""
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", instrumentation + (observer if engine == "pytest" else ""))
        for module in ("test_skips_a.py", "test_skips_b.py"):
            project.write(module, source)
        record = project.root / "skips.json"
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            f"--workers={workers if engine == 'pytest-native' else 1}",
            "--no-cov",
            "-q",
            "--junitxml=results.xml",
            env={"PYTEST_NATIVE_RESULT": str(record) if engine == "pytest-native" else None},
        )
        result.expect(1)
        calls = (project.root / "failure-rendering.txt").read_text().splitlines()
        assert calls == ["AssertionError", "AssertionError"]
        data = json.loads(record.read_text())
        rows = (
            [
                [case["nodeid"], phase["phase"], phase["exception"], phase["detail"]]
                for case in data["cases"]
                for phase in case["phases"]
                if phase["outcome"] == "skipped"
            ]
            if engine == "pytest-native"
            else data
        )
        skip_elements = [
            (
                case.attrib["classname"],
                case.attrib["name"],
                skip.attrib,
                skip.text.replace(str(project.root), "<ROOT>") if skip.text is not None else None,
            )
            for case in ET.parse(project.root / "results.xml").iter("testcase")
            for skip in case.findall("skipped")
        ]
        observations[engine] = (
            sorted([value.replace(str(project.root), "<ROOT>") for value in row] for row in rows),
            skip_elements,
        )
    assert observations["pytest-native"] == observations["pytest"]
