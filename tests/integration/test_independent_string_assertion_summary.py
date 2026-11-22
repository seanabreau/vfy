"""String equality failures summarize operand values as pytest does, for any operand expression."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CONFTEST = """\
import json


def pytest_runtest_logreport(report):
    if report.when == "call" and not report.passed:
        with open("reports.jsonl", "a") as stream:
            stream.write(json.dumps([report.nodeid, report.outcome, str(report.longrepr)]) + "\\n")
"""

_TESTS = """\
import pytest

LEFT = "\\n".join(str(i) for i in range(50))


def tail():
    return "\\nx"


@pytest.mark.xfail
def test_expected_failure_with_concatenated_operand():
    assert LEFT == "\\n".join(str(i) for i in range(49)) + "\\nx"


def test_concatenated_right_operand():
    assert LEFT == LEFT[:-2] + tail()


def test_concatenated_left_operand():
    assert LEFT[:-2] + tail() == LEFT


def test_repeated_operand():
    assert "ab" * 40 == "ab" * 39 + "ac"


def test_formatted_operand():
    assert "value: %s" % LEFT == "value: other"
"""


def test_string_equality_summary_matches_stock(tmp_path: Path) -> None:
    observed = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", _CONFTEST)
        project.write("test_strings.py", _TESTS)
        result = project.run_engine(engine, "-q", "-p", "no:cacheprovider")
        assert "4 failed" in result.stdout, result.stdout + result.stderr
        text = (project.root / "reports.jsonl").read_text().replace(str(project.root), "<root>")
        observed.append([json.loads(line) for line in text.splitlines()])
    assert observed[0]
    assert observed[1] == observed[0]


def test_string_explanation_respects_truncation_configuration(tmp_path: Path) -> None:
    observed = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "pytest.ini", "[pytest]\ntruncation_limit_lines = 0\ntruncation_limit_chars = 0\n"
        )
        project.write("conftest.py", _CONFTEST)
        project.write(
            "test_strings.py",
            "def test_difference():\n"
            "    left = '\\n'.join(f'left {i}' for i in range(20))\n"
            "    right = '\\n'.join(f'right {i}' for i in range(20))\n"
            "    assert left == right\n",
        )
        result = project.run_engine(
            engine, "-q", "-p", "no:cacheprovider", env={"CI": None, "BUILD_NUMBER": None}
        )
        result.expect(1)
        text = (project.root / "reports.jsonl").read_text().replace(str(project.root), "<root>")
        observed.append([json.loads(line) for line in text.splitlines()])
    assert "right 19" in str(observed[0])
    assert observed[1] == observed[0]
