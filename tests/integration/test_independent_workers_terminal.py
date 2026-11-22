"""The independent parallel route writes its terminal output as pytest-xdist does.

The controller alone writes progress and status lines. A usage error reaches
stderr once, although every worker collects the same selection.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="independent execution is unavailable on Windows"
)

_BRING_UP = re.compile(r"^(bringing up nodes\.\.\.|created: \d+/\d+ workers)$")
_DURATION = re.compile(r" in \d+\.\d+s")
_BANNER = re.compile(r"=+")


def _terminal_body(stdout: str) -> list[str]:
    """Return the lines from the worker bring-up onward, with timing normalized.

    Session headers differ between the two engines by plug-in and platform
    lines, so the comparison starts at the first bring-up line.
    """
    lines = stdout.splitlines()
    start = next(index for index, line in enumerate(lines) if _BRING_UP.match(line))
    return [_BANNER.sub("=", _DURATION.sub(" in <time>s", line)).rstrip() for line in lines[start:]]


@pytest.mark.parametrize("flags", [(), ("-q",)])
def test_parallel_terminal_output_matches_pytest_xdist(
    tmp_path: Path, flags: tuple[str, ...]
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_a.py", "def test_actual():\n    assert True\n\ndef test_second():\n    assert True\n"
    )
    project.write("test_b.py", "def test_actual():\n    assert True\n")
    stock = project.run_pytest(
        "-p", "xdist", "-n", "2", "-p", "no:cacheprovider", *flags, load_xdist=False
    )
    vfy = project.run_vfy("test", "--engine=native", "--workers=2", "--no-cov", *flags)
    assert stock.returncode == vfy.returncode == 0, stock.stdout + vfy.stdout + vfy.stderr
    assert _terminal_body(vfy.stdout) == _terminal_body(stock.stdout)


def test_parallel_not_found_forwards_the_usage_error_once(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_a.py", "def test_actual():\n    assert True\n")
    serial = project.run_vfy(
        "test", "--engine=native", "--workers=1", "--no-cov", "test_a.py::nope"
    )
    parallel = project.run_vfy(
        "test", "--engine=native", "--workers=2", "--no-cov", "test_a.py::nope"
    )
    message = "vfy independent usage error: not found: test_a.py::nope"
    assert serial.returncode == parallel.returncode == 4, parallel.stderr
    assert serial.stderr.splitlines().count(message) == 1
    assert parallel.stderr.splitlines().count(message) == 1
