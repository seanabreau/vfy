"""Private imports identify objects passed to third-party plugin hooks."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_terminal_reporter_alias_matches_stock(tmp_path: Path) -> None:
    observations = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """from _pytest.config import Config
from _pytest.config.argparsing import Parser
from _pytest.terminal import TerminalReporter

def pytest_addoption(parser):
    assert isinstance(parser, Parser)

def pytest_configure(config):
    assert isinstance(config, Config)

def pytest_terminal_summary(terminalreporter):
    assert isinstance(terminalreporter, TerminalReporter)
""",
        )
        project.write(
            "test_reporter.py",
            """import json
from pathlib import Path
from _pytest.terminal import TerminalReporter

def test_reporter(request):
    reporter = request.config.pluginmanager.getplugin('terminalreporter')
    Path('reporter.json').write_text(json.dumps({
        'registered': reporter is not None,
        'instance': isinstance(reporter, TerminalReporter),
        'exact_type': type(reporter) is TerminalReporter,
    }))
""",
        )
        if engine == "stock":
            result = project.run_pytest("-q")
        else:
            result = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        result.expect(0)
        observations.append(json.loads((project.root / "reporter.json").read_text()))

    assert observations == [
        {"registered": True, "instance": True, "exact_type": True},
        {"registered": True, "instance": True, "exact_type": True},
    ]
