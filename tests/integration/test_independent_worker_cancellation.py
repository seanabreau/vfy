"""Independent workers preserve live hooks and release writers on cancellation."""

from __future__ import annotations

import os
import signal
from contextlib import suppress
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path


@pytest.mark.skipif(os.name != "posix", reason="process groups and file locks require POSIX")
@pytest.mark.parametrize("stage", ["collection", "call"])
@pytest.mark.parametrize("termination", [signal.SIGINT, signal.SIGTERM])
def test_cancellation_releases_worker_descendant_writers(
    tmp_path: Path, stage: str, termination: signal.Signals
) -> None:
    import fcntl  # noqa: PLC0415 - POSIX file locks are unavailable on Windows

    project = VfyProject(tmp_path)
    project.write(
        "writer.py",
        """import fcntl, os, signal, sys
from pathlib import Path
with open(sys.argv[1], 'w') as stream:
    fcntl.flock(stream, fcntl.LOCK_EX)
    Path(sys.argv[2]).write_text(str(os.getpid()))
    print('ready', flush=True)
    signal.pause()
""",
    )
    project.write(
        "conftest.py",
        f"""import os, signal, subprocess, sys
from pathlib import Path
children = []
def pytest_collection_finish(session):
    index = os.environ['VFY_NATIVE_PARALLEL_INDEX']
    Path(f'worker-{{index}}.pid').write_text(str(os.getpid()))
    Path('controller.pid').write_text(str(os.getppid()))
    child = subprocess.Popen(
        [sys.executable, 'writer.py', f'writer-{{index}}.lock', f'writer-{{index}}.pid'],
        stdout=subprocess.PIPE, text=True)
    children.append(child)
    assert child.stdout.readline().strip() == 'ready'
    Path(f'collected-{{index}}').touch()
    if {stage!r} == 'collection': signal.pause()
""",
    )
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            "import os, signal\nfrom pathlib import Path\n"
            "def test_wait():\n"
            "    Path('running-' + os.environ['VFY_NATIVE_PARALLEL_INDEX']).touch()\n"
            "    signal.pause()\n",
        )
    command = project.start_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        env={"PYTHONPATH": isolated_vfy_path()},
    )
    try:
        indexes = range(1) if stage == "collection" else range(2)
        for index in indexes:
            project.wait_for_path(f"{'collected' if stage == 'collection' else 'running'}-{index}")
        command.send_signal(termination)
        result = command.wait(timeout=15)
        assert result.returncode in ({2, 130, -2} if termination == signal.SIGINT else {143, -15})
        for path in project.root.glob("writer-*.lock"):
            with path.open() as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if command.poll() is None:
            command.kill()
            command.wait(timeout=5)
        for path in project.root.glob("worker-*.pid"):
            with suppress(ProcessLookupError):
                os.killpg(int(path.read_text()), signal.SIGKILL)
        controller = project.root / "controller.pid"
        if controller.is_file():
            with suppress(ProcessLookupError):
                os.kill(int(controller.read_text()), signal.SIGKILL)
        for path in project.root.glob("writer-*.pid"):
            with path.with_suffix(".lock").open() as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    with suppress(ProcessLookupError):
                        os.kill(int(path.read_text()), signal.SIGKILL)
