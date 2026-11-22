"""Hook dispatch keeps selected implementations alive during registry mutation."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_selected_hook_survives_unregister_and_reentrant_finalizer(tmp_path: Path) -> None:
    observations = []
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import gc
import json
from pathlib import Path
import weakref
import pytest

events = []
manager = None
reference = None

class Late:
    @pytest.hookimpl(trylast=True)
    def pytest_runtest_setup(self, item):
        events.append('selected')

    def __del__(self):
        events.append(['finalized', manager.get_plugin('late') is None])

class Mutator:
    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        removed = manager.unregister(name='late')
        events.append(['unregistered', removed is reference()])
        del removed
        gc.collect()
        events.append(['alive', reference() is not None])

def pytest_configure(config):
    global manager, reference
    manager = config.pluginmanager
    late = Late()
    reference = weakref.ref(late)
    manager.register(late, 'late')
    manager.register(Mutator(), 'mutator')

def pytest_sessionfinish():
    gc.collect()
    events.append(['released', reference() is None])
    Path('events.json').write_text(json.dumps(events))
""",
        )
        project.write("test_case.py", "def test_case(): pass\n")
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = (
            project.run_pytest("-q", load_xdist=False, env=env)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=env)
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observations.append(json.loads((project.root / "events.json").read_text()))
    assert observations[0][:3] == [["unregistered", True], ["alive", True], "selected"]
    assert observations[1][:3] == observations[0][:3]


def test_monitor_mutates_live_implementation_list(tmp_path: Path) -> None:
    observations = []
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_hooks.py",
            """import json
from pathlib import Path
import pytest

def test_hooks(pytestconfig):
    manager = pytestconfig.pluginmanager
    events = []
    class Specs:
        @pytest.hookspec
        def pytest_owned_snapshot(): pass
    class Plugin:
        def __init__(self, name): self.name = name
        def pytest_owned_snapshot(self):
            events.append(self.name)
    manager.add_hookspecs(Specs)
    manager.register(Plugin('first'), 'first')
    manager.register(Plugin('second'), 'second')
    def before(name, methods, kwargs):
        if name == 'pytest_owned_snapshot':
            events.append(['monitor', [method.plugin_name for method in methods]])
            methods[:] = [method for method in methods if method.plugin_name != 'second']
    def after(outcome, name, methods, kwargs): pass
    undo = manager.add_hookcall_monitoring(before, after)
    manager.hook.pytest_owned_snapshot()
    undo()
    manager.hook.pytest_owned_snapshot()
    Path('events.json').write_text(json.dumps(events))
""",
        )
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = (
            project.run_pytest("-q", load_xdist=False, env=env)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=env)
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observations.append(json.loads((project.root / "events.json").read_text()))
    assert observations[0] == [["monitor", ["first", "second"]], "first", "second", "first"]
    assert observations[1] == observations[0]


def test_empty_hook_result_identity_and_lifetime(tmp_path: Path) -> None:
    observations = []
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_hooks.py",
            """import gc
import json
from pathlib import Path
import pytest


def test_hook_results(pytestconfig):
    manager = pytestconfig.pluginmanager
    events = []
    shared = []

    class Specs:
        @pytest.hookspec
        def pytest_empty_result(): pass

        @pytest.hookspec
        def pytest_error_result(): pass

        @pytest.hookspec
        def pytest_ordered_result(): pass

    class Token:
        def __del__(self):
            events.append('finalized')

    class Inner:
        @pytest.hookimpl(wrapper=True)
        def pytest_empty_result(self):
            result = yield
            shared.append(result)
            result.append('inner')
            return result

        @pytest.hookimpl(wrapper=True)
        def pytest_error_result(self):
            result = yield
            result.append(Token())
            raise ValueError('failure')

        @pytest.hookimpl
        def pytest_ordered_result(self): return 'inner'

    class Outer:
        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_empty_result(self):
            result = yield
            events.append(['same', result is shared[0], result.copy()])
            result.append('outer')
            return result

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_error_result(self):
            try:
                yield
            except ValueError:
                gc.collect()
                events.append(['during_error', 'finalized' in events])
                return ['recovered']

        @pytest.hookimpl(tryfirst=True)
        def pytest_ordered_result(self): return 'outer'

    manager.add_hookspecs(Specs)
    manager.register(Inner(), 'inner-result')
    manager.register(Outer(), 'outer-result')
    def before(name, methods, kwargs): pass
    def after(outcome, name, methods, kwargs):
        if name == 'pytest_empty_result':
            events.append(['monitor', outcome.get_result() is shared[0]])
    undo = manager.add_hookcall_monitoring(before, after)
    result = manager.hook.pytest_empty_result()
    events.append(['returned', result is shared[0], result.copy()])
    undo()
    events.append(['recovered', manager.hook.pytest_error_result()])
    gc.collect()
    events.append(['ordered', manager.hook.pytest_ordered_result()])
    Path('result-events.json').write_text(json.dumps(events))
""",
        )
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = (
            project.run_pytest("-q", load_xdist=False, env=env)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=env)
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observations.append(json.loads((project.root / "result-events.json").read_text()))
    assert observations[1] == observations[0]
    assert ["same", True, ["inner"]] in observations[0]
    assert ["monitor", True] in observations[0]
    assert ["during_error", False] in observations[0]
    assert ["ordered", ["outer", "inner"]] in observations[0]
