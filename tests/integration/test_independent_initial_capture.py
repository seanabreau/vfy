"""The upstream initial-conftest default runs inside capture and user wrappers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("failure", ["none", "conftest", "after"])
@pytest.mark.parametrize("mode", ["fd", "no"])
def test_initial_wrapper_order_registration_and_output_match_pytest(
    tmp_path: Path, failure: str, mode: str
) -> None:
    _initial_capture_project(tmp_path, failure=failure)
    _compare_initial_capture(tmp_path, [f"--capture={mode}"])


def test_initial_capture_uses_the_actual_known_namespace_at_wrapper_entry(tmp_path: Path) -> None:
    _initial_capture_project(tmp_path, failure="none", known_capture="sys")
    _compare_initial_capture(tmp_path, ["--capture=fd"])


def _initial_capture_project(root: Path, *, failure: str, known_capture: str | None = None) -> None:
    project = VfyProject(root)
    project.write(
        "initial_state.py",
        """import json
from pathlib import Path

config = None

def observe(label):
    manager = None if config is None else config.pluginmanager
    conftest = str(Path('conftest.py').resolve())
    with Path('initial.jsonl').open('a') as stream:
        stream.write(json.dumps([label,
                                None if manager is None else manager.has_plugin('capturemanager'),
                                None if manager is None else manager.has_plugin(conftest)]) + '\\n')
    print('INITIAL:' + label, flush=True)
""",
    )
    for name in ("first", "normal", "last"):
        project.write(
            f"timing_{name}.py",
            f"""import pytest
import initial_state as state

state.observe('plugin:{name}')

@pytest.hookimpl(wrapper=True, tryfirst={name == "first"}, trylast={name == "last"})
def pytest_load_initial_conftests(early_config):
    state.config = early_config
    if {name == "first" and known_capture is not None}:
        early_config.known_args_namespace.capture = {known_capture!r}
    state.observe('{name}:before')
    try:
        yield
    finally:
        state.observe('{name}:after')
        if {failure == "after" and name == "normal"}:
            raise RuntimeError('initial outer wrapper witness')

def pytest_plugin_registered(plugin_name):
    if plugin_name.endswith('conftest.py'):
        state.observe('registered')
""",
        )
    project.write(
        "conftest.py",
        f"""import json, os
from pathlib import Path
import initial_state as state

state.observe('conftest')
if {failure == "conftest"}:
    raise RuntimeError('initial conftest witness')

def pytest_runtest_logreport(report):
    with Path('phases.jsonl').open('a') as stream:
        stream.write(json.dumps([report.when, report.outcome,
                                report.capstdout, report.capstderr]) + '\\n')
""",
    )
    project.write(
        "test_initial.py",
        """import os
import initial_state as state

state.observe('module')

def test_body():
    state.observe('call')
    os.write(1, b'INITIAL:direct descriptor\\n')
""",
    )


def _compare_initial_capture(root: Path, arguments: list[str]) -> None:
    project = VfyProject(root)
    plugins = ["-p", "timing_first", "-p", "timing_normal", "-p", "timing_last"]
    stock = project.run_python("-m", "pytest", "-q", *plugins, *arguments)
    journal = root / "initial.jsonl"
    expected = _journal(journal)
    assert [row.label for row in expected if ":" in row.label][3:6] == [
        "first:before",
        "normal:before",
        "last:before",
    ]
    journal.rename(root / "stock-initial.jsonl")
    phases = root / "phases.jsonl"
    expected_phases = _json_lines(phases) if phases.exists() else []
    if phases.exists():
        phases.rename(root / "stock-phases.jsonl")
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", *plugins, *arguments)
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert _journal(journal) == expected
    actual_phases = _json_lines(phases) if phases.exists() else []
    assert actual_phases == expected_phases
    for stream in ("stdout", "stderr"):
        assert sorted(re.findall(r"INITIAL:[^\r\n]*", getattr(candidate, stream))) == sorted(
            re.findall(r"INITIAL:[^\r\n]*", getattr(stock, stream))
        )


class JournalEntry(NamedTuple):
    label: str
    capturing: bool | None
    conftest: bool | None


def _journal(path: Path) -> list[JournalEntry]:
    return [JournalEntry(*json.loads(line)) for line in path.read_text().splitlines()]


def _json_lines(path: Path) -> list[list[object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]
