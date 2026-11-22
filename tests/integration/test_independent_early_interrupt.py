"""An interrupt before collection ends vfy like stock pytest."""

from __future__ import annotations

import signal
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("workers", [(), ("-n", "2")])
def test_keyboard_interrupt_while_loading_conftest_ends_on_sigint(
    tmp_path: Path, workers: tuple[str, ...]
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "print('LOADING', flush=True)\nraise KeyboardInterrupt()\n",
    )
    project.write("test_case.py", "def test_case():\n    pass\n")

    stock = project.run_engine("stock", "test_case.py", "-q", *workers)
    candidate = project.run_engine("native", "test_case.py", "-q", *workers)

    assert stock.returncode == -signal.SIGINT, stock.stdout + stock.stderr
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert candidate.stdout == stock.stdout == "LOADING\n"
    assert candidate.stderr.rstrip().endswith("KeyboardInterrupt")
    assert "evidence could not be recorded" not in candidate.stderr
