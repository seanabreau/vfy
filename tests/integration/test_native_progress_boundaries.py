"""Compare progress wrapping with pytest when terminal widths change."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("width", ["writer", "screen", "environment"])
def test_progress_wrap_uses_reporter_screen_width(tmp_path: Path, width: str) -> None:
    changes = {
        "writer": "reporter._tw.fullwidth = 28 if case == 0 else 96",
        "screen": "reporter._screen_width = 36 if case == 0 else 64",
        "environment": "os.environ['COLUMNS'] = '48' if case == 0 else '96'",
    }
    source = f"""import os
import pytest

@pytest.mark.parametrize('case', range(75))
def test_case(case, request):
    if case in (0, 20):
        reporter = request.config.pluginmanager.getplugin('terminalreporter')
        {changes[width]}
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            "import pytest\n\n"
            "@pytest.hookimpl(trylast=True)\n"
            "def pytest_configure(config):\n"
            "    reporter = config.pluginmanager.getplugin('terminalreporter')\n"
            "    reporter._screen_width = 48\n"
            f"    reporter._tw.fullwidth = {None if width == 'environment' else 48!r}\n",
        )
        project.write("test_case.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--color=no",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "COLUMNS": "48"},
        )
        result.expect(0)
        assert "75 passed" in result.stdout
        observed[engine] = [
            line for line in result.stdout.splitlines() if re.fullmatch(r"\.+\s+\[\s*\d+%\]", line)
        ]
    assert observed["pytest-native"] == observed["pytest"]
    assert sum(line.count(".") for line in observed["pytest-native"]) == 75


@pytest.mark.parametrize("verbosity", [[], ["-q"]])
def test_start_preserves_terminal_flushes_and_output_capture(
    tmp_path: Path, verbosity: list[str]
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """import pytest

@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    writer = config.pluginmanager.getplugin('terminalreporter')._tw
    original = writer.flush
    config.flush_events = []
    def flush():
        config.flush_events.append('flush')
        original()
    writer.flush = flush
""",
        )
        project.write(
            "test_output.py",
            """import json
import os
from pathlib import Path

def test_first(request):
    request.config.first_flushes = len(request.config.flush_events)
    os.write(1, b'first body output\\n')

def test_second(request):
    delta = len(request.config.flush_events) - request.config.first_flushes
    Path('flushes.json').write_text(json.dumps(delta))
    os.write(2, b'second body error\\n')
    assert False, 'show captured output'
""",
        )
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", *verbosity, "--color=no"
        )
        result.expect(1)
        assert "first body output" not in result.stdout
        assert "second body error" in result.stdout
        observed[engine] = json.loads((project.root / "flushes.json").read_text())
    assert observed["pytest-native"] == observed["pytest"] == (1 if verbosity else 2)
