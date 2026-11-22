"""Default serial mutations and explicit native file isolation retain their boundaries."""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "fork"), reason="file isolation requires POSIX fork"
)

_OPTIONS = (
    "mutate",
    "--all",
    "--source",
    "app.py",
    "--operator",
    "integer_plus_one",
    "--timeout",
    "8",
    "--format",
    "json",
)
_NATIVE_FILE_OPTIONS = (*_OPTIONS, "--engine=pytest-native", "--workers=2", "--no-cache")


def test_native_omitted_isolation_runs_files_in_separate_workers(tmp_path: Path) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write("app.py", "def value():\n    return 1\n")
    os.mkfifo(tmp_path / "mutant-handshake")
    for name, mode, handshake in (
        ("a", "w", "stream.write('ready')"),
        ("b", "r", "assert stream.read() == 'ready'"),
    ):
        project.write(
            f"tests/test_{name}.py",
            "import os\nfrom pathlib import Path\nimport app\n\n"
            "def test_value():\n"
            "    value = app.value()\n"
            "    stage = 'mutant' if value == 2 else 'baseline'\n"
            f"    Path(stage + '-{name}-pid').write_text(str(os.getpid()))\n"
            "    if value == 2:\n"
            f"        with open('mutant-handshake', '{mode}') as stream:\n"
            f"            {handshake}\n"
            "    assert value == 1\n",
        )

    completed = project.run_vfy(*_NATIVE_FILE_OPTIONS, env={"VFY_MUTATION_ENGINE": "auto"})
    assert completed.stdout, completed
    report = json.loads(completed.stdout)
    assert completed.returncode == 0, (report, completed.stderr)
    assert report["summary"] == {"protected": 1}, report
    assert report["metrics"]["fast_results"] == 1, report
    for stage in ("baseline", "mutant"):
        assert (tmp_path / f"{stage}-a-pid").read_text() != (
            tmp_path / f"{stage}-b-pid"
        ).read_text(), report


def _shared_session_project(tmp_path: Path) -> VfyProject:
    project = VfyProject(tmp_path, timeout=30)
    project.write("app.py", "def value(argument=1):\n    return argument\n")
    project.write("shared_state.py", "ready = False\n")
    project.write(
        "tests/test_a_seed.py",
        "import os\nfrom pathlib import Path\nimport app\nimport shared_state\n\n"
        "def test_seed():\n"
        "    shared_state.ready = True\n"
        "    stage = 'mutant' if app.value.__defaults__ != (1,) else 'baseline'\n"
        "    Path('seed-' + stage).write_text(str(os.getpid()))\n"
        "    assert app.value(5) == 5\n",
    )
    project.write(
        "tests/test_b_consumer.py",
        "import os\nfrom pathlib import Path\nimport app\nimport shared_state\n\n"
        "def test_consumer():\n"
        "    if not shared_state.ready:\n"
        "        Path('missing-seed').touch()\n"
        "    assert shared_state.ready\n"
        "    stage = 'mutant' if app.value.__defaults__ != (1,) else 'baseline'\n"
        "    Path('consumer-' + stage).write_text(str(os.getpid()))\n"
        "    assert app.value.__defaults__ == (1,)\n",
    )
    return project


def _assert_shared_session(tmp_path: Path) -> None:
    for stage in ("baseline", "mutant"):
        assert (tmp_path / f"seed-{stage}").read_text() == (
            tmp_path / f"consumer-{stage}"
        ).read_text()
    assert not (tmp_path / "missing-seed").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="independent mutations require Linux")
def test_explicit_serial_preserves_shared_state_across_files(tmp_path: Path) -> None:
    project = _shared_session_project(tmp_path)
    completed = project.run_vfy(*_OPTIONS, "--no-cache", "--test-isolation", "serial")
    assert completed.stdout, completed
    report = json.loads(completed.stdout)
    assert completed.returncode == 0, (report, completed.stderr)
    assert report["summary"] == {"protected": 1}, report
    assert report["mutations"][0]["killer"] == "tests/test_b_consumer.py::test_consumer", report
    _assert_shared_session(tmp_path)


def test_native_clean_file_failure_cannot_be_reported_as_protection(tmp_path: Path) -> None:
    project = _shared_session_project(tmp_path)
    completed = project.run_vfy(*_NATIVE_FILE_OPTIONS)
    assert (tmp_path / "missing-seed").exists(), completed
    assert completed.returncode == 1, completed
    assert "automatic mutation baseline collection failed" in completed.stderr
    assert completed.stdout == "", completed
    assert not (tmp_path / ".vfy/mutation-baseline.json").exists()


@pytest.mark.parametrize("termination", ["exit-one", "signal"])
@pytest.mark.skipif(sys.platform != "linux", reason="independent mutations require Linux")
def test_default_abrupt_mutation_exit_is_an_error_without_protection(
    tmp_path: Path, termination: str
) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write("app.py", "def value(argument=1):\n    return argument\n")
    terminate = (
        "os._exit(1)" if termination == "exit-one" else "os.kill(os.getpid(), signal.SIGTERM)"
    )
    project.write(
        "descendant.py",
        "import fcntl, os, signal\n"
        "with open('descendant.lock', 'w') as stream:\n"
        "    fcntl.flock(stream, fcntl.LOCK_EX)\n"
        "    stream.write(str(os.getpid()))\n"
        "    stream.flush()\n"
        "    print('ready', flush=True)\n"
        "    signal.pause()\n",
    )
    project.write(
        "tests/test_app.py",
        "import os, signal, subprocess, sys\nfrom pathlib import Path\nimport app\n\n"
        "def test_value():\n"
        "    assert app.value(5) == 5\n"
        "    if app.value.__defaults__ == (2,):\n"
        "        child = subprocess.Popen([sys.executable, 'descendant.py'], "
        "stdout=subprocess.PIPE, text=True)\n"
        "        assert child.stdout.readline().strip() == 'ready'\n"
        "        Path('mutant-started').touch()\n"
        f"        {terminate}\n"
        "    assert app.value.__defaults__ == (1,)\n",
    )

    try:
        for repetition in range(2):
            output = () if repetition == 0 else ("--output=report.json",)
            completed = project.run_vfy(*_OPTIONS, *output)
            content = (
                completed.stdout if repetition == 0 else (tmp_path / "report.json").read_text()
            )
            assert content, completed
            report = json.loads(content)
            assert (tmp_path / "mutant-started").exists(), report
            assert completed.returncode == 1, (report, completed.stderr)
            assert report["summary"] == {"error": 1}, report
            row = report["mutations"][0]
            assert (row["status"], row["killer"], row.get("phase"), row["returncode"]) == (
                "error",
                None,
                None,
                1 if termination == "exit-one" else -signal.SIGTERM,
            ), report
            assert "without a valid collection completion" in row["detail"], report
            assert report["metrics"]["executed"] == 1, report
            assert report["metrics"]["cache_hits"] == 0, report
            cache = tmp_path / ".vfy/mutations.json"
            if cache.exists():
                assert row["id"] not in json.loads(cache.read_text())["entries"], report
            pid = int((tmp_path / "descendant.lock").read_text())
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        lock = tmp_path / "descendant.lock"
        if lock.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(lock.read_text()), signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "linux", reason="independent mutations require Linux")
@pytest.mark.parametrize("tamper", ["missing", "malformed", "nonce"])
def test_default_mutation_rejects_tampered_completion_after_publishing_a_result(
    tmp_path: Path, tamper: str
) -> None:
    project = VfyProject(tmp_path)
    # Calling the function during collection keeps its body out of the retained
    # fork route, so the mutation runs in a fresh worker with its own completion.
    project.write("app.py", "def value():\n    return 1\n\n\nCOLLECTED = value()\n")
    project.write("test_app.py", "import app\ndef test_value():\n    assert app.value() == 1\n")
    first = project.run_vfy(*_OPTIONS, "--output=report.json")
    assert first.returncode == 0, first
    paths = [
        tmp_path / name
        for name in (
            ".coverage",
            ".vfy/mutations.json",
            ".vfy/mutation-baseline.json",
            "report.json",
        )
    ]
    before = {path: path.read_bytes() for path in paths}
    project.write(
        "conftest.py",
        "import atexit, json, os\nfrom pathlib import Path\n"
        "def corrupt():\n"
        "    directory = Path(os.environ['VFY_NATIVE_PARALLEL_DIR'])\n"
        "    bootstrap = json.loads((directory / 'invocation-bootstrap.json').read_text())\n"
        "    if bootstrap.get('mutation') is None:\n"
        "        return\n"
        "    path = directory / 'collection-finish.0.json'\n"
        f"    tamper = {tamper!r}\n"
        "    if tamper == 'missing':\n"
        "        path.unlink()\n"
        "    elif tamper == 'malformed':\n"
        "        path.write_text('{')\n"
        "    else:\n"
        "        envelope = json.loads(path.read_text())\n"
        "        envelope['invocation']['nonce'] = 'changed-real-issued-identity'\n"
        "        path.write_text(json.dumps(envelope))\n"
        "    Path('completion-tamper-reached').touch()\n"
        "    os._exit(1)\n"
        "atexit.register(corrupt)\n",
    )

    rejected = project.run_vfy(*_OPTIONS, "--output=report.json")
    assert rejected.returncode == 1, rejected
    assert (tmp_path / "completion-tamper-reached").exists(), rejected
    diagnostic = (
        "does not belong to this invocation"
        if tamper == "nonce"
        else "without a valid collection completion"
    )
    assert diagnostic in rejected.stderr, rejected
    assert {path: path.read_bytes() for path in paths} == before


def _hard_kill_project(
    tmp_path: Path, execution: str
) -> tuple[VfyProject, list[str], dict[str, str] | None]:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    names = ["mutation"] if execution == "mutation" else ["first", "second"]
    if execution == "parallel":
        project.write(
            "conftest.py",
            "import json, os, sys\nfrom pathlib import Path\n"
            "def pytest_configure(config):\n"
            "    owned_engine = sys.flags.no_site or "
            "sys.orig_argv[1:3] == ['-m', 'vfy.independent.entry']\n"
            "    if owned_engine and 'VFY_NATIVE_PARALLEL_DIR' not in os.environ:\n"
            "        Path('engine-ready.tmp').write_text(json.dumps({\n"
            "            'pid': os.getpid(), 'parent': os.getppid(), 'group': os.getpgrp()\n"
            "        }))\n"
            "        os.replace('engine-ready.tmp', 'engine-ready')\n",
        )
    if execution == "startup":
        project.write(
            "sitecustomize.py",
            "import json, os, signal, sys\nfrom pathlib import Path\n"
            "owned_engine = sys.flags.no_site or "
            "sys.orig_argv[1:3] == ['-m', 'vfy.independent.entry']\n"
            "if owned_engine and 'VFY_NATIVE_PARALLEL_DIR' not in os.environ:\n"
            "    Path('engine-ready.tmp').write_text(json.dumps({\n"
            "        'pid': os.getpid(), 'parent': os.getppid(), 'group': os.getpgrp()\n"
            "    }))\n"
            "    os.replace('engine-ready.tmp', 'engine-ready')\n"
            "    signal.pause()\n",
        )
    if execution == "warning-startup":
        project.write(
            "startup_category.py",
            "import json, os, signal, sys\nfrom pathlib import Path\n"
            "class StartupWarning(UserWarning): pass\n"
            "owned_engine = sys.flags.no_site or "
            "sys.orig_argv[1:3] == ['-m', 'vfy.independent.entry']\n"
            "if owned_engine and 'VFY_NATIVE_PARALLEL_DIR' not in os.environ:\n"
            "    Path('engine-ready.tmp').write_text(json.dumps({\n"
            "        'pid': os.getpid(), 'parent': os.getppid(), 'group': os.getpgrp()\n"
            "    }))\n"
            "    os.replace('engine-ready.tmp', 'engine-ready')\n"
            "    signal.pause()\n",
        )
    condition = "app.value() != 1" if execution == "mutation" else "True"
    for name in names:
        project.write(
            f"test_{name}.py",
            "import json, os, signal\nfrom pathlib import Path\nimport app\n"
            "def test_value():\n"
            f"    if {condition}:\n"
            f"        Path('worker-{name}-ready.tmp').write_text(json.dumps({{\n"
            "            'pid': os.getpid(), 'parent': os.getppid(), 'group': os.getpgrp()\n"
            "        }))\n"
            f"        os.replace('worker-{name}-ready.tmp', 'worker-{name}-ready')\n"
            "        signal.pause()\n"
            "    assert app.value() == 1\n",
        )
    environment = None
    if execution == "startup":
        environment = {"PYTHONPATH": str(tmp_path)}
    elif execution == "warning-startup":
        environment = {
            "PYTHONPATH": str(tmp_path),
            "PYTHONWARNINGS": "error::startup_category.StartupWarning",
        }
    return project, names, environment


@pytest.mark.skipif(
    sys.platform != "linux"
    or not hasattr(os, "pidfd_open")
    or not hasattr(signal, "pidfd_send_signal"),
    reason="owned worker death is observed through Linux process descriptors",
)
@pytest.mark.parametrize("execution", ["mutation", "parallel", "startup", "warning-startup"])
def test_owned_independent_workers_stop_when_their_controller_is_hard_killed(
    tmp_path: Path, execution: str
) -> None:
    if sys.platform != "linux":
        pytest.skip("process-exit observation requires Linux pidfds")
    project, names, environment = _hard_kill_project(tmp_path, execution)
    command = (
        project.start_vfy(*_OPTIONS, "--no-cache", "--timeout=60")
        if execution == "mutation"
        else project.start_vfy(
            "test",
            "--engine=native",
            "--all",
            "--no-cov",
            "--workers=2",
            env=environment,
        )
    )
    descriptors: dict[int, int] = {}
    try:
        descriptors[command.process.pid] = os.pidfd_open(command.process.pid)
        engine = None
        if execution != "mutation":
            ready = project.wait_for_path("engine-ready", timeout=20)
            engine = json.loads(ready.read_text())
            if engine["pid"] not in descriptors:
                descriptors[engine["pid"]] = os.pidfd_open(engine["pid"])
        workers = []
        for name in names if execution in {"mutation", "parallel"} else []:
            ready = project.wait_for_path(f"worker-{name}-ready", timeout=20)
            worker = json.loads(ready.read_text())
            if worker["pid"] not in descriptors:
                descriptors[worker["pid"]] = os.pidfd_open(worker["pid"])
            workers.append(worker)

        expected_parent = command.process.pid
        if engine is not None:
            assert engine["parent"] == command.process.pid, engine
            assert engine["pid"] != command.process.pid, engine
            assert engine["group"] == engine["pid"], engine
            expected_parent = engine["pid"]
        for worker in workers:
            assert worker["parent"] == expected_parent, worker
            assert worker["group"] == worker["pid"], worker
        exited = select.poll()
        for descriptor in descriptors.values():
            exited.register(descriptor, select.POLLIN)
        assert exited.poll(0) == [], "owned worker exited before its controller was killed"

        os.kill(command.process.pid, signal.SIGKILL)
        result = command.wait(timeout=10)
        assert result.returncode == -signal.SIGKILL, result
        for descriptor in descriptors.values():
            stopped = select.poll()
            stopped.register(descriptor, select.POLLIN)
            assert stopped.poll(5000), "owned independent worker outlived its killed controller"
    finally:
        if command.poll() is None:
            command.kill()
            command.wait(timeout=5)
        live = []
        for pid, descriptor in descriptors.items():
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            stopped = select.poll()
            stopped.register(descriptor, select.POLLIN)
            if not stopped.poll(5000):
                live.append(pid)
            os.close(descriptor)
        assert not live, f"test cleanup did not stop owned processes {live}"


def test_native_threaded_calling_process_defaults_to_serial_isolation(tmp_path: Path) -> None:
    project = _shared_session_project(tmp_path)
    project.write(
        "driver.py",
        "import threading\nfrom vfy.cli import main\n"
        "ready = threading.Event()\nstop = threading.Event()\n"
        "def wait():\n    ready.set()\n    stop.wait()\n"
        "thread = threading.Thread(target=wait)\nthread.start()\nready.wait()\n"
        "try:\n"
        f"    result = main({list(_NATIVE_FILE_OPTIONS)!r})\n"
        "finally:\n    stop.set()\n    thread.join()\n"
        "raise SystemExit(result)\n",
    )

    completed = project.run_python("driver.py")
    assert completed.stdout, completed
    report = json.loads(completed.stdout)
    assert completed.returncode == 0, (report, completed.stderr)
    assert report["summary"] == {"protected": 1}, report
    _assert_shared_session(tmp_path)
