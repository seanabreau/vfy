"""Native failure summaries match pytest's ERRORS and FAILURES sections."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = """\
import logging
import sys

import pytest


@pytest.fixture
def broken():
    print("setup output")
    raise RuntimeError("setup failure")


@pytest.fixture
def noisy_teardown():
    yield
    print("teardown output")


@pytest.fixture
def failing_teardown():
    yield
    raise ValueError("teardown failure")


def helper(value):
    assert value == 2


def test_call(noisy_teardown):
    print("call output")
    print("call error", file=sys.stderr)
    logging.warning("call log")
    helper(1)


def test_setup(broken):
    pass


def test_teardown(failing_teardown):
    print("passing call output")


class TestGroup:
    @pytest.mark.parametrize("value", [1])
    def test_parameter(self, value):
        assert value == 3


@pytest.mark.xfail(reason="known")
def test_expected():
    assert False
"""


def _failure_summary(output: str) -> str:
    start = re.search(r"(?m)^=+ (?:ERRORS|FAILURES) =+$", output)
    end = re.search(r"(?m)^=+ short test summary info =+$", output)
    assert start is not None, output
    assert end is not None, output
    return re.sub(r"0x[0-9a-f]+", "0x?", output[start.start() : end.start()])


def _run(project: VfyProject, engine: str, workers: str, traceback: str) -> str:
    result = project.run_vfy(
        "test",
        f"--engine={engine}",
        f"--workers={workers}",
        "--no-cov",
        "-q",
        "--color=no",
        f"--tb={traceback}",
        "--xfail-tb",
        "-rfE",
    )
    result.expect(1)
    return _failure_summary(result.stdout)


@pytest.mark.parametrize("traceback", ["auto", "long", "short"])
def test_failure_summary_matches_pytest(tmp_path: Path, traceback: str) -> None:
    project = VfyProject(tmp_path)
    project.write("test_failures.py", _SOURCE)
    expected = _run(project, "pytest", "1", traceback)
    assert "ERROR at setup of test_setup" in expected
    assert "ERROR at teardown of test_teardown" in expected
    assert "TestGroup.test_parameter[1]" in expected
    assert "Captured stdout teardown" in expected
    for workers in ("1", "2"):
        assert _run(project, "pytest-native", workers, traceback) == expected


def test_native_traceback_style_keeps_pytest_sections(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_failures.py", _SOURCE)
    expected = _run(project, "pytest", "1", "native")
    actual = _run(project, "pytest-native", "1", "native")
    # Interpreter frames above the test differ between the two runners.
    headline = re.compile(r"(?m)^(?:[=_-]+ .+ [=_-]+|E?\s*\w+Error: .+)$")
    assert "ERROR at setup of test_setup" in expected
    assert headline.findall(actual) == headline.findall(expected)
    assert actual.count("Traceback (most recent call last):") == expected.count(
        "Traceback (most recent call last):"
    )


def test_line_traceback_style_is_rejected(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_failures.py", "def test_fails():\n    assert False\n")
    result = project.run_vfy("test", "--engine=pytest-native", "--no-cov", "--tb=line")
    assert result.returncode != 0
    assert "native pytest supports --tb=auto, long, short, native, and no" in result.stderr
