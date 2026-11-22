"""Assertion failure text matches stock pytest for ordinary and failing representations."""

from __future__ import annotations

import re
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = """class Prefixed(AssertionError):
    def __repr__(self):
        return "AssertionError('assert from repr')"


class Broken(AssertionError):
    def __repr__(self):
        raise RuntimeError("representation failure")


def test_message():
    raise AssertionError("assert plain message")


def test_repr_prefix():
    raise Prefixed()


def test_failing_repr():
    raise Broken()


def test_chained():
    try:
        raise LookupError("original cause")
    except LookupError as cause:
        raise AssertionError("assert chained") from cause
"""


def _report(stdout: str) -> str:
    """Return the failure sections and summary, ending at the count line."""

    lines = stdout.splitlines()
    end = max(index for index, line in enumerate(lines) if re.search(r" in \d+\.\d+s$", line))
    return re.sub(r" in \d+\.\d+s$", " in <duration>", "\n".join(lines[: end + 1]))


def test_assertion_failure_text_matches_stock_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_assertions.py", _SOURCE)
    arguments = ("-q", "--tb=long", "-p", "no:cacheprovider")
    stock = project.run_python("-m", "pytest", *arguments)
    independent = project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    assert independent.returncode == stock.returncode == 1
    assert _report(independent.stdout) == _report(stock.stdout)
