"""Direct parameters retain fixture hooks and live callable replacement."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_direct_parameters_keep_live_fixture_behavior(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
import inspect
import json
from pathlib import Path

events = []
native_callables = []

def pytest_fixture_setup(fixturedef, request):
    if fixturedef.argname != 'value':
        return
    native_callables.append(inspect.isbuiltin(fixturedef.func))
    events.append(['setup', request.param, tuple(inspect.signature(fixturedef.func).parameters)])
    if request.param == 2:
        fixturedef.func = lambda request: request.param * 10

class LateObserver:
    def pytest_fixture_post_finalizer(self, fixturedef, request):
        if fixturedef.argname == 'value':
            events.append(['finalize', request.param, fixturedef.cached_result[0]])

def pytest_sessionfinish():
    Path('events.json').write_text(json.dumps(events))
    Path('callables.json').write_text(json.dumps(native_callables))
""",
        )
        project.write(
            "test_values.py",
            """\
import pytest
from conftest import LateObserver

@pytest.mark.parametrize('value', [1, 2])
def test_parameter(value, request):
    if value == 1:
        request.config.pluginmanager.register(LateObserver(), 'late-observer')
    assert value in (1, 20)

@pytest.mark.parametrize('value', [3], scope='class')
class TestClass:
    def test_value(self, value):
        assert value == 3
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
        if engine == "native":
            assert json.loads((project.root / "callables.json").read_text()) == [True, True, True]
    assert (
        observations["native"]
        == observations["pytest"]
        == [
            ["setup", 1, ["request"]],
            ["finalize", 1, 1],
            ["setup", 2, ["request"]],
            ["finalize", 2, 20],
            ["setup", 3, ["request"]],
            ["finalize", 3, 3],
        ]
    )
