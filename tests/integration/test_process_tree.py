from __future__ import annotations

import os
import re
import socket
import subprocess
from contextlib import suppress
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup requires POSIX")
def test_managed_timeout_kills_grandchild_and_retains_both_streams(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "runner.py",
        """import socket, subprocess, sys, threading

if len(sys.argv) > 1:
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    print(f'grandchild-port={listener.getsockname()[1]}', flush=True)
    threading.Event().wait()
else:
    print('root-stdout-ready', flush=True)
    print('root-stderr-ready', file=sys.stderr, flush=True)
    subprocess.Popen([sys.executable, __file__, 'grandchild'])
    threading.Event().wait()
""",
    )
    command = project.start_vfy("coverage", "run", "runner.py")
    try:
        command.wait_for("grandchild-port=", stream="stdout")
        with pytest.raises(subprocess.TimeoutExpired) as failure:
            command.wait(timeout=0)
    finally:
        if command.poll() is None:
            command.kill()
            with suppress(subprocess.TimeoutExpired):
                command.wait(timeout=10)

    stdout = failure.value.stdout or b""
    stderr = failure.value.stderr or b""
    assert b"root-stdout-ready" in stdout
    assert b"grandchild-port=" in stdout
    assert b"root-stderr-ready" in stderr

    match = re.search(rb"grandchild-port=(\d+)", stdout)
    assert match is not None, stdout
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", int(match.group(1))), timeout=1)
