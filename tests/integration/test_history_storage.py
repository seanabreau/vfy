"""Default history uses SQLite and explicit storage choices are honored."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from tests.integration.test_watch import _wait_for_watch_event, _wait_for_watch_path
from tests.support import VfyProject
from tests.support.history import read_history


@pytest.mark.parametrize(
    "engine",
    [
        pytest.param(
            "native",
            marks=pytest.mark.skipif(
                os.name == "nt", reason="independent execution requires POSIX"
            ),
        ),
        "pytest",
    ],
)
def test_default_history_records_outcomes_in_sqlite(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write("test_body.py", "def test_body(): pass\n")
    arguments = ("test", f"--engine={engine}", "--no-cov", "-j1", "-q")

    first = project.run_vfy(*arguments)

    first.expect(0)
    destination = tmp_path / ".vfy/test-history.sqlite3"
    assert destination.read_bytes().startswith(b"SQLite format 3\x00")
    observed = read_history(destination)["tests"]
    assert observed["test_body.py::test_body"]["observations"][-1]["outcome"] == "passed"
    assert not (tmp_path / ".vfy/test-history.json").exists()

    second = project.run_vfy(*arguments)

    second.expect(0)
    observed = read_history(destination)["tests"]
    assert len(observed["test_body.py::test_body"]["observations"]) == 2


@pytest.mark.parametrize("setting", ["command", "configuration"])
def test_explicit_json_history_preserves_json_fields_and_updates_outcomes(
    tmp_path: Path, setting: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_body.py", "def test_body(): pass\n")
    path = ".vfy/test-history.json"
    project.write(path, json.dumps({"custom": ["retained"], "tests": {"retained": 17}}))
    if setting == "configuration":
        project.write("pyproject.toml", f'[tool.vfy.test]\nhistory = "{path}"\n')
    arguments = ["--history", path] if setting == "command" else []

    result = project.run_vfy("test", "--engine=pytest", "--no-cov", *arguments, "-q")

    result.expect(0)
    observed = json.loads((tmp_path / path).read_text())
    assert observed["custom"] == ["retained"]
    assert observed["tests"]["retained"] == 17
    assert observed["tests"]["test_body.py::test_body"]["observations"][-1]["outcome"] == "passed"
    assert not (tmp_path / ".vfy/test-history.sqlite3").exists()


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
def test_watch_nested_history_write_completes_and_preserves_accepted_outputs(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "VALUE = 1\n")
    project.write("nested_test.py", "def test_nested():\n    pass\n")
    project.write(
        "test_body.py",
        """import subprocess
import sys
from pathlib import Path
from subject import VALUE
def test_body():
    if VALUE == 2:
        subprocess.run([
            sys.executable,
            "-m", "vfy", "test", "--no-cov", "--no-replay", "-j1", "-q", "nested_test.py",
        ], check=True, timeout=30, capture_output=True)
        Path('.vfy/nested-done').touch()
    assert VALUE in (1, 2)
""",
    )
    command = project.start_vfy(
        "test",
        "--engine=native",
        "--watch",
        "--source=subject.py",
        "--cov-report=json:coverage.json",
        "-q",
    )
    try:
        baseline = _wait_for_watch_event(command, after_run=0, expected="pytest exited 0")
        accepted = {name: (tmp_path / name).read_bytes() for name in (".coverage", "coverage.json")}
        project.write("subject.py", "VALUE = 2\n")
        _wait_for_watch_path(command, project, ".vfy/nested-done", timeout=10)
        _wait_for_watch_event(command, after_run=baseline, expected="publication error:")
        assert "test history changed during watch execution; save a file to retry" in command.text()
        assert {name: (tmp_path / name).read_bytes() for name in accepted} == accepted
        history = read_history(tmp_path / ".vfy/test-history.sqlite3")["tests"]
        assert history["nested_test.py::test_nested"]["observations"][-1]["outcome"] == "passed"
        assert len(history["test_body.py::test_body"]["observations"]) == 1
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    result.expect(2)
