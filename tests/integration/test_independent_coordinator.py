"""Public worker frontend drives the independent coordinator through real hooks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()

_EVENTS = """import json
import os
from pathlib import Path

def note(event, **values):
    with Path(f'events-{os.getpid()}.jsonl').open('a') as output:
        output.write(json.dumps({'event': event, **values}) + '\\n')
"""


@pytest.mark.parametrize("mode", ["run", "collect", "unwind"])
def test_public_worker_frontend_runs_real_coordinator_hooks(tmp_path: Path, mode: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        _EVENTS
        + """import pytest

def pytest_configure(config):
    note('configure')
    config.add_cleanup(lambda: note('cleanup'))

def pytest_sessionstart(session):
    note('start', collected=session.testscollected, items=len(session.items))

@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session):
    note('loop enter', collected=session.testscollected, items=len(session.items))
    result = yield
    note('loop exit', collected=session.testscollected, items=len(session.items))
    if not os.environ.get('VFY_NATIVE_PARALLEL_DIR') and os.environ.get('FAIL_COORDINATOR'):
        raise RuntimeError('public coordinator unwind')
    return result

def pytest_sessionfinish(session, exitstatus):
    note('finish', collected=session.testscollected, items=len(session.items),
         status=int(exitstatus))

def pytest_unconfigure(config):
    note('unconfigure')
""",
    )
    for index in range(2):
        project.write(
            f"test_worker_{index}.py",
            _EVENTS
            + f"""note('test import', file={index})
def test_body():
    note('body', file={index})
""",
        )
    arguments = [
        "-m",
        "vfy",
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        "--native-result-json=result.json",
    ]
    if mode == "collect":
        arguments.append("--collect-only")
    result = project.run_python(
        *arguments,
        env={"PYTHONPATH": _SOURCE, "FAIL_COORDINATOR": "1" if mode == "unwind" else ""},
    )
    expected_status = 3 if mode == "unwind" else 0
    assert result.returncode == expected_status, (result.stdout, result.stderr)
    record = json.loads((tmp_path / "result.json").read_text())
    workers = set(record["execution_topology"]["worker_pids"])
    events = {
        int(path.stem.removeprefix("events-")): [
            json.loads(line) for line in path.read_text().splitlines()
        ]
        for path in tmp_path.glob("events-*.jsonl")
    }
    assert len(workers) == 2
    assert len(events) == 3
    (controller,) = events.keys() - workers
    parent = events[controller]
    assert [event["event"] for event in parent] == [
        "configure",
        "start",
        "loop enter",
        "loop exit",
        "finish",
        "unconfigure",
        "cleanup",
    ]
    assert parent[1] == {"event": "start", "collected": 0, "items": 0}
    assert parent[3] == {"event": "loop exit", "collected": 2, "items": 0}
    assert parent[4] == {
        "event": "finish",
        "collected": 2,
        "items": 0,
        "status": expected_status,
    }
    assert record["collected"] == 2
    assert record["counts"]["passed"] == (0 if mode == "collect" else 2)
    assert record["exitstatus"] == expected_status
    assert len(record["cases"]) == (0 if mode == "collect" else 2)
    assert bool(record.get("internal_errors", [])) == (mode == "unwind")
    bodies = [event for pid in workers for event in events[pid] if event["event"] == "body"]
    assert len(bodies) == (0 if mode == "collect" else 2)
