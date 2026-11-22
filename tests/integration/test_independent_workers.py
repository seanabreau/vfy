"""Fresh independent workers preserve phases, cleanup, and ordering."""

from __future__ import annotations

import json
import os
import signal
import xml.etree.ElementTree as ET
from contextlib import suppress
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest


def _pids(directory: Path, prefix: str) -> set[int]:
    return {int(path.name.removeprefix(prefix)) for path in directory.glob(prefix + "*")}


def _assert_controller_and_workers(markers: set[int], workers: set[int]) -> None:
    """The vfy controller runs the same session hooks but executes no test body."""

    assert workers < markers
    assert len(markers - workers) == 1


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX FIFOs")
@pytest.mark.parametrize("scheduling", [None, "cases"])
def test_default_parallel_maxfail_stops_successors_in_both_active_workers(
    tmp_path: Path, scheduling: str | None
) -> None:
    project = VfyProject(tmp_path, timeout=30)
    for name in ("peer-running", "failure-published"):
        os.mkfifo(tmp_path / name)
    conftest = project.write(
        "conftest.py",
        """import json, os
from pathlib import Path
import pytest

@pytest.fixture(scope='session', autouse=True)
def session_resource():
    yield
    Path(f'session-finished-{os.getpid()}').touch()

@pytest.fixture(autouse=True)
def function_resource(request):
    yield
    Path(request.node.nodeid + '.function-finished').touch()
    if request.node.nodeid == 'test_a.py::test_first':
        with open('failure-published', 'w') as ready:
            ready.write('ready')

def pytest_runtest_teardown(item, nextitem):
    Path(item.nodeid + '.next').write_text(json.dumps(
        None if nextitem is None else nextitem.nodeid))
""",
    )
    if scheduling is None:
        conftest.write_text(
            conftest.read_text() + "\ndef pytest_collection_modifyitems(items):\n"
            "    by_nodeid = {item.nodeid: item for item in items}\n"
            "    items[:] = [by_nodeid[nodeid] for nodeid in (\n"
            "        'test_a.py::test_first', 'test_b.py::test_first',\n"
            "        'test_a.py::test_successor', 'test_b.py::test_successor')]\n"
        )
    project.write(
        "test_a.py",
        """import os
from pathlib import Path

def test_first():
    Path('body-a.pid').write_text(str(os.getpid()))
    with open('peer-running') as ready:
        assert int(ready.read()) != os.getpid()
    assert False, 'shared budget failure'

def test_successor():
    Path('successor-a').touch()
""",
    )
    project.write(
        "test_b.py",
        """import os
from pathlib import Path

def test_first():
    Path('body-b.pid').write_text(str(os.getpid()))
    with open('peer-running', 'w') as ready:
        ready.write(str(os.getpid()))
    with open('failure-published') as ready:
        assert ready.read() == 'ready'
    assert Path('test_a.py::test_first.function-finished').exists()

def test_successor():
    Path('successor-b').touch()
""",
    )

    result = project.run_vfy(
        "test",
        "--engine=native",
        "--workers=2",
        *([f"--native-scheduling={scheduling}"] if scheduling else []),
        "--maxfail=1",
        "--no-cov",
        "--native-result-json=owned.json",
        "--junitxml=results.xml",
        "-q",
        env={"VFY_NATIVE_SCHEDULING": None},
    )

    result.expect(1)
    pids = {int((tmp_path / f"body-{name}.pid").read_text()) for name in ("a", "b")}
    assert len(pids) == 2
    assert _pids(tmp_path, "session-finished-") == pids
    for name in ("a", "b"):
        assert not (tmp_path / f"successor-{name}").exists()
        assert (tmp_path / f"test_{name}.py::test_first.function-finished").exists()
        assert json.loads((tmp_path / f"test_{name}.py::test_first.next").read_text()) is None
    record = json.loads((tmp_path / "owned.json").read_text())
    assert record["engine"] == "vfy-independent-v1"
    assert record["execution_topology"]["active_workers"] == 2
    assert set(record["execution_topology"]["worker_pids"]) == pids
    assert record["collected"] == 4
    assert [
        (case["nodeid"], [(phase["phase"], phase["outcome"]) for phase in case["phases"]])
        for case in record["cases"]
    ] == [
        (
            "test_a.py::test_first",
            [("setup", "passed"), ("call", "failed"), ("teardown", "passed")],
        ),
        (
            "test_b.py::test_first",
            [("setup", "passed"), ("call", "passed"), ("teardown", "passed")],
        ),
    ]
    assert [
        (case.get("classname"), case.get("name"), case.find("failure") is not None)
        for case in ET.parse(tmp_path / "results.xml").iter("testcase")
    ] == [("test_a", "test_first", True), ("test_b", "test_first", False)]


@requires_native_pytest
@pytest.mark.parametrize("requested_workers", [2, 3])
def test_two_independent_workers_match_serial_phases_and_worker_pids(
    tmp_path: Path, requested_workers: int
) -> None:
    project = VfyProject(tmp_path)
    environment = tmp_path / ".independent-python"
    created = project.run_python("-m", "venv", "--without-pip", str(environment))
    created.expect(0)
    project.write(
        "conftest.py",
        """import os
from pathlib import Path
import pytest

def note(nodeid, event):
    with Path(nodeid + '.events').open('a') as output:
        output.write(event + '\\n')

@pytest.fixture
def resource(request):
    note(request.node.nodeid, 'setup')
    request.addfinalizer(lambda: note(request.node.nodeid, 'registered finalizer'))
    yield
    note(request.node.nodeid, 'yield teardown')

@pytest.fixture
def broken(request, resource):
    request.addfinalizer(lambda: note(request.node.nodeid, 'failed setup finalizer'))
    raise ValueError('setup failure')

@pytest.fixture
def teardown_error(resource):
    yield
    raise RuntimeError('teardown failure')

def pytest_collection_finish(session):
    assert len(session.items) == 8

def pytest_sessionfinish(session, exitstatus):
    coordinator = (getattr(session.config.option, 'workers', 1) > 1
                   and 'VFY_NATIVE_PARALLEL_DIR' not in os.environ)
    assert session.testscollected == 8
    assert len(session.items) == (0 if coordinator else 8)
    Path(f'finished-{os.getpid()}').write_text(str(exitstatus))
""",
    )
    for filename in ("test_a.py", "test_b.py"):
        project.write(
            filename,
            """import os
from pathlib import Path

def test_pass(resource):
    Path(__file__ + '.pid').write_text(str(os.getpid()))

def test_call_failure(resource):
    raise LookupError('call failure')

def test_setup_failure(broken):
    raise AssertionError('unreachable test body')

def test_teardown_failure(teardown_error):
    pass
""",
        )
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(1)
    expected_events = {path.name: path.read_text() for path in tmp_path.glob("*.events")}
    for path in (*tmp_path.glob("*.events"), *tmp_path.glob("finished-*")):
        path.unlink()
    command = [str(environment / "bin" / "python"), "-m", "vfy.independent.entry"]
    serial = project.run(
        *command, "--native-result-json=serial.json", env={"PYTHONPATH": isolated_vfy_path()}
    )
    serial.expect(1)
    serial_record = json.loads((tmp_path / "serial.json").read_text())
    assert {path.name: path.read_text() for path in tmp_path.glob("*.events")} == expected_events
    for path in (*tmp_path.glob("*.events"), *tmp_path.glob("finished-*")):
        path.unlink()

    parallel = project.run(
        str(environment / "bin" / "python"),
        "-m",
        "vfy",
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={requested_workers}",
        # Each file's events must match its serial order, so keep files whole.
        "--native-scheduling=file",
        "--native-result-json=parallel.json",
        env={"PYTHONPATH": isolated_vfy_path(), "VFY_WORKER_START": "exec"},
    )

    parallel.expect(1)
    record = json.loads((tmp_path / "parallel.json").read_text())
    pids = {int(path.read_text()) for path in tmp_path.glob("*.pid")}
    assert len(pids) == 2
    assert set(record["execution_topology"]["worker_pids"]) == pids
    assert record["execution_topology"]["active_workers"] == 2
    assert record["execution_topology"]["requested_workers"] == requested_workers
    # A requested worker beyond the two file groups finishes its session without tests.
    surplus = requested_workers - 2
    finished = _pids(tmp_path, "finished-")
    assert pids < finished
    assert len(finished - pids) == 1 + surplus
    statuses = sorted(path.read_text() for path in tmp_path.glob("finished-*"))
    assert statuses == ["0"] * surplus + ["1"] * 3
    assert {path.name: path.read_text() for path in tmp_path.glob("*.events")} == expected_events
    assert record["collected"] == serial_record["collected"] == 8
    assert record["counts"] == serial_record["counts"]
    assert record["case_markers"] == serial_record["case_markers"]
    assert [(item["nodeid"], item["outcome"]) for item in record["collection_reports"]] == [
        (item["nodeid"], item["outcome"]) for item in serial_record["collection_reports"]
    ]
    assert [
        (
            case["nodeid"],
            [(phase["phase"], phase["outcome"], phase["exception"]) for phase in case["phases"]],
        )
        for case in record["cases"]
    ] == [
        (
            case["nodeid"],
            [(phase["phase"], phase["outcome"], phase["exception"]) for phase in case["phases"]],
        )
        for case in serial_record["cases"]
    ]


@requires_native_pytest
@pytest.mark.parametrize("late_worker", [False, True])
def test_collection_failure_finishes_every_started_worker(
    tmp_path: Path, late_worker: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import os
from pathlib import Path

def pytest_sessionfinish(session, exitstatus):
    Path(f'finished-{os.getpid()}').write_text(str(exitstatus))

def pytest_unconfigure(config):
    Path(f'unconfigured-{os.getpid()}').touch()
""",
    )
    condition = "os.environ.get('VFY_NATIVE_PARALLEL_INDEX') == '1'" if late_worker else "True"
    project.write(
        "test_a.py",
        f"import os\nif {condition}:\n    raise ValueError('collection failed in worker')\n"
        "def test_a():\n    raise AssertionError('body must not run')\n",
    )
    project.write("test_b.py", "def test_b():\n    raise AssertionError('body must not run')\n")

    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        "--native-result-json=results.json",
    )

    result.expect(2)
    record = json.loads((tmp_path / "results.json").read_text())
    pids = set(record["execution_topology"]["worker_pids"])
    # Every requested worker starts before collection agrees, so both observe the abort.
    assert len(pids) == 2
    _assert_controller_and_workers(_pids(tmp_path, "unconfigured-"), pids)
    _assert_controller_and_workers(_pids(tmp_path, "finished-"), pids)
    assert all(path.read_text() == "2" for path in tmp_path.glob("finished-*"))
    assert record["cases"] == []
    assert record["counts"]["errors"] == 1
    assert "collection failed in worker" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="process groups and file locks require POSIX")
@pytest.mark.parametrize("mismatched_collection", [False, True])
def test_worker_completion_releases_descendant_writers(
    tmp_path: Path, mismatched_collection: bool
) -> None:
    import fcntl  # noqa: PLC0415 - POSIX file locks are unavailable on Windows

    project = VfyProject(tmp_path)
    project.write(
        "writer.py",
        "import fcntl, os, signal, sys\nfrom pathlib import Path\n"
        "with open(sys.argv[1], 'w') as stream:\n"
        "    fcntl.flock(stream, fcntl.LOCK_EX)\n"
        "    stream.write('worker descendant owns this writer')\n    stream.flush()\n"
        "    Path(sys.argv[2]).write_text(str(os.getpid()))\n"
        "    print('ready', flush=True)\n    signal.pause()\n",
    )
    project.write(
        "conftest.py",
        "import os, subprocess, sys\nchildren = []\n"
        "def pytest_collection_modifyitems(items):\n"
        f"    if {mismatched_collection!r} and os.environ['VFY_NATIVE_PARALLEL_INDEX'] == '1':\n"
        "        items.pop()\n"
        "def pytest_collection_finish(session):\n"
        "    index = os.environ['VFY_NATIVE_PARALLEL_INDEX']\n"
        "    child = subprocess.Popen([sys.executable, 'writer.py', f'writer-{index}.lock', "
        "f'writer-{index}.pid'], stdout=subprocess.PIPE, text=True)\n"
        "    children.append(child)\n"
        "    assert child.stdout.readline().strip() == 'ready'\n",
    )
    for name in ("a", "b"):
        project.write(f"test_{name}.py", "def test_pass():\n    pass\n")

    try:
        result = project.run_vfy("test", "--engine=native", "--no-cov", "--workers=2")
        # A manifest disagreement is infrastructure failure, not a failed test.
        assert result.returncode == (3 if mismatched_collection else 0), (
            result.stdout + result.stderr
        )
        if mismatched_collection:
            assert "different test manifest" in result.stdout
        for index in range(2):
            with (tmp_path / f"writer-{index}.lock").open() as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        for path in tmp_path.glob("writer-*.pid"):
            with path.with_suffix(".lock").open() as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    with suppress(ProcessLookupError):
                        os.kill(int(path.read_text()), signal.SIGKILL)
