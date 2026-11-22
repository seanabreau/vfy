"""Configuration cleanup matches pytest's callback order and exception context."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("phase", ["normal", "configure", "unconfigure"])
def test_cleanup_exception_context_matches_pytest(tmp_path: Path, phase: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"""from pathlib import Path
import sys

def event(value):
    with Path('events').open('a') as output:
        output.write(value + '\\n')

def handled():
    error = sys.exception()
    with Path('handled').open('a') as output:
        output.write(f'{{type(error).__name__}}:{{error}}\\n')

def first():
    event('first')
    handled()
    raise ValueError('CLEANUP_FIRST')

def second():
    event('second')
    handled()
    raise LookupError('CLEANUP_SECOND')

def pytest_configure(config):
    config.add_cleanup(first)
    config.add_cleanup(second)
    if {phase!r} == 'configure':
        raise RuntimeError('CONFIGURE')

def pytest_unconfigure(config):
    event('unconfigure')
    if {phase!r} == 'unconfigure':
        raise RuntimeError('UNCONFIGURE')
""",
    )
    project.write("test_pass.py", "def test_pass():\n    pass\n")
    observed = []
    for engine in ("stock", "native"):
        side = VfyProject(tmp_path / engine)
        side.write("conftest.py", (project.root / "conftest.py").read_text())
        side.write("test_pass.py", "def test_pass():\n    pass\n")
        result = side.run_engine(engine, "-q", "test_pass.py")
        chain = [
            line.removeprefix("INTERNALERROR> ")
            for line in (result.stdout + result.stderr).splitlines()
            if re.search(r"^(INTERNALERROR> )?(ValueError|LookupError|RuntimeError): ", line)
            or line.startswith(("During handling of the above", "The above exception was"))
        ]
        observed.append(
            (
                result.returncode,
                (side.root / "events").read_text(),
                (side.root / "handled").read_text(),
                chain,
            )
        )
    assert observed[1] == observed[0]
    assert observed[0][1] == "unconfigure\nsecond\nfirst\n"
    assert "ValueError: CLEANUP_FIRST" in observed[0][3]
    if phase == "configure":
        assert "RuntimeError: CONFIGURE" in observed[0][3]
