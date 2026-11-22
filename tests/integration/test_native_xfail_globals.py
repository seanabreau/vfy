"""Registered skipping hooks retain live evaluator globals and builtins."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or pytest.__version__ != "9.1.1",
    reason="native skipping oracle requires Unix and pinned pytest",
)

_SOURCE = """\
import gc
import json
import os
import sys
from pathlib import Path
from types import ModuleType
import pytest
import _pytest.skipping as skipping

events = []
original_evaluate = skipping.evaluate_xfail_marks

def first(item):
    events.append('first')
    return original_evaluate(item)

def second(item):
    events.append('second')
    return original_evaluate(item)

def decoy(item):
    raise AssertionError('sys.modules decoy evaluator was called')

@pytest.fixture(autouse=True)
def replace_evaluator(request):
    assert gc.isenabled()
    hooks = request.config.hook.pytest_runtest_call.get_hookimpls()
    hook = next(hook for hook in hooks if hook.plugin_name == 'skipping')
    namespace = hook.function.__globals__
    builtins = hook.function.__builtins__
    assert namespace is skipping.__dict__
    original = namespace['evaluate_xfail_marks']
    missing = object()
    old_builtin = builtins.get('evaluate_xfail_marks', missing)
    old_code = first.__code__
    fake = ModuleType('_pytest.skipping')
    fake.__dict__.update(skipping.__dict__)
    fake.evaluate_xfail_marks = decoy
    if os.environ['XFAIL_LOOKUP'] == 'globals':
        namespace['evaluate_xfail_marks'] = first
    else:
        del namespace['evaluate_xfail_marks']
        builtins['evaluate_xfail_marks'] = first
    sys.modules['_pytest.skipping'] = fake
    try:
        yield
        assert events == ['first', 'second'], events
        Path('observed.json').write_text(json.dumps(events))
    finally:
        first.__code__ = old_code
        namespace['evaluate_xfail_marks'] = original
        if old_builtin is missing:
            builtins.pop('evaluate_xfail_marks', None)
        else:
            builtins['evaluate_xfail_marks'] = old_builtin
        sys.modules['_pytest.skipping'] = skipping

def test_live_evaluator_code():
    assert events == ['first'], events
    first.__code__ = second.__code__
"""


@pytest.mark.parametrize("lookup", ["globals", "builtins"])
def test_native_xfail_uses_registered_live_namespace(tmp_path: Path, lookup: str) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_lookup.py", _SOURCE)
        environment = {
            "XFAIL_LOOKUP": lookup,
        }
        result = (
            project.run_pytest("-q", "--capture=sys", load_xdist=False, env=environment)
            if route == "pytest"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--native-scheduling=cases",
                "--no-replay",
                "--no-cov",
                "--capture=sys",
                "-q",
                env=environment,
            )
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "1 passed" in output, output
        observations[route] = json.loads((project.root / "observed.json").read_text())
    assert observations["native"] == observations["pytest"] == ["first", "second"]
