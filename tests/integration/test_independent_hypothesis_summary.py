"""Hypothesis summary scans live public report mappings in their original order."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_hypothesis_summary_reads_live_foreign_reports_and_duplicate_entries(
    tmp_path: Path,
) -> None:
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_summary.py",
            """\
import json
from pathlib import Path


def test_summary(request):
    class Observer:
        def pytest_terminal_summary(self, terminalreporter):
            terminal = terminalreporter
            events = []
            class Report:
                def __init__(self, name):
                    self.name = name
                    self._hypothesis_stats = name
                def __getattribute__(self, name):
                    if name == '__dict__':
                        events.append(['read', object.__getattribute__(self, 'name')])
                    return object.__getattribute__(self, name)
            class Stats(dict):
                def values(self):
                    events.append(['values'])
                    return super().values()
            first = Report('first')
            second = Report('second')
            hypothesis = terminal.config.pluginmanager.getplugin('hypothesispytest')
            old_stats, old_line, old_section = terminal.stats, terminal.write_line, terminal.section
            def write_line(text):
                events.append(['write', text])
                if text == 'first\\n\\n':
                    object.__getattribute__(second, '__dict__')['_hypothesis_stats'] = 'changed'
            terminal.stats = Stats({'custom': [first, second, first]})
            terminal.write_line = write_line
            terminal.section = lambda text: events.append(['section', text])
            try:
                hypothesis.pytest_terminal_summary(terminal)
            finally:
                terminal.stats = old_stats
                terminal.write_line = old_line
                terminal.section = old_section
            Path('events.json').write_text(json.dumps(events))
    request.config.pluginmanager.register(Observer(), "summary-observer")
""",
        )
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        args = ("-p", "hypothesispytest", "--hypothesis-show-statistics", "-q")
        result = (
            project.run_pytest(*args, load_xdist=False, env=env)
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=1",
                "--no-replay",
                "--no-cov",
                *args,
                env=env,
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observations[engine] = json.loads((project.root / "events.json").read_text())
    assert (
        observations["native"]
        == observations["pytest"]
        == [
            ["section", "Hypothesis Statistics"],
            ["values"],
            ["read", "first"],
            ["write", "first\n\n"],
            ["read", "first"],
            ["read", "second"],
            ["write", "changed\n\n"],
            ["read", "second"],
            ["read", "first"],
            ["write", "first\n\n"],
            ["read", "first"],
        ]
    )
