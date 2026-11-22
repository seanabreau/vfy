"""Workers that are still running when a peer requests the session exit finish cleanly."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

# Workers other than the first pause at random points, so the controller's interrupt
# reaches them at different stages of their own shutdown.
_CONFTEST = """import os
import random
import time


def _pause():
    if os.environ.get("VFY_NATIVE_PARALLEL_INDEX") not in (None, "0"):
        time.sleep(random.random() * 0.15)


def pytest_sessionstart(session):
    _pause()


def pytest_runtest_logstart(nodeid, location):
    _pause()


def pytest_runtest_logreport(report):
    _pause()


def pytest_sessionfinish(session):
    _pause()


def pytest_unconfigure(config):
    _pause()
"""

_TESTS = """import pytest

@pytest.mark.parametrize('value', range(16))
def test_family(value):
    pytest.exit('requested exit sentinel', returncode=7)
"""


@pytest.mark.parametrize("attempt", range(16))
def test_peer_exit_interrupt_never_becomes_a_worker_failure(tmp_path: Path, attempt: int) -> None:
    project = VfyProject(tmp_path / str(attempt))
    project.write("conftest.py", _CONFTEST)
    project.write("test_family.py", _TESTS)
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--workers=4",
        "--no-replay",
        "--no-cov",
        "-q",
        env={"VFY_NATIVE_SCHEDULING": "striped"},
    )
    output = result.stdout + result.stderr
    assert result.returncode == 7, output
    assert "INTERNALERROR" not in output, output
    assert "requested exit sentinel" in output


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX signals and flock")
def test_owned_peer_interrupt_preserves_exit_without_a_completion(tmp_path: Path) -> None:
    import fcntl  # noqa: PLC0415 - POSIX lock checks are skipped on Windows.

    project = VfyProject(tmp_path, timeout=15)
    project.write(
        "conftest.py",
        """import fcntl, os, signal, time, pytest
from pathlib import Path

index = os.environ.get('VFY_NATIVE_PARALLEL_INDEX')

def request_exit():
    deadline = time.monotonic() + 5
    while not Path('peer-ready').exists():
        assert time.monotonic() < deadline, 'peer did not reach its barrier'
        time.sleep(0.01)
    pytest.exit('owned peer stop sentinel', returncode=7)

def await_controller_interrupt():
    with open('peer.lock', 'w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        Path('peer-ready').touch()
        while True:
            signal.pause()

def pytest_runtest_logstart(nodeid, location):
    if index == '1':
        await_controller_interrupt()
""",
    )
    project.write(
        "test_family.py",
        """import os, pytest
from pathlib import Path
from conftest import request_exit

@pytest.mark.parametrize('value', range(4))
def test_value(value):
    if os.environ['VFY_NATIVE_PARALLEL_INDEX'] == '0':
        request_exit()
    Path('peer-body-ran').touch()
""",
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--workers=2",
        "--native-scheduling=cases",
        "--native-result-json=result.json",
        "--no-replay",
        "--no-cov",
        "-q",
    )
    assert (tmp_path / "peer-ready").exists(), result.stdout + result.stderr
    with (tmp_path / "peer.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    output = result.stdout + result.stderr
    assert result.returncode == 7, output
    assert "owned peer stop sentinel" in output
    assert "INTERNALERROR" not in output, output
    assert not (tmp_path / "peer-body-ran").exists()
    assert not (tmp_path / "result.json").exists()
