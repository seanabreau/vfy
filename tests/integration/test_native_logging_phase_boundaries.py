"""Logging phase acquisition and cleanup failures remain test outcomes."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native logging requires Unix")


@pytest.mark.parametrize("early_stop", [False, True])
@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
@pytest.mark.parametrize("boundary", ["enter", "exit"])
def test_logging_phase_failure_preserves_reports_output_and_finalizers(
    tmp_path: Path, phase: str, boundary: str, early_stop: bool
) -> None:
    conftest = f"""import atexit
import contextlib
import json
from pathlib import Path
import pytest

events = []
atexit.register(lambda: Path('events.json').write_text(json.dumps(events)))

@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    plugin = config.pluginmanager.getplugin('logging-plugin')
    original = plugin._runtest_for
    @contextlib.contextmanager
    def scope(item, when):
        selected = item.name == 'test_first' and when == {phase!r}
        if selected and {boundary!r} == 'enter':
            raise RuntimeError('logging enter sentinel')
        with original(item, when):
            yield
        if selected and {boundary!r} == 'exit':
            raise RuntimeError('logging exit sentinel')
    plugin._runtest_for = scope
"""
    source = """import logging
import pytest
from conftest import events

@pytest.fixture(autouse=True)
def lifecycle(request):
    events.append('setup:' + request.node.name)
    print('setup-output:' + request.node.name)
    logging.info('setup-log:' + request.node.name)
    request.node.addfinalizer(lambda: events.append('node-finalizer:' + request.node.name))
    yield
    events.append('teardown:' + request.node.name)
    print('teardown-output:' + request.node.name)
    logging.info('teardown-log:' + request.node.name)

def test_first():
    events.append('call:test_first')
    print('call-output:test_first')
    logging.info('call-log:test_first')

def test_second():
    events.append('call:test_second')
    print('call-output:test_second')
    logging.info('call-log:test_second')
"""
    observed_events: dict[str, list[str]] = {}
    observed_cases: dict[str, list[tuple[str | None, list[str], list[tuple[str, str | None]]]]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "pytest.ini",
            "[pytest]\nlog_level=INFO\nlog_format=%(message)s\n",
        )
        project.write("conftest.py", conftest)
        project.write("test_phases.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            *(["--maxfail=1"] if early_stop else []),
            "--junitxml=result.xml",
            "-o",
            "junit_logging=all",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(1)
        assert "logging " + boundary + " sentinel" in result.stdout
        cases = list(ET.parse(project.root / "result.xml").iter("testcase"))
        observed_events[engine] = json.loads((project.root / "events.json").read_text())
        observed_cases[engine] = [
            (
                case.get("name"),
                [child.tag for child in case if child.tag in {"error", "failure", "skipped"}],
                [
                    (child.tag, child.text)
                    for child in case
                    if child.tag in {"system-out", "system-err"}
                ],
            )
            for case in cases
        ]
    assert observed_events["pytest-native"] == observed_events["pytest"]
    assert observed_cases["pytest-native"] == observed_cases["pytest"]
    expected = {
        ("setup", "enter"): ["error"],
        ("setup", "exit"): ["error"],
        ("call", "enter"): ["failure"],
        ("call", "exit"): ["failure"],
        ("teardown", "enter"): ["error", "error"],
        ("teardown", "exit"): ["error"],
    }
    expected_outcomes = expected[(phase, boundary)]
    if early_stop:
        expected_outcomes = expected_outcomes[:1]
    for engine in ("pytest", "pytest-native"):
        assert [
            tag for _, outcomes, _ in observed_cases[engine] for tag in outcomes
        ] == expected_outcomes
