"""Test native workers through vfy subprocesses."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject


def _native_parallel_available() -> bool:
    if os.name == "nt" or not sys.platform.startswith(("linux", "darwin")):
        return False
    try:
        return importlib.util.find_spec(
            "vfy._pytest.native_pytest"
        ) is not None and importlib.metadata.version("pytest").split(".", maxsplit=1)[0] in {
            "8",
            "9",
        }
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


pytestmark = pytest.mark.skipif(
    not _native_parallel_available(),
    reason="native parallel tests require Linux or macOS and a supported pytest version",
)


def test_native_workers_restart_from_the_invocation_directory(tmp_path: Path) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("upstream", "serial", "parallel")}
    for project in projects.values():
        project.write("runtime/.keep", "")
        project.write(
            "conftest.py",
            "import os\n"
            "def pytest_configure(config):\n"
            "    os.chdir(config.invocation_params.dir / 'runtime')\n",
        )
        for name in ("one", "two"):
            project.write(
                f"tests/test_{name}.py",
                "from pathlib import Path\n"
                "def test_working_directory():\n"
                "    assert Path.cwd().name == 'runtime'\n",
            )

    oracle = projects["upstream"].run_pytest("tests", "-q", load_xdist=False)
    serial = projects["serial"].run_vfy(
        "test", "--engine=pytest-native", "--workers=1", "--no-replay", "--no-cov", "tests", "-q"
    )
    parallel = projects["parallel"].run_vfy(
        "test", "--engine=pytest-native", "--workers=2", "--no-replay", "--no-cov", "tests", "-q"
    )
    for result in (oracle, serial, parallel):
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 passed" in result.stdout


def test_successful_native_workers_release_descendants(tmp_path: Path) -> None:
    import fcntl  # noqa: PLC0415 - keep POSIX-only import inside its skipped test

    project = VfyProject(tmp_path)
    project.write(".vfy/.keep", "")
    project.write(
        "descendant.py",
        "import fcntl, os, signal, sys\n"
        "from pathlib import Path\n"
        "with open(sys.argv[1], 'w') as locked:\n"
        "    fcntl.flock(locked, fcntl.LOCK_EX)\n"
        "    Path(sys.argv[2]).write_text(str(os.getpid()))\n"
        "    print('ready', flush=True)\n"
        "    signal.pause()\n",
    )
    for name in ("a", "b"):
        project.write(
            f"tests/test_{name}.py",
            "import subprocess, sys\n"
            "children = []\n"
            "def test_spawn():\n"
            "    child = subprocess.Popen([sys.executable, 'descendant.py', "
            f"'.vfy/{name}.lock', "
            f"'.vfy/{name}.pid'], stdout=subprocess.PIPE, text=True)\n"
            "    children.append(child)\n"
            "    assert child.stdout.readline().strip() == 'ready'\n",
        )
    try:
        completed = project.run_vfy(
            "test", "--engine=pytest-native", "--workers=2", "--no-replay", "--no-cov"
        )
        completed.expect(0)
        for name in ("a", "b"):
            with (tmp_path / f".vfy/{name}.lock").open() as locked:
                fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        # Contain fixture-owned descendants even when the cleanup assertion fails.
        for name in ("a", "b"):
            pid_file = tmp_path / f".vfy/{name}.pid"
            if pid_file.exists():
                with (tmp_path / f".vfy/{name}.lock").open() as locked:
                    try:
                        fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        with suppress(ProcessLookupError):
                            os.kill(int(pid_file.read_text()), signal.SIGKILL)


def _successful_run(project: VfyProject, *arguments: str) -> None:
    completed = project.run_vfy("test", "--engine=pytest-native", "--no-replay", *arguments, "-q")
    completed.expect(0)


def test_native_worker_streams_replace_invalid_utf8(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import os\n\n"
        "def pytest_configure(config):\n"
        "    if os.environ.get('VFY_NATIVE_PARALLEL_INDEX') == '0':\n"
        "        os.write(1, b'worker-stdout-\\xff\\n')\n"
        "        os.write(2, b'worker-stderr-\\xfe\\n')\n",
    )
    for name in ("alpha", "beta"):
        project.write(f"tests/test_{name}.py", f"def test_{name}():\n    assert True\n")

    completed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "--no-cov",
        "-q",
        env={"VFY_WORKER_START": "exec"},
    )

    completed.expect(0)
    assert "worker-stdout-\ufffd" in completed.stdout
    assert "worker-stderr-\ufffd" in completed.stderr


def _seed_coverage(project: VfyProject) -> bytes:
    project.write("app.py", "VALUE = 1\n")
    project.write(
        "tests/test_seed.py",
        "from app import VALUE\ndef test_seed():\n    assert VALUE == 1\n",
    )
    _successful_run(project, "--workers=1", "--cov=app")
    return (project.root / ".coverage").read_bytes()


@pytest.mark.parametrize(
    "workers", [("--workers=2",), ("-n", "2"), ("-n2",), ("--no-cov", "-n2"), ("tests", "-n", "2")]
)
def test_native_workers_keep_files_together_and_finalize_each_worker_session(
    tmp_path: Path,
    workers: tuple[str, ...],
) -> None:
    project = VfyProject(tmp_path)
    project.write(".vfy/.keep", "")
    project.write(
        "conftest.py",
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        "import pytest\n\n"
        "@pytest.fixture(scope='session', autouse=True)\n"
        "def worker_lifecycle(tmp_path_factory, request):\n"
        "    assert not request.config.pluginmanager.hasplugin('xdist')\n"
        "    assert not request.config.pluginmanager.hasplugin('xdist.plugin')\n"
        "    assert not hasattr(request.config, 'workerinput')\n"
        "    root = Path('.vfy')\n"
        "    root.mkdir(exist_ok=True)\n"
        "    pid = os.getpid()\n"
        "    worker_tmp = tmp_path_factory.mktemp(f'worker-{pid}')\n"
        "    (worker_tmp / 'sentinel').write_text(str(pid), encoding='utf-8')\n"
        "    (root / f'temp-path-{pid}').write_text(str(worker_tmp), encoding='utf-8')\n"
        "    (root / f'session-start-{pid}').touch()\n"
        "    deadline = time.monotonic() + 15\n"
        "    while len(list(root.glob('session-start-*'))) < 2:\n"
        "        if time.monotonic() >= deadline:\n"
        "            raise AssertionError('both native file workers did not start')\n"
        "        time.sleep(0.01)\n"
        "    for path_record in root.glob('temp-path-*'):\n"
        "        worker_pid = path_record.name.removeprefix('temp-path-')\n"
        "        sentinel = Path(path_record.read_text(encoding='utf-8')) / 'sentinel'\n"
        "        assert sentinel.read_text(encoding='utf-8') == worker_pid\n"
        "    yield\n"
        "    (root / f'session-finish-{pid}').touch()\n",
    )
    for name in ("alpha", "beta"):
        project.write(
            f"tests/test_{name}.py",
            "import os\n"
            "from pathlib import Path\n\n"
            f"def test_{name}_first():\n"
            f"    with Path('.vfy/{name}-pids').open('a', encoding='utf-8') as stream:\n"
            "        stream.write(f'{os.getpid()}\\n')\n\n"
            f"def test_{name}_second():\n"
            f"    with Path('.vfy/{name}-pids').open('a', encoding='utf-8') as stream:\n"
            "        stream.write(f'{os.getpid()}\\n')\n",
        )

    completed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--no-replay",
        *workers,
        "--basetemp=.vfy/temp-workers",
        "-q",
    )

    completed.expect(0)
    assert "4 passed" in completed.stdout
    alpha_pids = (tmp_path / ".vfy/alpha-pids").read_text().splitlines()
    beta_pids = (tmp_path / ".vfy/beta-pids").read_text().splitlines()
    assert len(alpha_pids) == len(beta_pids) == 2
    assert len(set(alpha_pids)) == len(set(beta_pids)) == 1
    assert alpha_pids[0] != beta_pids[0]
    starts = {
        path.name.removeprefix("session-start-") for path in tmp_path.glob(".vfy/session-start-*")
    }
    finishes = {
        path.name.removeprefix("session-finish-") for path in tmp_path.glob(".vfy/session-finish-*")
    }
    assert len(starts) == len(finishes) == 2
    assert starts == finishes == {alpha_pids[0], beta_pids[0]}
    temp_path_records = list((tmp_path / ".vfy").glob("temp-path-*"))
    assert len(temp_path_records) == 2
    temp_roots = [Path(record.read_text(encoding="utf-8")) for record in temp_path_records]
    basetemp = tmp_path / ".vfy/temp-workers"
    assert len(set(temp_roots)) == 2
    for record, worker_tmp in zip(temp_path_records, temp_roots, strict=True):
        worker_pid = record.name.removeprefix("temp-path-")
        assert worker_tmp.is_relative_to(basetemp)
        assert (worker_tmp / "sentinel").read_text(encoding="utf-8") == worker_pid


def test_native_auto_workers_accept_one_collected_case(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("tests/test_only.py", "def test_only_case():\n    assert True\n")

    completed = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=auto", "--no-replay", "-q"
    )

    completed.expect(0)
    assert "1 passed" in completed.stdout


def _write_parity_project(project: VfyProject) -> None:
    project.write(
        "app.py",
        "def alpha():\n    return 'alpha'\n\n"
        "def beta():\n    return 'beta'\n\n"
        "def cleanup():\n    return 'clean'\n",
    )
    project.write(
        "conftest.py",
        "import pytest\n"
        "from app import cleanup\n\n"
        "@pytest.fixture\n"
        "def cleaned_up():\n"
        "    yield\n"
        "    cleanup()\n",
    )
    for name in ("alpha", "beta"):
        project.write(
            f"tests/test_{name}.py",
            f"from app import {name}\n\n"
            f"def test_{name}(cleaned_up):\n"
            f"    assert {name}() == '{name}'\n",
        )


def _execution_outcomes(path: Path) -> list[tuple[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        (
            case["nodeid"],
            next(phase["outcome"] for phase in case["phases"] if phase["phase"] == "call"),
        )
        for case in payload["cases"]
    ]


def _report_semantics(path: Path) -> tuple[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    files = {Path(filename).name: report for filename, report in payload["files"].items()}
    return json.dumps(payload["totals"], sort_keys=True), json.dumps(files, sort_keys=True)


def test_native_auto_workers_match_serial_outcomes_contexts_and_reports(tmp_path: Path) -> None:
    projects: dict[str, VfyProject] = {}
    records: dict[str, list[tuple[str, str]]] = {}
    for route, worker_option in (("serial", "--workers=1"), ("parallel", "--workers=auto")):
        project = VfyProject(tmp_path / route)
        project.root.mkdir()
        _write_parity_project(project)
        result_path = project.root / "native-result.json"
        completed = project.run_vfy(
            "test",
            "--engine=pytest-native",
            worker_option,
            "--no-replay",
            "--cov=app",
            "--cov-context=test",
            "--cov-report=json:coverage.json",
            "-q",
            env={"PYTEST_NATIVE_RESULT": str(result_path)},
        )
        completed.expect(0)
        assert "2 passed" in completed.stdout
        projects[route] = project
        records[route] = _execution_outcomes(result_path)

    expected_outcomes = [
        ("tests/test_alpha.py::test_alpha", "passed"),
        ("tests/test_beta.py::test_beta", "passed"),
    ]
    assert records["serial"] == records["parallel"] == expected_outcomes
    serial, parallel = projects["serial"], projects["parallel"]
    serial_data, parallel_data = serial.data(), parallel.data()
    for project, data in ((serial, serial_data), (parallel, parallel_data)):
        source = str(project.root / "app.py")
        assert data.lines(source) == [1, 2, 4, 5, 7, 8]
        assert data.contexts_by_lineno(source)[8]
        contexts = data.contexts_by_lineno(source)[8]
        assert any(context.endswith("test_alpha|teardown") for context in contexts)
        assert any(context.endswith("test_beta|teardown") for context in contexts)
    assert serial_data.arcs(str(serial.root / "app.py")) == parallel_data.arcs(
        str(parallel.root / "app.py")
    )
    serial_contexts = serial_data.contexts_by_lineno(str(serial.root / "app.py"))
    parallel_contexts = parallel_data.contexts_by_lineno(str(parallel.root / "app.py"))
    assert {line: set(values) for line, values in serial_contexts.items()} == {
        line: set(values) for line, values in parallel_contexts.items()
    }
    assert _report_semantics(serial.root / "coverage.json") == _report_semantics(
        parallel.root / "coverage.json"
    )


def test_native_worker_collection_mismatch_does_not_run_tests_or_replace_coverage(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    prior_coverage = _seed_coverage(project)
    (tmp_path / "tests/test_seed.py").unlink()
    project.write(
        "conftest.py",
        "import os\n\n"
        "def pytest_collection_modifyitems(items):\n"
        "    if os.environ.get('VFY_NATIVE_PARALLEL_INDEX') == '1':\n"
        "        items.pop()\n",
    )
    for name in ("first", "second"):
        project.write(
            f"tests/test_{name}.py",
            f"from pathlib import Path\ndef test_{name}():\n    Path('executed-{name}').touch()\n",
        )

    completed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "-q",
        env={"VFY_WORKER_START": "exec"},
    )

    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert "different test manifest" in completed.stdout.lower() + completed.stderr.lower()
    assert not (tmp_path / "executed-first").exists()
    assert not (tmp_path / "executed-second").exists()
    assert (tmp_path / ".coverage").read_bytes() == prior_coverage


@pytest.mark.parametrize(("failure", "expected_status"), [("usage", 4), ("collection", 2)])
def test_native_worker_bootstrap_preserves_pytest_exit_status_and_coverage(
    tmp_path: Path, failure: str, expected_status: int
) -> None:
    project = VfyProject(tmp_path)
    prior_coverage = _seed_coverage(project)
    (tmp_path / "tests/test_seed.py").unlink()
    project.write(
        "conftest.py",
        "import os\n"
        "import pytest\n\n"
        "def pytest_collection_modifyitems(items):\n"
        "    if os.environ.get('VFY_NATIVE_PARALLEL_INDEX') != '1':\n"
        "        return\n"
        "    if os.environ.get('VFY_NATIVE_WORKER_FAILURE') == 'usage':\n"
        "        raise pytest.UsageError('native worker usage status sentinel')\n",
    )
    for name in ("first", "second"):
        failure_source = ""
        if name == "first":
            failure_source = (
                "import os\n"
                "if (os.environ.get('VFY_NATIVE_WORKER_FAILURE') == 'collection'\n"
                "        and os.environ.get('VFY_NATIVE_PARALLEL_INDEX') == '1'):\n"
                "    raise RuntimeError('native worker collection status sentinel')\n\n"
            )
        project.write(
            f"tests/test_{name}.py",
            failure_source
            + "from pathlib import Path\n"
            + f"def test_{name}():\n    Path('executed-{name}').touch()\n",
        )

    completed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "-q",
        env={"VFY_NATIVE_WORKER_FAILURE": failure, "VFY_WORKER_START": "exec"},
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == expected_status, output
    assert f"native worker {failure} status sentinel" in output
    assert not (tmp_path / "executed-first").exists()
    assert not (tmp_path / "executed-second").exists()
    assert (tmp_path / ".coverage").read_bytes() == prior_coverage


@pytest.mark.parametrize("worker_exit", [0, 37], ids=["missing-result", "crash"])
def test_native_worker_failure_reaps_peer_preserves_coverage_and_allows_reuse(
    tmp_path: Path, worker_exit: int
) -> None:
    project = VfyProject(tmp_path, timeout=25)
    prior_coverage = _seed_coverage(project)
    (tmp_path / "tests/test_seed.py").unlink()
    project.write(
        "tests/test_crash.py",
        "import os\n"
        "import time\n"
        "from pathlib import Path\n\n"
        "def test_crash_worker():\n"
        "    root = Path('.vfy')\n"
        "    (root / 'crash-started').touch()\n"
        "    deadline = time.monotonic() + 15\n"
        "    while not (root / 'blocked-pid').exists():\n"
        "        if time.monotonic() >= deadline:\n"
        "            raise AssertionError('peer worker did not start')\n"
        "        time.sleep(0.01)\n"
        f"    os._exit({worker_exit})\n",
    )
    project.write(
        "tests/test_blocked.py",
        "import os\n"
        "import time\n"
        "from pathlib import Path\n\n"
        "def test_blocked_worker():\n"
        "    root = Path('.vfy')\n"
        "    (root / 'blocked-pid').write_text(str(os.getpid()), encoding='utf-8')\n"
        "    while True:\n"
        "        time.sleep(0.05)\n",
    )

    crashed = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=2", "--no-replay", "--cov=app", "-q"
    )

    assert crashed.returncode != 0, crashed.stdout + crashed.stderr
    assert (tmp_path / ".vfy/crash-started").is_file()
    assert sorted(path.name for path in tmp_path.glob(".coverage.*")) == []
    assert (tmp_path / ".vfy/blocked-pid").is_file()
    failure_output = crashed.stdout.lower() + crashed.stderr.lower()
    assert "worker" in failure_output
    if worker_exit:
        assert "37" in failure_output
    else:
        assert "result" in failure_output
    blocked_pid = int((tmp_path / ".vfy/blocked-pid").read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(blocked_pid, 0)
    assert (tmp_path / ".coverage").read_bytes() == prior_coverage

    (tmp_path / "tests/test_crash.py").unlink()
    (tmp_path / "tests/test_blocked.py").unlink()
    project.write("tests/test_recovery.py", "def test_reusable_worker_pool():\n    assert True\n")
    recovered = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=2", "--no-replay", "-q"
    )
    recovered.expect(0)
    assert "1 passed" in recovered.stdout


def test_native_maxfail_stops_pending_cases_and_finalizes_worker_fixtures(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write(
        "conftest.py",
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n\n"
        "@pytest.fixture(scope='session', autouse=True)\n"
        "def session_lifecycle():\n"
        "    root = Path('.vfy')\n"
        "    root.mkdir(exist_ok=True)\n"
        "    pid = os.getpid()\n"
        "    (root / f'session-start-{pid}').touch()\n"
        "    yield\n"
        "    (root / f'session-finish-{pid}').touch()\n\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    if session.testsfailed:\n"
        "        Path('.vfy/failing-worker-session-finished').touch()\n",
    )
    project.write(
        "tests/test_failure.py",
        "import time\n"
        "from pathlib import Path\n\n"
        "def test_failure():\n"
        "    root = Path('.vfy')\n"
        "    (root / 'failure-started').touch()\n"
        "    deadline = time.monotonic() + 15\n"
        "    while not (root / 'peer-started').exists():\n"
        "        if time.monotonic() >= deadline:\n"
        "            raise AssertionError('peer case did not start')\n"
        "        time.sleep(0.01)\n"
        "    assert False, 'shared failure budget sentinel'\n",
    )
    project.write(
        "tests/test_peer.py",
        "import time\n"
        "from pathlib import Path\n\n"
        "def test_inflight_case():\n"
        "    root = Path('.vfy')\n"
        "    (root / 'peer-started').touch()\n"
        "    deadline = time.monotonic() + 15\n"
        "    while not (root / 'failing-worker-session-finished').exists():\n"
        "        if time.monotonic() >= deadline:\n"
        "            raise AssertionError('failing worker did not finish its session')\n"
        "        time.sleep(0.01)\n\n"
        "def test_must_not_start_after_budget():\n"
        "    Path('.vfy/pending-case-started').touch()\n",
    )

    completed = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=2", "--no-replay", "--maxfail=1", "-q"
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    assert "shared failure budget sentinel" in output
    assert (tmp_path / ".vfy/peer-started").exists()
    assert not (tmp_path / ".vfy/pending-case-started").exists()
    started = list((tmp_path / ".vfy").glob("session-start-*"))
    finished = list((tmp_path / ".vfy").glob("session-finish-*"))
    assert len(started) == len(finished) == 2
    assert {path.name.removeprefix("session-start-") for path in started} == {
        path.name.removeprefix("session-finish-") for path in finished
    }
