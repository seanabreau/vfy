"""Public terminal buckets own the live report objects."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_terminal_stats_publish_all_phases_as_one_live_mapping(tmp_path: Path) -> None:
    observations = []
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import json
from pathlib import Path

def pytest_sessionfinish(session):
    terminal = session.config.pluginmanager.get_plugin('terminalreporter')
    stats = terminal.stats
    empty = stats['']
    passed = stats['passed']
    report = passed[0]
    report.user_properties.append(('proof', 'live'))
    stats['custom'] = [report]
    observations = {
        'same_mapping': terminal.stats is stats,
        'same_empty_bucket': terminal.stats[''] is empty,
        'same_passed_bucket': terminal.stats['passed'] is passed,
        'phases': [item.when for item in empty] + [report.when],
        'same_report': terminal.stats['custom'][0] is report,
        'properties': list(terminal.stats['passed'][0].user_properties),
    }
    Path('buckets.json').write_text(json.dumps(observations))
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
        observations.append(json.loads((project.root / "buckets.json").read_text()))
    assert observations[0] == {
        "same_mapping": True,
        "same_empty_bucket": True,
        "same_passed_bucket": True,
        "phases": ["setup", "teardown", "call"],
        "same_report": True,
        "properties": [["proof", "live"]],
    }
    assert observations[1] == observations[0]


def test_status_hook_caller_class_replacement_is_live(tmp_path: Path) -> None:
    observations = []
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import json
from pathlib import Path

events = []
original = None
caller_type = None

def pytest_sessionstart(session):
    global original, caller_type
    caller_type = type(session.config.hook.pytest_report_teststatus)
    original = caller_type.__call__
    def replacement(self, **kwargs):
        if self.name == 'pytest_report_teststatus':
            events.append(kwargs['report'].when)
        return original(self, **kwargs)
    caller_type.__call__ = replacement

def pytest_sessionfinish():
    caller_type.__call__ = original
    Path('hook-calls.json').write_text(json.dumps(events))
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
        observations.append(json.loads((project.root / "hook-calls.json").read_text()))
    assert observations[0] == ["setup", "call", "teardown"]
    assert observations[1] == observations[0]


def test_stats_attribute_interception_is_observed_during_reporting(tmp_path: Path) -> None:
    observations = []
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import json
from pathlib import Path

events = []
seen = []
reporter_type = None
original = None

def pytest_sessionstart(session):
    global reporter_type, original
    reporter = session.config.pluginmanager.get_plugin('terminalreporter')
    reporter_type = type(reporter)
    original = reporter_type.__getattribute__
    def intercept(self, name):
        if name == 'stats':
            events.append(name)
        return original(self, name)
    reporter_type.__getattribute__ = intercept

def pytest_runtest_logfinish():
    seen.append(bool(events))

def pytest_sessionfinish():
    reporter_type.__getattribute__ = original
    Path('intercept.json').write_text(json.dumps(seen))
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
        observations.append(json.loads((project.root / "intercept.json").read_text()))
    assert observations == [[True], [True]]
