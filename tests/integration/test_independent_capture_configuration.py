"""Capture uses the actual parsed namespace and declared native API defaults."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("mutation", ["delete", "property"])
def test_initial_capture_propagates_real_namespace_attribute_errors(
    tmp_path: Path, mutation: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "broken_capture.py",
        f"""import json
import pytest
from pathlib import Path

events = []

def record(event):
    events.append(event)
    Path('events.json').write_text(json.dumps(events))

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_load_initial_conftests(early_config):
    record('before')
    if {mutation == "delete"}:
        del early_config.known_args_namespace.capture
    else:
        original = early_config.known_args_namespace
        class Namespace:
            @property
            def capture(self):
                raise AttributeError('capture accessor witness')
        early_config.known_args_namespace = Namespace()
        early_config.known_args_namespace.__dict__.update(vars(original))
    record('mutated')
    try:
        yield
    finally:
        record('after')
""",
    )
    project.write("test_capture.py", "def test_pass():\n    pass\n")
    observed = []
    for engine in ("stock", "native"):
        side = VfyProject(tmp_path / engine)
        for name in ("broken_capture.py", "test_capture.py"):
            side.write(name, (tmp_path / name).read_text())
        result = side.run_engine(engine, "-q", "-p", "broken_capture")
        events = json.loads((side.root / "events.json").read_text())
        output = result.stdout + result.stderr
        observed.append((result.returncode, events, "AttributeError" in output))
        if mutation == "property":
            assert "capture accessor witness" in output
    assert observed[0][1] == ["before", "mutated", "after"]
    assert observed[0][0] != 0
    assert observed[1][1:] == observed[0][1:]
    assert (observed[1][0] != 0) == (observed[0][0] != 0)
