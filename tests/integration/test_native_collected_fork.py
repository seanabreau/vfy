"""Prove the explicit collected-snapshot worker contract through public commands."""

from __future__ import annotations

import json
import os
import signal
import sys
import xml.etree.ElementTree as ET
from contextlib import suppress
from pathlib import Path

import pytest

from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="collected fork workers require Linux"
)

_FORK_ENV = {
    "VFY_WORKER_START": "fork",
    "VFY_NATIVE_SCHEDULING": "cases",
    "VFY_WORKER_COLLECTION": "0",
}


def _command(
    project: VfyProject, *, fork: bool, junit: bool = False, basetemp: Path | None = None
) -> CommandResult:
    arguments = [
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--history",
        "history.json",
        "--no-replay",
        "--no-cov",
        "--capture=sys",
        "-q",
    ]
    if basetemp is not None:
        arguments += ["--basetemp", str(basetemp)]
    if junit:
        arguments += [
            "--junitxml=results.xml",
            "-o",
            "junit_logging=all",
            "-o",
            "junit_family=xunit1",
        ]
    return project.run_vfy(
        *arguments,
        env=_FORK_ENV if fork else {**_FORK_ENV, "VFY_WORKER_START": "exec"},
    )


def _case_records(path: Path) -> list[tuple[object, ...]]:
    root = ET.parse(path).getroot()
    return sorted(
        (
            case.attrib.get("classname"),
            case.attrib["name"],
            case.attrib.get("file"),
            case.attrib.get("line"),
            tuple(
                (entry.tag, entry.attrib.get("type"))
                for entry in case
                if entry.tag in {"failure", "error", "skipped"}
            ),
            case.findtext("system-out"),
            case.findtext("system-err"),
        )
        for case in root.iter("testcase")
    )


def test_collected_fork_fixture_junit_smoke(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_cases.py",
        """
import os
from pathlib import Path
import pytest

@pytest.fixture(scope='session')
def resource():
    pid = os.getpid()
    yield pid
    Path(f'finalized.{pid}').touch()

@pytest.mark.parametrize('value', [0, 1])
def test_case(value, resource, tmp_path, tmpdir):
    assert resource == os.getpid()
    assert tmp_path == Path(str(tmpdir))
    assert tmp_path.parent.parent == Path("requested-base").resolve()
    assert tmp_path.parent.name in {"native-worker-0", "native-worker-1"}
    print(f'captured {value}')
    Path(f'ran.{value}').write_text(str(resource))
""",
    )
    completed = _command(project, fork=True, junit=True, basetemp=tmp_path / "requested-base")
    completed.expect(0)
    assert "2 passed" in completed.stdout
    pids = {(tmp_path / f"ran.{value}").read_text() for value in (0, 1)}
    assert len(pids) == 2
    assert all((tmp_path / f"finalized.{pid}").exists() for pid in pids)
    cases = _case_records(tmp_path / "results.xml")
    assert len(cases) == 2
    assert all(case[2] == "tests/test_cases.py" and case[3] is not None for case in cases)
    assert all(f"captured {value}" in str(case[5]) for value, case in enumerate(cases))
    assert (tmp_path / "history.json").is_file()


def test_collected_fork_preserves_reports_and_isolates_runtime_state(tmp_path: Path) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("fresh", "fork")}
    for project in projects.values():
        project.write(
            "conftest.py",
            """
import json, os, warnings
from pathlib import Path
import pytest

def pytest_collection_finish(session):
    with open('collections.jsonl', 'a') as stream:
        stream.write(json.dumps({'pid': os.getpid(), 'count': len(session.items)}) + '\\n')
    warnings.warn('once-only collection warning', UserWarning)

@pytest.fixture(scope='session')
def state(request):
    value = {'pid': os.getpid(), 'cases': []}
    yield value
    Path(f'finalized.{os.getpid()}.json').write_text(json.dumps(value))

@pytest.fixture
def phases():
    print('setup output')
    yield
    print('teardown output')
""",
        )
        project.write(
            "tests/test_cases.py",
            """
import json, os, sys, warnings
from pathlib import Path
import pytest

import_pid = os.getpid()
saved_stdout = sys.stdout

@pytest.mark.parametrize('value', range(4))
def test_case(value, state, phases, tmp_path, request):
    assert type(request.node) is pytest.Function
    assert type(vars(request.node)) is dict
    assert not saved_stdout.closed
    saved_stdout.write(f'alias output {value}\\n')
    print(f'body output {value}')
    print(f'body error {value}', file=sys.stderr)
    warnings.warn(f'case warning {value}', UserWarning)
    state['cases'].append(value)
    assert state['pid'] == os.getpid()
    Path(f'case.{value}.json').write_text(json.dumps({
        'pid': os.getpid(), 'import_pid': import_pid,
        'inventory': len(request.session.items), 'tmp': str(tmp_path),
    }))
    if value == 1:
        pytest.skip('intentional skip')
    if value == 2:
        pytest.xfail('intentional xfail')
""",
        )
    for name, project in projects.items():
        completed = _command(project, fork=name == "fork", junit=True)
        completed.expect(0)
        assert "2 passed" in completed.stdout
        assert "1 skipped" in completed.stdout
        assert "1 xfailed" in completed.stdout
        records = [
            json.loads((project.root / f"case.{value}.json").read_text()) for value in range(4)
        ]
        pids = {record["pid"] for record in records}
        assert len(pids) == 2
        assert all(record["inventory"] == 4 for record in records)
        assert len({record["tmp"] for record in records}) == 4
        finalized = [json.loads(path.read_text()) for path in project.root.glob("finalized.*.json")]
        assert sorted(sorted(record["cases"]) for record in finalized) == [[0, 1], [2, 3]]
        collections = [
            json.loads(line)
            for line in (project.root / "collections.jsonl").read_text().splitlines()
        ]
        assert len(collections) == (1 if name == "fork" else 3)
        if name == "fork":
            assert all(record["import_pid"] == collections[0]["pid"] for record in records)
            assert collections[0]["pid"] not in pids
        else:
            assert all(record["import_pid"] == record["pid"] for record in records)
    assert _case_records(projects["fresh"].root / "results.xml") == _case_records(
        projects["fork"].root / "results.xml"
    )


@pytest.mark.parametrize(
    "boundary",
    ["thread", "resource", "cleanup", "cleanup_owner", "lifecycle", "lifecycle_spoof", "os_alias"],
)
def test_collected_fork_rejects_unsafe_boundaries_before_bodies(
    tmp_path: Path, boundary: str
) -> None:
    project = VfyProject(tmp_path)
    setup = {
        "thread": (
            "import threading\n"
            "threading.Thread(target=threading.Event().wait, daemon=True).start()\n"
        ),
        "resource": "resource = open('held-resource', 'w')\n",
        "cleanup": (
            "def pytest_collection_finish(session):\n    session.config.add_cleanup(lambda: None)\n"
        ),
        "cleanup_owner": (
            "from _pytest.monkeypatch import MonkeyPatch\n"
            "def pytest_collection_finish(session):\n"
            "    patch = MonkeyPatch()\n    patch.setenv('foreign-state', '1')\n"
            "    session.config.add_cleanup(patch.undo)\n"
        ),
        "lifecycle": "def pytest_sessionfinish(session):\n    pass\n",
        "lifecycle_spoof": (
            "def pytest_sessionfinish(session):\n    pass\n"
            "pytest_sessionfinish.__module__ = '_pytest.main'\n"
        ),
        "os_alias": "def pytest_collection_finish(session):\n    os.fork = os.getpid\n",
    }[boundary]
    project.write("conftest.py", "import os\n" + setup)
    project.write(
        "tests/test_case.py",
        "from pathlib import Path\ndef test_body():\n    Path('body-ran').touch()\n",
    )
    completed = _command(project, fork=True)
    assert completed.returncode != 0
    assert "collected fork workers" in completed.stdout + completed.stderr
    assert not (tmp_path / "body-ran").exists()


def test_collected_fork_reaps_descendants_after_interrupt(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "child.py",
        "import fcntl, signal\nfrom pathlib import Path\n"
        "locked = open('descendant.lock', 'w')\n"
        "fcntl.flock(locked, fcntl.LOCK_EX)\n"
        "Path('descendant.ready').touch()\nsignal.pause()\n",
    )
    project.write(
        "tests/test_case.py",
        """
import os, signal, subprocess, sys
from pathlib import Path

def test_running():
    child = subprocess.Popen([sys.executable, 'child.py'])
    import time
    while not Path('descendant.ready').exists():
        time.sleep(0.01)
    Path('pids').write_text(f'{os.getpid()} {child.pid}')
    print('fork body ready', file=sys.stderr, flush=True)
    signal.pause()
""",
    )
    command = project.start_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "--no-cov",
        "-s",
        "-q",
        env=_FORK_ENV,
    )
    try:
        project.wait_for_path("pids")
        os.kill(command.process.pid, signal.SIGINT)
        completed = command.wait(timeout=10)
        assert completed.returncode != 0
        worker_pid, _ = map(int, (tmp_path / "pids").read_text().split())
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
        import fcntl  # noqa: PLC0415 - Linux-only boundary

        with (tmp_path / "descendant.lock").open() as locked:
            fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if command.poll() is None:
            command.terminate()
        if (tmp_path / "pids").exists():
            for pid in map(int, (tmp_path / "pids").read_text().split()):
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


def test_collected_fork_bounds_child_startup_before_bodies(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """
import os, time

def pytest_collection_finish(session):
    os.register_at_fork(after_in_child=lambda: time.sleep(20))
""",
    )
    project.write(
        "tests/test_case.py",
        "from pathlib import Path\ndef test_body():\n    Path('body-ran').touch()\n",
    )
    completed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "--no-cov",
        "--capture=sys",
        "-q",
        env={**_FORK_ENV, "VFY_FORK_STARTUP_SECONDS": "1"},
    )
    completed.expect(3)
    assert "startup deadline" in completed.stdout + completed.stderr
    assert not (tmp_path / "body-ran").exists()


@pytest.mark.parametrize("fork", [False, True])
def test_collected_worker_preserves_fixture_exit_status(tmp_path: Path, fork: bool) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_case.py",
        """
import pytest

@pytest.fixture
def resource():
    yield
    pytest.exit('fixture requested exit', returncode=7)

def test_case(resource):
    pass
""",
    )
    completed = _command(project, fork=fork)
    completed.expect(7)
    assert "fixture requested exit" in completed.stdout + completed.stderr
