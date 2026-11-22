"""Stop reasons and internal errors are written to the same stream and order as stock pytest."""

from __future__ import annotations

import re
from pathlib import Path

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _lines(text: str) -> list[str]:
    return [re.sub(r" in [0-9.]+s\b", " in Xs", line) for line in text.splitlines()]


def test_maxfail_stop_banner_matches_stock_stdout_and_stderr(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_stop.py",
        "def test_first():\n    assert 0\n\n\ndef test_second():\n    pass\n",
    )

    stock = project.run_pytest("-p", "no:cacheprovider", "-x", "--no-header", load_xdist=False)
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-x", "--no-header")

    assert independent.returncode == stock.returncode == 1
    assert _lines(independent.stdout) == _lines(stock.stdout)
    assert independent.stderr == stock.stderr == ""


def test_internal_error_lines_are_written_to_stdout(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "def pytest_runtest_logreport(report):\n"
        '    if report.when == "call":\n'
        '        raise RuntimeError("internal boom")\n',
    )
    project.write(
        "test_internal.py", "def test_first():\n    pass\n\n\ndef test_second():\n    pass\n"
    )

    stock: CommandResult = project.run_pytest(
        "-p", "no:cacheprovider", "--no-header", load_xdist=False
    )
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "--no-header")

    assert independent.returncode == stock.returncode == 3
    for result in (stock, independent):
        # The traceback frames differ by engine; the stream and line placement must not.
        lines = result.stdout.splitlines()
        assert "INTERNALERROR> Traceback (most recent call last):" in lines
        assert "INTERNALERROR> RuntimeError: internal boom" in lines
        assert "INTERNALERROR>" not in result.stderr
    # Stock ends the partial progress line before the first internal-error line.
    header = "INTERNALERROR> Traceback (most recent call last):"
    stock_lines = stock.stdout.splitlines()
    independent_lines = independent.stdout.splitlines()
    preceding = stock_lines[stock_lines.index(header) - 1]
    assert independent_lines[independent_lines.index(header) - 1] == preceding
