"""Terminal boundary hooks retain their live public operands."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_logstart_monitor_and_wrappers_observe_the_same_arguments(tmp_path: Path) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("test_body.py", "def test_body(): pass\n")
        project.write(
            "conftest.py",
            """import json
from pathlib import Path
import pytest

events = []
operands = {}

class Hooks:
    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_logstart(self, nodeid, location):
        events.append(['wrapper-before', nodeid, location is operands['location']])
        result = yield
        events.append(['wrapper-after', nodeid, location is operands['location']])
        return result

    @pytest.hookimpl
    def pytest_runtest_logfinish(self, nodeid, location):
        events.append(['finish', nodeid, list(location)])

def pytest_configure(config):
    config.pluginmanager.register(Hooks(), name='boundary-observer')
    def before(name, hook_impls, kwargs):
        if name == 'pytest_runtest_logstart':
            kwargs['nodeid'] = 'virtual::test_body'
            kwargs['location'] = ('virtual.py', 3, 'test_body')
            operands['location'] = kwargs['location']
            events.append(['monitor-before', kwargs['nodeid'], list(kwargs['location'])])
    def after(outcome, name, hook_impls, kwargs):
        if name == 'pytest_runtest_logstart':
            events.append(['monitor-after', kwargs['nodeid'],
                           kwargs['location'] is operands['location']])
    config.pluginmanager.add_hookcall_monitoring(before, after)

def pytest_sessionfinish(session, exitstatus):
    Path('boundary.json').write_text(json.dumps(events))
""",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        result.expect(0)
        observations.append(json.loads((project.root / "boundary.json").read_text()))
    assert observations[0] == observations[1]
    assert observations[0][:4] == [
        ["monitor-before", "virtual::test_body", ["virtual.py", 3, "test_body"]],
        ["wrapper-before", "virtual::test_body", True],
        ["wrapper-after", "virtual::test_body", True],
        ["monitor-after", "virtual::test_body", True],
    ]


@requires_native_pytest
def test_public_logstart_unpacks_iterable_before_live_index_lookup(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_body.py",
        """import inspect
import pytest

def test_public_logstart(request):
    reporter = request.config.pluginmanager.getplugin('terminalreporter')
    original = reporter._status_index
    callback = original.terminal_logstart
    assert callback.__name__ == 'terminal_logstart'
    assert str(inspect.signature(callback)) == '(reporter, nodeid, location)'
    events = []
    class Index:
        def terminal_logstart(self, reporter_arg, nodeid, location):
            events.append(('called', reporter_arg is reporter, nodeid, location))
    def location():
        events.append('iterate')
        yield 'custom.py'
        yield 7
        yield 'test_custom'
    reporter._status_index = Index()
    try:
        descriptor = type(reporter).__dict__['pytest_runtest_logstart']
        descriptor.__get__(reporter)('custom::test', location())
        with pytest.raises(ValueError):
            reporter.pytest_runtest_logstart('bad', iter(['only-one']))
    finally:
        reporter._status_index = original
    assert events == ['iterate', ('called', True, 'custom::test',
                                  ('custom.py', 7, 'test_custom'))]
    callback(reporter=reporter, nodeid='custom::test',
             location=('custom.py', 7, 'test_custom'))
""",
    )
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    result.expect(0)


@requires_native_pytest
def test_registered_logstart_uses_live_status_index(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_body.py", "def test_body(): pass\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

config = None
events = []

def pytest_configure(config):
    globals()['config'] = config

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_logstart(nodeid, location):
    reporter = config.pluginmanager.getplugin('terminalreporter')
    previous = reporter._status_index
    class Index:
        def terminal_logstart(self, reporter_arg, actual_nodeid, actual_location):
            events.append([reporter_arg is reporter, actual_nodeid,
                           actual_location is location, list(actual_location)])
    reporter._status_index = Index()
    try:
        yield
    finally:
        reporter._status_index = previous

def pytest_sessionfinish(session, exitstatus):
    Path('live-index.json').write_text(json.dumps(events))
""",
    )
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    result.expect(0)
    assert json.loads((project.root / "live-index.json").read_text()) == [
        [True, "test_body.py::test_body", False, ["test_body.py", 0, "test_body"]]
    ]


@requires_native_pytest
def test_registered_logstart_calls_replaced_status_method(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_body.py", "def test_body(): pass\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

config = None
events = []

def pytest_configure(config):
    globals()['config'] = config

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_logstart(nodeid, location):
    reporter = config.pluginmanager.getplugin('terminalreporter')
    index = reporter._status_index
    cls = type(index)
    previous = cls.terminal_logstart
    def replacement(self, actual_reporter, actual_nodeid, actual_location):
        events.append([self is index, actual_reporter is reporter, actual_nodeid,
                       actual_location is location, list(actual_location)])
    cls.terminal_logstart = replacement
    try:
        yield
    finally:
        cls.terminal_logstart = previous

def pytest_sessionfinish(session, exitstatus):
    Path('replaced-index.json').write_text(json.dumps(events))
""",
    )
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    result.expect(0)
    assert json.loads((project.root / "replaced-index.json").read_text()) == [
        [True, True, "test_body.py::test_body", False, ["test_body.py", 0, "test_body"]]
    ]
