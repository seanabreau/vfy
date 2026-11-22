"""Live pyfunc hook registration affects the next test call."""

from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_pyfunc_hook_registration_and_removal_between_tests(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_dynamic.py",
        """from pathlib import Path

def event(value):
    with Path('events').open('a') as output:
        output.write(value + '\\n')

class Plugin:
    def pytest_pyfunc_call(self, pyfuncitem):
        event('hook ' + pyfuncitem.name)

plugin = Plugin()

def test_register(request):
    event('register')
    request.config.pluginmanager.register(plugin, 'dynamic-pyfunc')

def test_active():
    event('active')

def test_unregister(request):
    event('unregister')
    request.config.pluginmanager.unregister(plugin)

def test_removed():
    event('removed')
""",
    )
    native = project.run_engine("native")
    native.expect(0)
    events = (tmp_path / "events").read_text()
    (tmp_path / "events").unlink()
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    assert events == (tmp_path / "events").read_text()
    assert events.splitlines() == [
        "register",
        "hook test_active",
        "active",
        "hook test_unregister",
        "unregister",
        "removed",
    ]
