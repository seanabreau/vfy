"""Owned outcome exceptions keep stock type names, notes and summaries in failure output."""

from __future__ import annotations

import re
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = """import pytest


class ChildFailed(pytest.fail.Exception):
    pass


class ChildXFailed(pytest.xfail.Exception):
    pass


class Outer:
    class Child(pytest.fail.Exception):
        pass


class Failed(BaseException):
    pass


class XFailed(Exception):
    pass


class Note:
    def __str__(self):
        return "callback-note"


class CallbackFailed(pytest.fail.Exception):
    def __str__(self):
        return "callback-message"


class ReprNotes:
    def __repr__(self):
        raise RuntimeError("actual repr failure")


def test_helper_failed():
    pytest.fail("actual outcome")


def test_helper_failed_value():
    pytest.fail("actual outcome", pytrace=False)


def test_helper_xfailed():
    pytest.xfail("actual outcome")


def test_helper_skipped():
    pytest.skip("actual outcome")


def test_child_failed():
    raise ChildFailed("actual outcome")


def test_child_xfailed():
    raise ChildXFailed("actual outcome")


def test_nested_child():
    raise Outer.Child("actual outcome")


def test_lookalike_failed():
    raise Failed("actual outcome")


def test_lookalike_xfailed():
    raise XFailed("actual outcome")


def test_empty_failed():
    raise pytest.fail.Exception("")


def test_empty_xfailed():
    raise pytest.xfail.Exception("")


def test_noted_failed():
    error = pytest.fail.Exception("actual outcome")
    error.add_note("first\\nsecond")
    error.add_note("untouched note")
    raise error


def test_callback_notes_failed():
    error = CallbackFailed("actual outcome")
    error.__notes__ = [Note(), "tail"]
    raise error


def test_notes_repr_failed():
    error = pytest.fail.Exception("actual outcome")
    error.__notes__ = ReprNotes()
    raise error
"""


def _report(stdout: str) -> str:
    """Return the failure sections and summary, ending at the count line.

    Output after the count line is not compared: it can carry environment warnings.
    """

    lines = stdout.splitlines()
    end = max(index for index, line in enumerate(lines) if re.search(r" in \d+\.\d+s$", line))
    return re.sub(r" in \d+\.\d+s$", " in <duration>", "\n".join(lines[: end + 1]))


def test_owned_outcome_exceptions_match_stock_failure_output(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_outcomes.py", _SOURCE)
    arguments = ("-q", "-rA", "--tb=long", "-p", "no:cacheprovider")
    stock = project.run_python("-m", "pytest", *arguments)
    independent = project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    assert independent.returncode == stock.returncode
    assert _report(independent.stdout) == _report(stock.stdout)
