"""Fresh workers preserve native lifecycle failures instead of losing their status."""

from __future__ import annotations

import json
import os
import signal
from contextlib import suppress
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_static_surplus_retains_public_collection_during_sessionfinish(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import os
from pathlib import Path
def pytest_sessionfinish(session, exitstatus):
    controller = (getattr(session.config.option, 'workers', 1) > 1
                  and 'VFY_NATIVE_PARALLEL_DIR' not in os.environ)
    assert session.testscollected == 2
    assert len(session.items) == (0 if controller else 2)
    Path(f'finished-{os.getpid()}').write_text(str(int(exitstatus)))
""",
    )
    project.write("test_a.py", "def test_fail():\n    assert False\n")
    project.write("test_b.py", "def test_pass():\n    pass\n")
    stock = project.run_python("-m", "pytest", "-q")
    stock.expect(1)
    assert [path.read_text() for path in tmp_path.glob("finished-*")] == ["1"]
    for path in tmp_path.glob("finished-*"):
        path.unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=3",
        "--native-scheduling=file",
        "--native-result-json=result.json",
    )
    candidate.expect(1)
    record = json.loads((tmp_path / "result.json").read_text())
    assert record["collected"] == 2
    assert record["counts"]["failed"] == record["counts"]["passed"] == 1
    assert record["execution_topology"]["active_workers"] == 2
    assert [case["nodeid"] for case in record["cases"]] == [
        "test_a.py::test_fail",
        "test_b.py::test_pass",
    ]
    assert sorted(path.read_text() for path in tmp_path.glob("finished-*")) == ["0", "0", "1", "1"]


@requires_native_pytest
@pytest.mark.parametrize(
    ("failure", "status"),
    [("conftest", 4), ("configure", 3), ("exit", 42), ("keyboard", 2)],
)
def test_worker_before_collection_preserves_status_and_cleanup(
    tmp_path: Path, failure: str, status: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"""import json, os, pytest
from pathlib import Path
failure = {failure!r}
worker = 'VFY_NATIVE_PARALLEL_INDEX' in os.environ

def note(event):
    if worker:
        path = Path('events-' + os.environ['TEST_ENGINE'] + '-' + str(os.getpid()) + '.jsonl')
        with path.open('a') as output:
            output.write(json.dumps(event) + '\\n')

note('conftest')
if worker and failure == 'conftest':
    raise RuntimeError('worker conftest witness')

def pytest_configure(config):
    if worker:
        note('configure')
        config.add_cleanup(lambda: note('cleanup'))
        if failure == 'configure':
            raise RuntimeError('worker configure witness')
        if failure == 'exit':
            pytest.exit('worker exit witness', returncode=42)
        if failure == 'keyboard':
            raise KeyboardInterrupt('worker interrupt witness')

def pytest_unconfigure():
    note('unconfigure')
""",
    )
    for name in ["test_first.py", "test_second.py"]:
        project.write(name, "def test_body():\n    raise AssertionError('body must not run')\n")
    stock = project.run_python(
        "-m", "pytest", "-q", env={"TEST_ENGINE": "oracle", "VFY_NATIVE_PARALLEL_INDEX": "0"}
    )
    assert stock.returncode == status, stock.stdout + stock.stderr
    oracle_paths = list(tmp_path.glob("events-oracle-*.jsonl"))
    assert len(oracle_paths) == 1
    expected = [json.loads(line) for line in oracle_paths[0].read_text().splitlines()]
    assert expected == (
        ["conftest"]
        if failure == "conftest"
        else ["conftest", "configure", "unconfigure", "cleanup"]
    )
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        "--native-result-json=result.json",
        env={"TEST_ENGINE": "candidate"},
    )
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    # Both requested workers start together; each completes the same lifecycle as stock.
    worker_paths = list(tmp_path.glob("events-candidate-*.jsonl"))
    assert len(worker_paths) == 2
    for path in worker_paths:
        assert [json.loads(line) for line in path.read_text().splitlines()] == expected
    assert not (tmp_path / "result.json").exists()


@requires_native_pytest
def test_unexpected_worker_death_does_not_publish_a_successful_result(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import os
from pathlib import Path

def pytest_configure():
    if 'VFY_NATIVE_PARALLEL_INDEX' in os.environ:
        Path('unexpected-death').write_text(str(os.getpid()))
        os._exit(17)
""",
    )
    project.write("test_body.py", "def test_body():\n    pass\n")
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        "--native-result-json=result.json",
    )
    assert int((tmp_path / "unexpected-death").read_text()) > 0
    assert result.returncode != 0, result.stdout + result.stderr
    assert "without a valid collection completion" in result.stdout
    assert not (tmp_path / "result.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="requires process groups and flock")
@pytest.mark.parametrize(
    ("failure", "status"), [("conftest", 4), ("configure", 3), ("exit", 42), ("keyboard", 2)]
)
def test_later_worker_failure_drains_an_already_collected_peer(
    tmp_path: Path, failure: str, status: int
) -> None:
    import fcntl  # noqa: PLC0415 - POSIX only

    project = VfyProject(tmp_path)
    project.write(
        "writer.py",
        """import fcntl, os, signal
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
        "conftest.py",
        f"""import json, os, pytest, subprocess, sys
from pathlib import Path
failure = {failure!r}
index = os.environ.get('VFY_NATIVE_PARALLEL_INDEX')
engine = os.environ['TEST_ENGINE']

def note(event):
    if index is not None:
        path = Path('events-' + engine + '-' + str(os.getpid()) + '.jsonl')
        with path.open('a') as stream:
            stream.write(json.dumps(event) + '\\n')

note('conftest')
if index == '1' and failure == 'conftest':
    raise RuntimeError('later worker conftest')

def pytest_configure(config):
    note('configure')
    config.add_cleanup(lambda: note('cleanup'))
    if index == '0':
        child = subprocess.Popen([sys.executable, 'writer.py'], stdout=subprocess.PIPE,
            text=True, start_new_session=True)
        assert child.stdout.readline().strip() == 'ready'
        def close_writer():
            child.terminate()
            child.wait(timeout=5)
            Path('writer-finalized').touch()
        config.add_cleanup(close_writer)
    if index == '1':
        if failure == 'configure':
            raise RuntimeError('later worker configure')
        if failure == 'exit':
            pytest.exit('later worker exit', returncode=42)
        if failure == 'keyboard':
            raise KeyboardInterrupt('later worker interrupt')

def pytest_unconfigure():
    note('unconfigure')
""",
    )
    for file in ["test_a.py", "test_b.py"]:
        project.write(file, "def test_body():\n    raise AssertionError('body must not run')\n")
    stock = project.run_python(
        "-m", "pytest", "-q", env={"TEST_ENGINE": "oracle", "VFY_NATIVE_PARALLEL_INDEX": "1"}
    )
    assert stock.returncode == status, stock.stdout + stock.stderr
    oracle = list(tmp_path.glob("events-oracle-*.jsonl"))
    assert len(oracle) == 1
    expected = [json.loads(line) for line in oracle[0].read_text().splitlines()]
    try:
        candidate = project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            "--workers=2",
            "--native-result-json=result.json",
            env={"TEST_ENGINE": "candidate"},
        )
        assert candidate.returncode == status, candidate.stdout + candidate.stderr
        workers = list(tmp_path.glob("events-candidate-*.jsonl"))
        assert len(workers) == 2
        events = [[json.loads(line) for line in path.read_text().splitlines()] for path in workers]
        assert expected in events
        assert ["conftest", "configure", "unconfigure", "cleanup"] in events
        assert (tmp_path / "writer-finalized").exists()
        assert not (tmp_path / "result.json").exists()
        with (tmp_path / "writer.lock").open() as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        path = tmp_path / "writer.pid"
        if path.exists():
            with suppress(ProcessLookupError):
                os.kill(int(path.read_text()), signal.SIGKILL)
