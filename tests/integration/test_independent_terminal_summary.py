"""Summary hooks write through the same public stream operations as pytest."""

from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_summary_hook_output_matches_stock_terminal_reporter(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_value():\n    pass\n")
    project.write(
        "conftest.py",
        "def pytest_terminal_summary(terminalreporter):\n"
        "    terminalreporter.write('REPORT_BEGIN\\n')\n"
        "    terminalreporter.section('custom summary', sep='+-')\n"
        "    terminalreporter.write_line('body', green=True)\n"
        "    terminalreporter.write_sep('=', 'another section')\n"
        "    terminalreporter.write('REPORT_END\\n')\n",
    )
    # Both runs use default verbosity: the progress line leaves the reporter's
    # current-file state set, which is what decides whether a write adds a newline.
    oracle = project.run_python("-m", "pytest", env={"NO_COLOR": "1"})
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", env={"NO_COLOR": "1"})
    assert oracle.returncode == candidate.returncode == 0, (
        oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    )

    def section(output: str) -> str:
        return output.split("REPORT_BEGIN\n", 1)[1].split("REPORT_END\n", 1)[0]

    assert section(candidate.stdout) == section(oracle.stdout)
