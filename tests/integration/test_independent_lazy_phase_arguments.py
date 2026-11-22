"""Phase hooks retain the real mutable arguments seen by monitors."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_phase_monitor_mutation_and_wrapper_order_match_pytest(tmp_path: Path) -> None:
    observed = []
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
import json
from pathlib import Path
import pytest

events = []
current = {}
sentinel = object()

def pytest_configure(config):
    def before(name, methods, kwargs):
        if name not in {'pytest_runtest_setup', 'pytest_runtest_call', 'pytest_runtest_teardown'}:
            return
        original = dict(kwargs)
        current[name] = (kwargs, original)
        kwargs['item'] = sentinel
        if 'nextitem' in kwargs:
            kwargs['nextitem'] = sentinel
        events.append([name, 'before'])
    def after(outcome, name, methods, kwargs):
        if name in current:
            events.append([name, 'after', kwargs is current[name][0]])
    config.pluginmanager.add_hookcall_monitoring(before, after)

def restore(name, item, nextitem=None):
    kwargs, original = current[name]
    assert item is sentinel
    if 'nextitem' in original:
        assert nextitem is sentinel
    kwargs.update(original)
    events.append([name, 'wrapper'])

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    restore('pytest_runtest_setup', item)
    return (yield)

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    restore('pytest_runtest_call', item)
    return (yield)

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    restore('pytest_runtest_teardown', item, nextitem)
    return (yield)

def pytest_unconfigure():
    Path('events.json').write_text(json.dumps(events))
""",
        )
        project.write("test_case.py", "def test_one(): pass\ndef test_two(): pass\n")
        result = (
            project.run_pytest("-q", load_xdist=False, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=1",
                "--no-cov",
                "-q",
                env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observed.append(json.loads((project.root / "events.json").read_text()))
    assert observed[0] == observed[1]
    assert len(observed[0]) == 18
