"""Plugin header wrappers retain the actual CPython reversal protocol."""

from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_header_wrapper_result_uses_reversed_protocol(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_value():\n    pass\n")
    project.write(
        "conftest.py",
        """import pytest

class HeaderResults(list):
    def __iter__(self):
        raise AssertionError('header results were eagerly iterated')
    def __reversed__(self):
        yield ['CUSTOM_reversed_protocol']

@pytest.hookimpl(wrapper=True)
def pytest_report_header():
    results = yield
    return HeaderResults(results)

@pytest.hookimpl(specname='pytest_report_header')
def pytest_report_header_custom():
    return 'CUSTOM_ordinary'
""",
    )
    oracle = project.run_engine("stock")
    oracle.expect(0)
    expected = [line for line in oracle.stdout.splitlines() if line.startswith("CUSTOM_")]
    assert expected == ["CUSTOM_reversed_protocol"]
    candidate = project.run_engine("native")
    assert candidate.returncode == oracle.returncode, candidate.stdout + candidate.stderr
    assert [
        line for line in candidate.stdout.splitlines() if line.startswith("CUSTOM_")
    ] == expected
