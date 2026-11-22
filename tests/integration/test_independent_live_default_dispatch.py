"""Independent-engine phase defaults respect hooks installed during a running session."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_live_phase_hook_and_monitor_follow_an_unobserved_test(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_cases.py",
            """\
import json
from pathlib import Path

events = []
manager = None
plugin = None
undo = None

class Observer:
    def pytest_runtest_setup(self, item):
        events.append(['setup-hook', item.name])
    def pytest_runtest_teardown(self, item):
        events.append(['teardown-hook', item.name])

def test_first():
    pass

def test_install(request):
    global manager, plugin, undo
    manager = request.config.pluginmanager
    plugin = Observer()
    manager.register(plugin, 'runtime-observer')
    def before(name, implementations, kwargs):
        if name in {'pytest_runtest_setup', 'pytest_runtest_call', 'pytest_runtest_teardown'}:
            events.append(['monitor', name, kwargs['item'].name])
    def after(outcome, name, implementations, kwargs):
        pass
    undo = manager.add_hookcall_monitoring(before, after)

def test_observed():
    undo()
    manager.unregister(plugin)
    Path('events.json').write_text(json.dumps(events))
""",
        )
        result = (
            project.run_pytest("-q", load_xdist=False, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=1",
                "--no-replay",
                "--no-cov",
                "-q",
                env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "3 passed" in result.stdout
        observations[engine] = json.loads((project.root / "events.json").read_text())
    assert (
        observations["native"]
        == observations["pytest"]
        == [
            ["monitor", "pytest_runtest_teardown", "test_install"],
            ["teardown-hook", "test_install"],
            ["monitor", "pytest_runtest_setup", "test_observed"],
            ["setup-hook", "test_observed"],
            ["monitor", "pytest_runtest_call", "test_observed"],
        ]
    )
