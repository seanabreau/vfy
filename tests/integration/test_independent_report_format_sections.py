"""Formatter callbacks cannot leave authoritative report sections stale."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("formatter", ["longrepr", "wasxfail"])
@pytest.mark.parametrize("action", ["replace", "append", "clear"])
def test_report_formatter_section_edits_match_final_native_transport(
    tmp_path: Path, formatter: str, action: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "report_plugin.py",
        f"""import atexit
import json
from pathlib import Path

reports = []

class Formatter:
    def __init__(self, report):
        self.report = report
        self.calls = 0

    def __str__(self):
        self.calls += 1
        if {action!r} == 'replace':
            self.report.sections = [('formatter edit', str(self.calls))]
        elif {action!r} == 'append':
            self.report.sections.append(('formatter edit', str(self.calls)))
        else:
            self.report.sections.clear()
        self.report.outcome = 'failed'
        return 'formatted failure'

def pytest_runtest_logreport(report):
    reports.append(report)
    if report.when == 'call':
        report.sections = [('before format', 'initial')]
        report.{formatter} = Formatter(report)

def pytest_unconfigure():
    if {action!r} == 'clear':
        for report in reports:
            if report.when == 'call':
                report.sections = [('late edit', 'must be cleared')]

def record_live_report():
    report = next(report for report in reports if report.when == 'call')
    Path('live.json').write_text(json.dumps({{
        'sections': report.sections,
        'outcome': report.outcome,
        'calls': report.{formatter}.calls,
    }}))

# Runs after the runner has published its result.
atexit.register(record_live_report)
""",
    )
    project.write("test_case.py", "def test_case(): assert False\n")
    oracle = project.run_engine("stock", "-q", "-p", "report_plugin")
    oracle.expect(1)
    stock = json.loads((tmp_path / "live.json").read_text())
    assert stock["outcome"] == "failed"
    (tmp_path / "live.json").unlink()

    candidate = project.run_engine(
        "native", "-p", "report_plugin", "--native-result-json=result.json"
    )
    candidate.expect(1)
    live = json.loads((tmp_path / "live.json").read_text())
    assert live["calls"] > 0
    raw = json.loads((tmp_path / "result.json").read_text())
    phase = next(phase for phase in raw["cases"][0]["phases"] if phase["phase"] == "call")
    assert [[section["name"], section["content"]] for section in phase["report_sections"]] == live[
        "sections"
    ]
    assert phase["outcome"] == live["outcome"]
