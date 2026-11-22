"""``pytest_assertrepr_compare`` implementations explain failed comparisons as in pytest."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CONFTEST = """\
import json

import pytest


class Money:
    def __init__(self, cents):
        self.cents = cents

    def __eq__(self, other):
        return isinstance(other, Money) and self.cents == other.cents

    __hash__ = None


def pytest_assertrepr_compare(config, op, left, right):
    if isinstance(left, Money) and isinstance(right, Money):
        return [f"Money comparison {op}", "  left: %d" % left.cents, "multi\\nline", "50% off"]
    if op == "<" and left == 1000:
        return ["x" * 30] * 20


def pytest_runtest_logreport(report):
    if report.when == "call" and report.failed:
        with open("reports.jsonl", "a") as stream:
            stream.write(json.dumps([report.nodeid, str(report.longrepr)]) + "\\n")
"""

_TESTS = """\
from conftest import Money


def test_plugin_explains_equality():
    assert Money(100) == Money(250)


def test_plugin_explains_inequality_operator():
    assert Money(1) != Money(1)


def test_long_plugin_explanation_is_truncated():
    assert 1000 < 5


def test_builtin_rules_still_apply_without_plugin_answer():
    assert [1, 2, 3] == [1, 2, 4]


def test_unhandled_operands_use_expression_text():
    assert 1 + 1 == 3
"""


def test_assertrepr_compare_hook_matches_stock(tmp_path: Path) -> None:
    observed = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", _CONFTEST)
        project.write("test_compare.py", _TESTS)
        result = project.run_engine(engine, "-q", "-p", "no:cacheprovider")
        assert "5 failed" in result.stdout, result.stdout + result.stderr
        text = (project.root / "reports.jsonl").read_text().replace(str(project.root), "<root>")
        observed.append([json.loads(line) for line in text.splitlines()])
    assert any("Money comparison" in report for _, report in observed[0])
    assert observed[1] == observed[0]


def test_comparison_hooks_follow_item_scope_and_test_protocol(tmp_path: Path) -> None:
    observed = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        for name in ("a", "b"):
            project.write(
                f"{name}/conftest.py",
                "from pathlib import Path\n"
                "def pytest_assertrepr_compare(config, op, left, right):\n"
                "    with Path(config.rootpath, 'calls.txt').open('a') as f:\n"
                f"        f.write('{name}\\n')\n"
                f"    return ['{name} explanation']\n",
            )
        project.write(
            "a/test_scope.py",
            "try:\n    assert 1 == 2\nexcept AssertionError:\n    pass\n"
            "def test_failure():\n    assert 1 == 2\n",
        )
        project.write("b/test_other.py", "def test_other():\n    pass\n")
        result = project.run_engine(engine, "-q", "-p", "no:cacheprovider")
        result.expect(1)
        observed.append((project.root / "calls.txt").read_text().splitlines())
    assert observed[0] == ["a"]
    assert observed[1] == observed[0]


def test_comparison_hook_respects_truncation_configuration_and_ci(tmp_path: Path) -> None:
    for scenario, settings, environment in (
        ("disabled", "truncation_limit_lines = 0\ntruncation_limit_chars = 0\n", {}),
        ("limits", "truncation_limit_lines = 2\ntruncation_limit_chars = 0\n", {}),
        ("negative-lines", "truncation_limit_lines = -1\ntruncation_limit_chars = 1000\n", {}),
        ("negative-chars", "truncation_limit_lines = 2\ntruncation_limit_chars = -1\n", {}),
        ("negative-both", "truncation_limit_lines = -1\ntruncation_limit_chars = -1\n", {}),
        ("ci", "", {"CI": "1"}),
    ):
        observed = []
        for engine in ("stock", "native"):
            project = VfyProject(tmp_path / scenario / engine)
            project.write("pytest.ini", "[pytest]\n" + settings)
            project.write(
                "conftest.py",
                "def pytest_assertrepr_compare(config, op, left, right):\n"
                "    return [f'line {i}' for i in range(20)]\n",
            )
            project.write("test_compare.py", "def test_failure():\n    assert 1 == 2\n")
            result = project.run_engine(
                engine,
                "-q",
                "-p",
                "no:cacheprovider",
                env={"CI": None, "BUILD_NUMBER": None, **environment},
            )
            result.expect(1)
            observed.append(
                [
                    line.strip()
                    for line in result.stdout.splitlines()
                    if "line " in line or "Full output truncated" in line
                ]
            )
        assert observed[0]
        assert observed[1] == observed[0], scenario


def test_comparison_hook_runs_in_background_thread(tmp_path: Path) -> None:
    observed = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            "def pytest_assertrepr_compare(config, op, left, right):\n"
            "    return ['thread comparison']\n",
        )
        project.write(
            "test_thread.py",
            "import threading\nfrom pathlib import Path\n"
            "def fail():\n"
            "    try:\n        assert 1 == 2\n"
            "    except AssertionError as error:\n"
            "        Path('thread.txt').write_text(str(error))\n"
            "def test_thread():\n"
            "    thread = threading.Thread(target=fail)\n"
            "    thread.start()\n    thread.join()\n",
        )
        result = project.run_engine(engine, "-q", "-p", "no:cacheprovider")
        result.expect(0)
        assert (project.root / "thread.txt").exists(), result.stdout + result.stderr
        observed.append((project.root / "thread.txt").read_text())
    assert "thread comparison" in observed[0]
    assert observed[1] == observed[0]
