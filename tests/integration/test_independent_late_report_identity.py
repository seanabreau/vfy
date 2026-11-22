"""Late hook registration observes already-published report state."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_late_report_observer_preserves_prior_stats_and_phase_snapshots(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_late.py",
            """\
import json
from pathlib import Path

state = {'events': []}

class LateObserver:
    def pytest_runtest_logreport(self, report):
        if report.nodeid.endswith('::test_first'):
            state['events'].append([report.when, list(report.keywords)])
            if report.when == 'call':
                state['call_report'] = report

def test_first(request):
    terminal = request.config.pluginmanager.getplugin('terminalreporter')
    stats = terminal.stats
    prior = next(report for report in stats['']
                 if report.nodeid == request.node.nodeid and report.when == 'setup')
    assert 'late-key' not in prior.keywords
    prior.keywords['edited-key'] = 1
    prior.sections.append(('retained', 'edited during body'))
    request.node.keywords['late-key'] = 1
    observer = LateObserver()
    request.config.pluginmanager.register(observer, 'late-report-observer')
    state.update(stats=stats, prior=prior, observer=observer)

def test_second(request):
    terminal = request.config.pluginmanager.getplugin('terminalreporter')
    assert terminal.stats is state['stats']
    prior = next(report for report in terminal.stats['']
                 if report.nodeid.endswith('::test_first') and report.when == 'setup')
    call = next(report for report in terminal.stats['passed']
                if report.nodeid.endswith('::test_first'))
    assert prior is state['prior']
    assert call is state['call_report']
    observed = {
        'same_stats': terminal.stats is state['stats'],
        'same_setup': prior is state['prior'],
        'same_call': call is state['call_report'],
        'setup_edited': 'edited-key' in prior.keywords,
        'setup_has_late': 'late-key' in prior.keywords,
        'call_has_late': 'late-key' in call.keywords,
        'sections': prior.sections,
        'observed_phases': [entry[0] for entry in state['events']],
    }
    request.config.pluginmanager.unregister(state['observer'])
    Path('observed.json').write_text(json.dumps(observed))
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
        assert "2 passed" in result.stdout
        observations[engine] = json.loads((project.root / "observed.json").read_text())
    assert (
        observations["native"]
        == observations["pytest"]
        == {
            "same_stats": True,
            "same_setup": True,
            "same_call": True,
            "setup_edited": True,
            "setup_has_late": False,
            "call_has_late": True,
            "sections": [["retained", "edited during body"]],
            "observed_phases": ["call", "teardown"],
        }
    )
