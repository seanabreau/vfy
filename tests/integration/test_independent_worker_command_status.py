"""Two-worker command and session status through the public interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

Engine = Literal["stock", "independent"]
_ENV = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}

_STATUS_PLUGIN = """import json
import os
from pathlib import Path
import pytest

def event(config, kind, **values):
    worker = getattr(config, 'workerinput', {}).get('workerid')
    if worker is None:
        worker = os.environ.get('VFY_NATIVE_PARALLEL_INDEX')
    payload = dict(event=kind, pid=os.getpid(), worker=worker, **values)
    with Path('events-' + str(os.getpid()) + '.jsonl').open('a') as stream:
        stream.write(json.dumps(payload) + '\\n')

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_cmdline_main(config):
    event(config, 'command-enter')
    status = yield
    event(config, 'command-return', incoming=int(status), outgoing=4)
    assert int(status) == 0
    return 4

def pytest_sessionfinish(session, exitstatus):
    event(session.config, 'sessionfinish', status=int(exitstatus))

def pytest_unconfigure(config):
    event(config, 'unconfigure')
"""

_STATUS_TEST = """from status_plugin import event

def test_body(request):
    event(request.config, 'body')
"""


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
def test_worker_command_status_preserves_each_session_result(
    tmp_path: Path, engine: Engine
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("status_plugin.py", _STATUS_PLUGIN)
    project.write("test_a.py", _STATUS_TEST)
    project.write("test_b.py", _STATUS_TEST)
    arguments = ("-q", "-p", "xdist.plugin", "-p", "status_plugin")
    record = None
    if engine == "stock":
        result = project.run_pytest(*arguments, "-n2", load_xdist=False, env=_ENV)
    else:
        result = project.run_python(
            "-m",
            "vfy.independent.entry",
            "--workers=2",
            "--native-result-json=actual.json",
            *arguments,
            env=_ENV,
        )
    project.write("stdout.txt", result.stdout)
    project.write("stderr.txt", result.stderr)
    project.write("returncode.json", json.dumps(result.returncode))
    result.expect(4)
    if engine == "native":
        record = json.loads((tmp_path / "actual.json").read_text())
        assert record["exitstatus"] == 0
        assert record["counts"]["passed"] == 2
        topology = record["execution_topology"]
        assert topology["active_workers"] == 2
        assert len(set(topology["worker_pids"])) == 2
    assert "2 passed" in result.stdout
    events = [
        json.loads(line)
        for path in tmp_path.glob("events-*.jsonl")
        for line in path.read_text().splitlines()
    ]
    sessions = [row for row in events if row["event"] == "sessionfinish"]
    assert len(sessions) == 3
    assert all(row["status"] == 0 for row in sessions)
    worker_pids = {row["pid"] for row in sessions if row["worker"] is not None}
    assert len(worker_pids) == 2
    if engine == "native":
        assert record is not None
        assert set(record["execution_topology"]["worker_pids"]) == worker_pids
    assert {row["pid"] for row in events if row["event"] == "body"} == worker_pids
    commands = [row for row in events if row["event"] == "command-return"]
    assert len(commands) == 3
    assert all(row["incoming"] == 0 and row["outgoing"] == 4 for row in commands)
    for pid in {row["pid"] for row in sessions}:
        kinds = [row["event"] for row in events if row["pid"] == pid]
        assert kinds[0] == "command-enter"
        assert kinds[-3:] == ["sessionfinish", "unconfigure", "command-return"]
