"""Hookwrapper teardown warnings use pluggy's class and a user-owned location."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PLUGIN = """import pytest

class OriginalError(Exception):
    pass

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    yield
    raise OriginalError()
"""

_CASES = {
    "summary": [],
    "command-line-error": ["-W", "error::pluggy.PluggyTeardownRaisedWarning"],
    "command-line-ignore": ["-W", "ignore::pluggy.PluggyTeardownRaisedWarning"],
    "ini-error": ["-o", "filterwarnings=error::pluggy.PluggyTeardownRaisedWarning"],
    "ini-ignore": ["-o", "filterwarnings=ignore::pluggy.PluggyTeardownRaisedWarning"],
    "parent-class": ["-W", "ignore::pluggy.PluggyWarning"],
}


def _error_lines(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if line.startswith(("E   pluggy.", "    Plugin:", "FAILED "))
    ]


def _normalized(text: str) -> str:
    # Stock attributes the warning to a pytest frame; vfy has none, so only that location
    # (the pytest install path, file and source line) is normalised.
    text = re.sub(
        r"\n  \S*_pytest/runner\.py:\d+: PluggyTeardownRaisedWarning",
        "\n  <pytest-location>: PluggyTeardownRaisedWarning",
        text,
    )
    text = re.sub(
        r"\n  \S*legacy\.py:\d+: PluggyTeardownRaisedWarning",
        "\n  <pytest-location>: PluggyTeardownRaisedWarning",
        text,
    )
    text = re.sub(r"(For more information see \S+\n)    .*\n", r"\1", text)
    return re.sub(r" in [0-9.]+s", " in <time>", text)


@pytest.mark.parametrize("case", sorted(_CASES))
def test_teardown_warning_class_filters_match_stock(tmp_path: Path, case: str) -> None:
    project = VfyProject(tmp_path)
    project.write("legacy.py", _PLUGIN)
    project.write("test_one.py", "def test_one():\n    pass\n")
    arguments = ["-p", "legacy", "-q", "-p", "no:cacheprovider", *_CASES[case]]
    stock = project.run_engine("stock", *arguments)
    independent = project.run_engine("native", *arguments)
    assert independent.returncode == stock.returncode
    if case.endswith("-error"):
        # An error filter fails the call; the traceback below the warning shows engine frames.
        for line in _error_lines(stock.stdout):
            assert line in _error_lines(independent.stdout)
        assert "pluggy.PluggyTeardownRaisedWarning: A plugin raised" in independent.stdout
    else:
        assert _normalized(independent.stdout) == _normalized(stock.stdout)
    assert "vfy/independent" not in independent.stdout


def test_teardown_warning_location_is_the_raising_hook(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("legacy.py", _PLUGIN)
    project.write("test_one.py", "def test_one():\n    pass\n")
    result = project.run_engine("native", "-p", "legacy", "-q", "-p", "no:cacheprovider")
    assert re.search(r"\n  \S*legacy\.py:9: PluggyTeardownRaisedWarning", result.stdout)
