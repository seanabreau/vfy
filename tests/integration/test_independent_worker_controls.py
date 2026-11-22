"""Worker controls retain real prefixes and drain fixture-owned child processes."""

from __future__ import annotations

import json
import os
import signal
from contextlib import suppress
from pathlib import Path

import pytest

from tests.support import VfyProject


def _events(path: Path) -> list[str]:
    events: list[str] = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        assert isinstance(event, str)
        events.append(event)
    return events


def _demand_order(policy: str, nodeids: tuple[str, ...]) -> str:
    if policy != "demand":
        return ""
    return f"""
import os, time
from pathlib import Path
def wait_for_prepasses():
    if os.environ.get('CONTROL_ORACLE'):
        return
    deadline = time.monotonic() + 5
    while not all(Path(name).exists() for name in ('first.pid', 'second.pid')):
        assert time.monotonic() < deadline, 'both seeded workers must enter their prepass'
        time.sleep(0.01)
def pytest_collection_modifyitems(items):
    if os.environ.get('CONTROL_ORACLE'):
        return
    by_id = {{item.nodeid: item for item in items}}
    items[:] = [by_id[nodeid] for nodeid in {nodeids!r}]
"""


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX FIFOs, process groups and flock")
@pytest.mark.parametrize(
    ("control", "status"),
    [
        ("exit", 42),
        ("keyboard", 2),
        ("override", 77),
        ("override-one", 1),
        ("override-five", 5),
        ("override-zero", 0),
    ],
)
@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("policy", ["cases", "demand"])
def test_worker_control_preserves_prefix_and_drains_peer_cleanup(
    tmp_path: Path, control: str, status: int, detached: bool, policy: str
) -> None:
    import fcntl  # noqa: PLC0415 - POSIX only

    project = VfyProject(tmp_path)
    prepass_wait = (
        "\n    from conftest import wait_for_prepasses\n    wait_for_prepasses()"
        if policy == "demand"
        else ""
    )
    project.write(
        "conftest.py",
        f"""import json, os, pytest
from pathlib import Path

def note(event):
    path = Path('events-' + str(os.getpid()) + '.jsonl')
    with path.open('a') as stream:
        stream.write(json.dumps(event) + '\\n')

def pytest_configure(config):
    config.add_cleanup(lambda: note('cleanup'))

@pytest.fixture(autouse=True)
def observed(request):
    note('setup:' + request.node.nodeid)
    yield
    note('finalized:' + request.node.nodeid)

def pytest_sessionfinish(session, exitstatus):
    note('sessionfinish:' + str(int(exitstatus)))
    owner = Path('control.pid')
    if ({control!r}.startswith('override') and owner.exists()
            and owner.read_text() == str(os.getpid())):
        session.exitstatus = {status}

def pytest_unconfigure():
    note('unconfigure')
"""
        + _demand_order(
            policy,
            (
                "test_first.py::test_first_pass",
                "test_second.py::test_second_pass",
                "test_first.py::test_control",
                "test_second.py::test_wait",
                "test_first.py::test_first_never",
                "test_second.py::test_second_never",
            ),
        ),
    )
    project.write(
        "writer.py",
        """import fcntl, os, signal, sys
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_IGN)
with open('writer.lock', 'w') as stream:
    fcntl.flock(stream, fcntl.LOCK_EX)
    Path('writer.pid').write_text(str(os.getpid()))
    print('ready', flush=True)
    signal.pause()
""",
    )
    project.write(
        "test_first.py",
        f"""import os, pytest
from pathlib import Path

def test_first_pass():
    Path('first.pid').write_text(str(os.getpid())){prepass_wait}

def test_control():
    Path('control.pid').write_text(str(os.getpid()))
    if not os.environ.get('CONTROL_ORACLE'):
        with open('peer-ready') as stream:
            assert stream.read() == 'ready'
    if {control!r} == 'keyboard':
        raise KeyboardInterrupt('actual worker control')
    pytest.exit('actual worker control', returncode=42)

def test_first_never():
    Path('first-never').touch()
""",
    )
    project.write(
        "test_second.py",
        f"""import os, signal, subprocess, sys, pytest
from pathlib import Path

def test_second_pass():
    Path('second.pid').write_text(str(os.getpid())){prepass_wait}

@pytest.fixture
def writer():
    child = subprocess.Popen([sys.executable, 'writer.py'], stdout=subprocess.PIPE,
        text=True, start_new_session={detached!r})
    assert child.stdout.readline().strip() == 'ready'
    yield
    Path('writer-finalized').touch()
    if {detached!r}:
        child.terminate()
        child.wait(timeout=5)

def test_wait(writer):
    with open('peer-ready', 'w') as stream:
        stream.write('ready')
    signal.pause()

def test_second_never():
    Path('second-never').touch()
""",
    )
    os.mkfifo(tmp_path / "peer-ready")
    command = project.start_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        f"--native-scheduling={policy}",
        "--native-result-json=result.json",
    )
    try:
        result = command.wait(timeout=20)
        assert result.returncode == status, result.stdout + result.stderr
        record = json.loads((tmp_path / "result.json").read_text())
        assert record["engine"] == "vfy-independent-v1"
        assert record["exitstatus"] == status
        assert [case["nodeid"] for case in record["cases"]] == [
            "test_first.py::test_first_pass",
            "test_second.py::test_second_pass",
        ]
        assert record["counts"]["passed"] == 2
        assert record["collected"] == 6
        topology = record["execution_topology"]
        pids = {int((tmp_path / name).read_text()) for name in ["first.pid", "second.pid"]}
        assert len(pids) == 2
        assert set(topology["worker_pids"]) == pids
        assert topology["active_workers"] == 2
        assert not (tmp_path / "first-never").exists()
        assert not (tmp_path / "second-never").exists()
        assert (tmp_path / "writer-finalized").exists()
        control_pid = int((tmp_path / "control.pid").read_text())
        assert control_pid in pids
        first: list[str] | None = None
        for path in tmp_path.glob("events-*.jsonl"):
            events = _events(path)
            assert events.count("unconfigure") == 1
            assert events.count("cleanup") == 1
            if int(path.stem.removeprefix("events-")) in pids:
                assert sum(event.startswith("sessionfinish:") for event in events) == 1
                assert sum(event.startswith("finalized:") for event in events) == 2
                if "setup:test_first.py::test_control" in events:
                    assert int(path.stem.removeprefix("events-")) == control_pid
                    first = events
        assert first is not None
        prefix = [event.removeprefix("setup:") for event in first if event.startswith("setup:")]
        assert prefix[-1] == "test_first.py::test_control"
        oracle_project = VfyProject(tmp_path / "oracle")
        for name in ("conftest.py", "test_first.py", "test_second.py", "writer.py"):
            oracle_project.write(name, (tmp_path / name).read_text())
        stock = oracle_project.run_python(
            "-m",
            "pytest",
            "-q",
            *prefix,
            env={"CONTROL_ORACLE": "1", "VFY_NATIVE_PARALLEL_INDEX": "0"},
        )
        assert stock.returncode == status, stock.stdout + stock.stderr
        oracle_events = list(oracle_project.root.glob("events-*.jsonl"))
        assert len(oracle_events) == 1
        oracle = _events(oracle_events[0])
        assert oracle.count("finalized:test_first.py::test_control") == 1
        assert first == oracle
        with (tmp_path / "writer.lock").open() as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if command.poll() is None:
            command.kill()
            command.wait(timeout=5)
        writer = tmp_path / "writer.pid"
        if writer.exists():
            with suppress(ProcessLookupError):
                os.kill(int(writer.read_text()), signal.SIGKILL)
        for name in ["first.pid", "second.pid"]:
            path = tmp_path / name
            if path.exists():
                with suppress(ProcessLookupError):
                    os.killpg(int(path.read_text()), signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX FIFOs and process groups")
@pytest.mark.parametrize("peer", ["dies", "ignores"])
@pytest.mark.parametrize("policy", ["cases", "demand"])
def test_interrupted_peer_without_a_valid_completion_stays_an_error(
    tmp_path: Path, peer: str, policy: str
) -> None:
    project = VfyProject(tmp_path)
    prepass_wait = (
        "\n    from conftest import wait_for_prepasses\n    wait_for_prepasses()"
        if policy == "demand"
        else ""
    )
    project.write(
        "conftest.py",
        _demand_order(
            policy,
            (
                "test_first.py::test_pass",
                "test_second.py::test_pass",
                "test_first.py::test_control",
                "test_second.py::test_wait",
            ),
        ),
    )
    project.write(
        "test_first.py",
        f"""import os, pytest
from pathlib import Path

def test_pass():
    Path('first.pid').write_text(str(os.getpid())){prepass_wait}

def test_control():
    Path('control.pid').write_text(str(os.getpid()))
    with open('peer-ready') as stream:
        assert stream.read() == 'ready'
    pytest.exit('primary actual exit', returncode=42)
""",
    )
    project.write(
        "test_second.py",
        f"""import os, signal
from pathlib import Path

def test_pass():
    Path('second.pid').write_text(str(os.getpid())){prepass_wait}

def test_wait():
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    with open('peer-ready', 'w') as stream:
        stream.write('ready')
    if {peer!r} == 'dies':
        os._exit(17)
    signal.pause()
""",
    )
    os.mkfifo(tmp_path / "peer-ready")
    command = project.start_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        f"--native-scheduling={policy}",
        "--native-result-json=result.json",
    )
    try:
        result = command.wait(timeout=15)
        result.expect(3)
        assert not (tmp_path / "result.json").exists()
        assert (
            len({int((tmp_path / name).read_text()) for name in ["first.pid", "second.pid"]}) == 2
        )
        assert (
            "without a valid collection completion" if peer == "dies" else "drain deadline"
        ) in result.stdout
    finally:
        if command.poll() is None:
            command.kill()
            command.wait(timeout=5)
        for name in ["first.pid", "second.pid"]:
            path = tmp_path / name
            if path.exists():
                with suppress(ProcessLookupError):
                    os.killpg(int(path.read_text()), signal.SIGKILL)
