"""Capture reports an interrupt like stock pytest and leaves the next run unaffected."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("stage", ["conftest", "setup", "call", "teardown"])
def test_capture_survives_interrupt_and_the_next_run_is_clean(tmp_path: Path, stage: str) -> None:
    runs = [
        _interrupt_then_recover(tmp_path / engine, engine, stage) for engine in ("stock", "native")
    ]
    assert runs[1] == runs[0]
    assert runs[0][1] == 0


def _interrupt_then_recover(
    root: Path, engine: str, stage: str
) -> tuple[tuple[int, list[str]], int]:
    project = VfyProject(root)
    project.write(
        "conftest.py",
        """import os, pytest

if os.environ.get('CAPTURE_INTERRUPT') == 'conftest':
    print('INTERRUPT:conftest', flush=True)
    raise KeyboardInterrupt()

@pytest.fixture(autouse=True)
def phase():
    if os.environ.get('CAPTURE_INTERRUPT') == 'setup':
        print('INTERRUPT:setup', flush=True)
        raise KeyboardInterrupt()
    yield
    if os.environ.get('CAPTURE_INTERRUPT') == 'teardown':
        print('INTERRUPT:teardown', flush=True)
        raise KeyboardInterrupt()
""",
    )
    project.write(
        "test_capture.py",
        """import os

def test_interrupt():
    print('INTERRUPT:call', flush=True)
    os.write(1, b'INTERRUPT:descriptor\\n')
    if os.environ.get('CAPTURE_INTERRUPT') == 'call':
        raise KeyboardInterrupt()
""",
    )
    project.write("test_recovery.py", "def test_recovery():\n    pass\n")
    interrupted = project.run_engine(
        engine, "test_capture.py", "-q", env={"CAPTURE_INTERRUPT": stage}
    )
    markers = sorted(
        line
        for line in (interrupted.stdout + interrupted.stderr).splitlines()
        if line.startswith("INTERRUPT:")
    )
    recovery = project.run_engine(engine, "test_recovery.py", "-q", env={"CAPTURE_INTERRUPT": None})
    return (interrupted.returncode, markers), recovery.returncode
