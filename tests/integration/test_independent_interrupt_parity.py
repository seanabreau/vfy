"""Interrupt and exit banners that pytest's terminal reporter writes at session finish."""

from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject
from tests.support.differential import run_matching
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_keyboard_interrupt_in_test_prints_stock_banner(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_interrupt.py", "def test_interrupt():\n    raise KeyboardInterrupt\n")

    result = run_matching(project)

    assert result.returncode == 2
    assert "!!!!! KeyboardInterrupt !!!!!" in result.stdout
    assert "(to show a full traceback on KeyboardInterrupt use --full-trace)" in result.stdout


def test_pytest_exit_in_test_prints_stock_banner(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_exit.py", "import pytest\n\n\ndef test_exit():\n    pytest.exit('bye')\n")

    result = run_matching(project)

    assert result.returncode == 2
    assert "!!!!! _pytest.outcomes.Exit: bye !!!!!" in result.stdout


def test_interrupt_with_every_traceback_frame_hidden(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import pytest\n"
        "@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_keyboard_interrupt(excinfo):\n"
        "    frame = excinfo.tb\n"
        "    while frame is not None:\n"
        "        frame.tb_frame.f_globals['__tracebackhide__'] = True\n"
        "        frame = frame.tb_next\n",
    )
    project.write(
        "test_stop.py", "def test_stop():\n    raise KeyboardInterrupt('hidden sentinel')\n"
    )

    result = project.run_engine("native", "test_stop.py", "-q")
    output = result.stdout + result.stderr
    assert result.returncode == 2, output
    assert output.count("KeyboardInterrupt: hidden sentinel") == 1, output
    assert "INTERNALERROR" not in output
