"""Cancellation and bounded stream replay through native worker processes."""

from __future__ import annotations

import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="native worker process groups require Linux or macOS",
)


def _release_fixture_processes(project: VfyProject) -> None:
    import fcntl  # noqa: PLC0415 - POSIX-only fixture cleanup

    for pid_file in project.root.glob("*.pid"):
        lock_file = pid_file.with_suffix(".lock")
        if not lock_file.exists():
            continue
        with lock_file.open() as locked:
            try:
                fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                with suppress(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)


@pytest.mark.parametrize(
    ("phase", "coverage", "custom_handler"),
    [
        pytest.param("collection", False, False, id="collection-no-cov-default-handler"),
        pytest.param("collection", False, True, id="collection-no-cov-custom-handler"),
        pytest.param("collection", True, False, id="collection-coverage-handler"),
        pytest.param("execution", False, False, id="execution-no-cov-default-handler"),
        pytest.param("execution", False, True, id="execution-no-cov-custom-handler"),
        pytest.param("execution", True, False, id="execution-coverage-handler"),
        pytest.param("admission", False, False, id="admission-no-cov-default-handler"),
    ],
)
def test_sigterm_cancels_worker_groups_and_runs_no_previous_handler_before_cleanup(
    tmp_path: Path, coverage: bool, custom_handler: bool, phase: str
) -> None:
    import fcntl  # noqa: PLC0415 - POSIX-only process liveness proof

    project = VfyProject(tmp_path, timeout=30)
    project.write("app.py", "VALUE = 1\n")
    if coverage:
        project.write(".coveragerc", "[run]\nsigterm = true\n")
    project.write(
        "descendant.py",
        "import fcntl, os, signal, sys\n"
        "from pathlib import Path\n"
        "with open(sys.argv[1] + '.lock', 'w') as locked:\n"
        "    fcntl.flock(locked, fcntl.LOCK_EX)\n"
        "    Path(sys.argv[1] + '.pid').write_text(str(os.getpid()))\n"
        "    print('ready', flush=True)\n"
        "    signal.pause()\n",
    )
    project.write(
        "conftest.py",
        "import fcntl, os, signal, subprocess, sys\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "from app import VALUE\n"
        "children = []\n"
        "locks = []\n"
        "def previous_handler(signum, frame):\n"
        "    raise AssertionError('previous SIGTERM handler ran before worker cleanup')\n"
        "def pytest_configure(config):\n"
        "    if 'VFY_NATIVE_PARALLEL_INDEX' in os.environ:\n"
        "        Path('started' + os.environ['VFY_NATIVE_PARALLEL_INDEX']).touch()\n"
        "    if 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ:\n"
        f"        if {custom_handler!r}:\n"
        "            signal.signal(signal.SIGTERM, previous_handler)\n"
        "        Path('coordinator.pid').write_text(str(os.getpid()))\n"
        "        Path('previous-handler').write_text(repr(signal.getsignal(signal.SIGTERM)))\n"
        "def block_worker():\n"
        "    index = os.environ['VFY_NATIVE_PARALLEL_INDEX']\n"
        "    locked = open('worker' + index + '.lock', 'w')\n"
        "    locks.append(locked)\n"
        "    fcntl.flock(locked, fcntl.LOCK_EX)\n"
        "    Path('worker' + index + '.pid').write_text(str(os.getpid()))\n"
        "    child = subprocess.Popen([sys.executable, 'descendant.py', 'descendant' + index],\n"
        "                             stdout=subprocess.PIPE, text=True)\n"
        "    children.append(child)\n"
        "    assert child.stdout.readline().strip() == 'ready'\n"
        "    Path('ready' + index).touch()\n"
        # Worker zero must publish the initial inventory before a peer starts.
        # Its descendant remains alive while the peer blocks during collection.
        f"    if {phase!r} != 'collection' or index != '0':\n"
        "        signal.pause()\n"
        "def pytest_collection_modifyitems():\n"
        f"    if {phase!r} in ('collection', 'admission') "
        "and 'VFY_NATIVE_PARALLEL_INDEX' in os.environ:\n"
        "        block_worker()\n",
    )
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            "from conftest import block_worker\ndef test_wait():\n    block_worker()\n",
        )

    coverage_option = "--cov=app" if coverage else "--no-cov"
    command = project.start_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        coverage_option,
        "-q",
        env={"VFY_WORKER_START": "exec"},
    )
    try:
        admitted_workers = 1 if phase == "admission" else 2
        for index in range(admitted_workers):
            project.wait_for_path(f"ready{index}")
        if phase == "admission":
            assert not (tmp_path / "started1").exists()
        coordinator = int(project.wait_for_path("coordinator.pid").read_text())
        os.killpg(os.getpgid(coordinator), signal.SIGTERM)
        completed = command.wait(timeout=10)
        output = completed.stdout + completed.stderr
        assert completed.returncode == 143, output
        assert "native parallel session terminated by SIGTERM" in output
        if phase == "admission":
            assert not (tmp_path / "started1").exists()
            owners = ("worker0", "descendant0")
        else:
            owners = ("worker0", "worker1", "descendant0", "descendant1")
        for owner in owners:
            with (tmp_path / f"{owner}.lock").open() as locked:
                fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        command.kill()
        command.wait(timeout=10)
        _release_fixture_processes(project)


def test_worker_output_replays_in_bounded_chunks_after_descendants_stop(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "descendant.py",
        "import fcntl, os, signal, sys\n"
        "from pathlib import Path\n"
        "with open(sys.argv[1] + '.lock', 'w') as locked:\n"
        "    fcntl.flock(locked, fcntl.LOCK_EX)\n"
        "    Path(sys.argv[1] + '.pid').write_text(str(os.getpid()))\n"
        "    print('ready', flush=True)\n"
        "    signal.pause()\n",
    )
    project.write(
        "conftest.py",
        "import fcntl, os, signal, sys, time\n"
        "from pathlib import Path\n"
        "class BoundedOutput:\n"
        "    def __init__(self, stream):\n"
        "        self.stream = stream\n"
        "        self.maximum = 0\n"
        "    def __getattr__(self, name):\n"
        "        return getattr(self.stream, name)\n"
        "    def write(self, text):\n"
        "        self.maximum = max(self.maximum, len(text))\n"
        "        if 'PAYLOAD-' in text:\n"
        "            for path in Path('.').glob('descendant*.lock'):\n"
        "                with path.open() as locked:\n"
        "                    deadline = time.monotonic() + 1\n"
        "                    while True:\n"
        "                        try:\n"
        "                            fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "                            break\n"
        "                        except BlockingIOError:\n"
        "                            if time.monotonic() >= deadline:\n"
        "                                raise\n"
        "                            time.sleep(0.001)\n"
        "        return self.stream.write(text)\n"
        "def previous_handler(signum, frame):\n"
        "    raise AssertionError('unexpected SIGTERM')\n"
        "def pytest_configure(config):\n"
        "    if 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ:\n"
        "        signal.signal(signal.SIGTERM, previous_handler)\n"
        "        sys.stdout = BoundedOutput(sys.stdout)\n"
        "def pytest_sessionfinish(session):\n"
        "    if 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ:\n"
        "        Path('maximum-write').write_text(str(sys.stdout.maximum))\n"
        "        restored = signal.getsignal(signal.SIGTERM) is previous_handler\n"
        "        Path('handler-restored').write_text(str(restored))\n",
    )
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            "import os, subprocess, sys\n"
            "children = []\n"
            "def test_output():\n"
            "    child = subprocess.Popen([sys.executable, 'descendant.py', "
            f"'descendant-{name}'],\n"
            "                             stdout=subprocess.PIPE, text=True)\n"
            "    children.append(child)\n"
            "    assert child.stdout.readline().strip() == 'ready'\n"
            f"    os.write(1, b'PAYLOAD-{name}:' + b'x' * (65535 - 10) + b'\\xe2\\x82\\xac' +\n"
            "                b'y' * 131072 + b'\\xffEND\\n')\n",
        )
    try:
        completed = project.run_vfy(
            "test", "--engine=pytest-native", "--workers=2", "--no-replay", "--no-cov", "-s", "-q"
        )
        assert completed.returncode == 0, completed.stdout[-1000:] + completed.stderr
        for name in ("a", "b"):
            expected = f"PAYLOAD-{name}:" + "x" * (65535 - 10) + "€" + "y" * 131072 + "�END\n"
            assert completed.stdout.count(expected) == 1
        assert int((tmp_path / "maximum-write").read_text()) <= 65536
        assert (tmp_path / "handler-restored").read_text() == "True"
    finally:
        _release_fixture_processes(project)


def test_worker_failure_keeps_bounded_diagnostic_tails(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import os\n"
        "def pytest_configure(config):\n"
        "    if os.environ.get('VFY_NATIVE_PARALLEL_INDEX') == '0':\n"
        "        os.write(1, b'OMITTED-HEAD\\n' + b'x' * 262144 + b'\\nSTDOUT-TAIL\\n')\n"
        "        os.write(2, b'OMITTED-HEAD\\n' + b'y' * 262144 + b'\\nSTDERR-TAIL\\n')\n"
        "        os._exit(4)\n",
    )
    for name in ("a", "b"):
        project.write(f"test_{name}.py", "def test_ok():\n    pass\n")
    completed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "--no-cov",
        "-q",
        env={"VFY_WORKER_START": "exec"},
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 4, output[-1000:]
    assert "exited before all workers compiled" in output
    assert "STDOUT-TAIL" in output
    assert "STDERR-TAIL" in output
    assert "earlier output omitted" in output
    assert "OMITTED-HEAD" not in output
    assert len(output) < 150000


@pytest.mark.parametrize("text_adapter", [False, True], ids=["standard-streams", "text-only"])
@pytest.mark.parametrize(
    "capture_fixture", [None, "capsys", "capfd"], ids=["no-capture", "capsys", "capfd"]
)
@pytest.mark.parametrize(
    ("stdout_encoding", "stderr_encoding", "stdout_text", "stderr_text"),
    [
        ("latin-1", "cp1252", "café", "café €"),
        ("cp1252", "latin-1", "café €", "café"),
        ("ascii:backslashreplace", "ascii", "café", "café"),
    ],
)
def test_worker_stream_replay_preserves_python_encodings_and_text_adapters(
    tmp_path: Path,
    text_adapter: bool,
    capture_fixture: str | None,
    stdout_encoding: str,
    stderr_encoding: str,
    stdout_text: str,
    stderr_text: str,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import os, sys\n"
        "from pathlib import Path\n"
        "class TextOnly:\n"
        "    def __init__(self, stream):\n"
        "        self.stream = stream\n"
        "        self.maximum = 0\n"
        "    def write(self, text):\n"
        "        self.maximum = max(self.maximum, len(text))\n"
        "        return self.stream.write(text)\n"
        "    def flush(self):\n"
        "        self.stream.flush()\n"
        "    def isatty(self):\n"
        "        return False\n"
        "def pytest_configure(config):\n"
        f"    sys.__stderr__.reconfigure(encoding={stderr_encoding!r}, errors='backslashreplace')\n"
        "    if 'VFY_TEXT_ONLY' in os.environ and 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ:\n"
        "        sys.stdout, sys.stderr = TextOnly(sys.stdout), TextOnly(sys.stderr)\n"
        "def pytest_sessionfinish(session):\n"
        "    if 'VFY_TEXT_ONLY' in os.environ and 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ:\n"
        "        maximum = max(sys.stdout.maximum, sys.stderr.maximum)\n"
        "        Path('maximum-write').write_text(str(maximum))\n",
    )
    for name in ("a", "b"):
        signature = capture_fixture or ""
        body = f"    with {capture_fixture}.disabled():\n" if capture_fixture is not None else ""
        indent = "        " if capture_fixture is not None else "    "
        project.write(
            f"test_{name}.py",
            "import sys\n"
            f"def test_output({signature}):\n"
            f"{body}"
            f"{indent}print({stdout_text!r})\n"
            f"{indent}print({stderr_text!r}, file=sys.stderr)\n",
        )
    output_codec, separator, errors = stdout_encoding.partition(":")
    stdout_errors = errors if separator else "strict"
    expected_stdout = stdout_text.encode(output_codec, errors=stdout_errors).decode(output_codec)
    expected_stderr = stderr_text.encode(stderr_encoding, errors="backslashreplace").decode(
        stderr_encoding
    )
    # Read pipe bytes explicitly: the harness's UTF-8 decoding is independent
    # of the subprocess's Python standard-stream configuration.
    project.write(
        "launcher.py",
        "import json, subprocess, sys\n"
        "result = subprocess.run([sys.executable, '-m', 'vfy', *sys.argv[1:]],\n"
        "                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
        "print(json.dumps({'returncode': result.returncode,\n"
        f"                  'stdout': result.stdout.decode({output_codec!r}),\n"
        f"                  'stderr': result.stderr.decode({stderr_encoding!r})}}))\n",
    )
    for engine, workers in (("pytest", 1), ("pytest-native", 1), ("pytest-native", 2)):
        completed = project.run_python(
            "launcher.py",
            "test",
            f"--engine={engine}",
            f"--workers={workers}",
            "--no-replay",
            "--no-cov",
            *(["-s"] if capture_fixture is None else []),
            "-q",
            # The text-only adapter wraps the fresh-worker launcher, so pin that route.
            env={
                "PYTHONIOENCODING": stdout_encoding,
                "VFY_WORKER_START": "exec",
                "VFY_TEXT_ONLY": "1"
                if text_adapter and (engine, workers) == ("pytest-native", 2)
                else None,
            },
        )
        completed.expect(0)
        result = json.loads(completed.stdout)
        assert result["returncode"] == 0, result
        assert result["stdout"].count(expected_stdout + "\n") == 2, result
        assert result["stderr"].count(expected_stderr + "\n") == 2, result
        if text_adapter and engine == "pytest-native" and workers == 2:
            assert int((tmp_path / "maximum-write").read_text()) <= 65536


def test_worker_bootstrap_diagnostics_preserve_non_utf8_text(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import os\n"
        "import pytest\n"
        "def pytest_collection_modifyitems():\n"
        "    if os.environ.get('VFY_NATIVE_PARALLEL_INDEX') == '0':\n"
        "        pytest.fail('OMITTED-HEAD ' + 'x' * 262144 + ' café-TAIL')\n",
    )
    for name in ("a", "b"):
        project.write(f"test_{name}.py", "def test_ok():\n    pass\n")
    project.write(
        "launcher.py",
        "import json, subprocess, sys\n"
        "result = subprocess.run([sys.executable, '-m', 'vfy', 'test',\n"
        "                         '--workers=2', '--no-replay', '--no-cov', '-q'],\n"
        "                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
        "print(json.dumps({'returncode': result.returncode,\n"
        "                  'stdout': result.stdout.decode('latin-1'),\n"
        "                  'stderr': result.stderr.decode('latin-1')}))\n",
    )
    completed = project.run_python(
        "launcher.py", env={"PYTHONIOENCODING": "latin-1", "VFY_WORKER_START": "exec"}
    )
    completed.expect(0)
    result = json.loads(completed.stdout)
    output = result["stdout"] + result["stderr"]
    assert result["returncode"] == 3, output[-1000:]
    assert "café-TAIL" in output
    assert "OMITTED-HEAD" not in output
    assert "earlier output omitted" in output
    assert len(output) < 150000
