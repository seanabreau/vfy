"""Terminal option checks retain the configuration through the lookup."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_terminal_config_lives_through_worker_option_lookup(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_example.py", "def test_example(): pass\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

events = []
terminal = None

class Options:
    def __init__(self, original): self.original = original
    def __getattr__(self, name):
        if name == '_worker_basetemp_index': events.append('probe')
        return getattr(self.original, name)

class Config:
    def __init__(self, original): self.original, self.used = original, False
    @property
    def option(self):
        self.used = True
        events.append('option')
        return Options(self.original.option)
    def __getattr__(self, name): return getattr(self.original, name)
    def __del__(self):
        if self.used: events.append('released')

def pytest_sessionstart(session):
    global terminal
    terminal = session.config.pluginmanager.get_plugin('terminalreporter')

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_logreport(report):
    if report.when != 'call': return (yield)
    cls, original = type(terminal), terminal.config
    cls.config = property(lambda self: Config(original))
    try: return (yield)
    finally: del cls.config

def pytest_sessionfinish():
    Path('events.json').write_text(json.dumps(events))
""",
    )
    result = project.run_python(
        "-m", "vfy.independent.entry", "-q", env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    )
    result.expect(0)
    assert json.loads((tmp_path / "events.json").read_text()) == ["option", "probe", "released"]
