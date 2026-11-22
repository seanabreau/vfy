"""Filtered coverage runs retain reports and history without unused replay identity."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.history import read_history


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
@pytest.mark.parametrize(
    ("selector", "exit_status"),
    [("node", 0), ("node", 1), ("keyword", 0), ("keyword", 1)],
)
def test_filtered_coverage_preserves_execution_reports_history_and_passing_baseline(
    tmp_path: Path, selector: str, exit_status: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n.pytest_cache/\n__pycache__/\n")
    project.write(".vfy/executions", "")
    project.write("app.py", "def chosen():\n    return 1\n\ndef other():\n    return 2\n")
    project.write(
        "tests/test_app.py",
        "from pathlib import Path\n"
        "from app import chosen, other\n\n"
        "def test_chosen():\n"
        "    with Path('.vfy/executions').open('a') as stream:\n"
        "        stream.write('chosen\\n')\n"
        "    assert chosen() == 1\n\n"
        "def test_other():\n"
        "    with Path('.vfy/executions').open('a') as stream:\n"
        "        stream.write('other\\n')\n"
        "    assert other() == 2\n",
    )
    project.write(
        "checked_cli.py",
        "import sys\n"
        "import vfy.run.controller\n"
        "from vfy.run import ci_baseline, ci_run, replay, selected_run\n"
        "from vfy.cli import main\n\n"
        "def unused(*args, **kwargs):\n"
        "    raise AssertionError('filtered run requested unused replay identity')\n\n"
        "replay._installed_dependency_fingerprint = unused\n"
        "for module in (ci_baseline, ci_run, selected_run):\n"
        "    module._read_test_coverage = unused\n"
        "replay.git_replay_state = unused\n"
        "sys.argv[0] = 'vfy'\n"
        "raise SystemExit(main())\n",
    )
    project.git_init()
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": None}
    baseline = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=1",
        "--cov=app.py",
        "--cov-report=",
        "-q",
        env=environment,
    )
    baseline.expect(0)
    baseline_path = tmp_path / ".vfy/replay.coverage"
    baseline_bytes = baseline_path.read_bytes()
    if exit_status:
        project.write("app.py", "def chosen():\n    return -1\n\ndef other():\n    return 2\n")
    arguments = ["test", "--workers=1"]
    if selector == "node":
        arguments.append("tests/test_app.py::test_chosen")
    arguments.extend(("--cov=app.py", "--cov-report=json:.vfy/filtered.json", "-q"))
    if selector == "keyword":
        arguments.extend(("-k", "chosen"))

    result = project.run_python("checked_cli.py", *arguments, env=environment)

    assert result.returncode == exit_status, result.stdout + result.stderr
    assert ("1 failed" if exit_status else "1 passed") in result.stdout
    assert "Reused" not in result.stderr
    assert "coverage is disabled" not in result.stderr
    assert (tmp_path / ".vfy/executions").read_text().splitlines() == [
        "chosen",
        "other",
        "chosen",
    ]
    assert baseline_path.read_bytes() == baseline_bytes
    assert {
        context for context in project.data().measured_contexts() if context.endswith("|run")
    } == {"tests/test_app.py::test_chosen|run"}
    report = json.loads((tmp_path / ".vfy/filtered.json").read_text())
    assert report["files"]["app.py"]["executed_lines"] == [1, 2, 4]
    assert report["files"]["app.py"]["missing_lines"] == [5]
    history = read_history(tmp_path / ".vfy/test-history.sqlite3")["tests"]
    selected = history["tests/test_app.py::test_chosen"]["observations"]
    assert len(selected) == 2
    assert selected[-1]["outcome"] == ("failed" if exit_status else "passed")
    assert len(history["tests/test_app.py::test_other"]["observations"]) == 1
