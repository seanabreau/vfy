"""Fresh mutation pytest processes preserve controller and cleanup boundaries."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
from pathlib import Path
from typing import cast

import pytest

from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process ownership requires POSIX")

_OPTIONS = (
    "mutate",
    "--engine=pytest-native",
    "--all",
    "--source",
    "app.py",
    "--operator",
    "integer_plus_one",
    "--no-cache",
    "--workers",
    "2",
    "--timeout",
    "8",
    "--format",
    "json",
)
_ENVIRONMENT = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "VFY_MUTATION_ENGINE": "auto"}


def _project(tmp_path: Path, body: str) -> VfyProject:
    project = VfyProject(tmp_path, timeout=30)
    # A default is evaluated during import and requires a fresh pytest process.
    project.write("app.py", "def value(argument=1):\n    return argument\n")
    project.write(
        "tests/test_app.py",
        "import os, signal, subprocess, sys\nfrom pathlib import Path\nimport app\n\n"
        "def test_value():\n"
        "    if app.value.__defaults__ == (2,):\n"
        + "".join(f"        {line}\n" for line in body.splitlines())
        + "    assert app.value(5) == 5\n",
    )
    return project


def _report(completed: CommandResult) -> dict[str, object]:
    try:
        return cast(dict[str, object], json.loads(completed.stdout))
    except json.JSONDecodeError as error:
        raise AssertionError(f"mutation command returned no JSON: {completed}") from error


def _mutation(report: dict[str, object]) -> dict[str, object]:
    mutations = cast(list[dict[str, object]], report["mutations"])
    assert len(mutations) == 1, report
    return mutations[0]


def test_fresh_native_mutation_runs_as_a_direct_controller_child(tmp_path: Path) -> None:
    project = _project(tmp_path, "Path('pytest-parent').write_text(str(os.getppid()))")
    # Serial tests execute in the fresh interpreter whose parent is the controller.
    command = project.start_vfy(*_OPTIONS, "--test-isolation", "serial", env=_ENVIRONMENT)
    try:
        completed = command.wait(timeout=25)
    finally:
        if command.poll() is None:
            command.kill()
            command.wait(timeout=5)

    report = _report(completed)
    assert completed.returncode == 0, report
    assert report["summary"] == {"survived": 1}
    assert cast(dict[str, object], report["metrics"])["portable_fallbacks"] == 1
    assert int((tmp_path / "pytest-parent").read_text()) == command.process.pid


def test_portable_validation_does_not_run_compile_hooks_in_the_collected_parent(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "assert app.value.__defaults__ == (2,)")
    project.write(
        "conftest.py",
        "import os, sys\n\n"
        "controller_pid = os.getpid()\n"
        "def audit(event, arguments):\n"
        "    if event != 'compile' or os.getenv('VFY_MUTATION'):\n"
        "        return\n"
        "    if os.getpid() != controller_pid:\n"
        "        return\n"
        "    if arguments[0] == b'def value(argument=2):\\n    return argument\\n':\n"
        "        raise RuntimeError('mutated compilation escaped child isolation')\n"
        "sys.addaudithook(audit)\n",
    )

    completed = project.run_vfy(*_OPTIONS, env=_ENVIRONMENT)
    report = _report(completed)
    assert completed.returncode == 0, report
    assert report["summary"] == {"survived": 1}


@pytest.mark.parametrize("termination", ["signal", "exit-one"])
def test_portable_abrupt_exit_cannot_be_mutation_protection(
    tmp_path: Path, termination: str
) -> None:
    terminate = "os.kill(os.getpid(), signal.SIGTERM)" if termination == "signal" else "os._exit(1)"
    project = _project(tmp_path, "Path('mutant-started').touch()\n" + terminate)
    # This contract preserves the direct pytest process's raw exit status.
    completed = project.run_vfy(*_OPTIONS, "--test-isolation", "serial", env=_ENVIRONMENT)
    report = _report(completed)
    assert (tmp_path / "mutant-started").exists(), report
    assert completed.returncode == 1, report
    assert report["summary"] == {"error": 1}
    mutation = _mutation(report)
    assert (
        mutation["status"],
        mutation["killer"],
        mutation.get("phase"),
        mutation["returncode"],
    ) == (
        "error",
        None,
        None,
        -signal.SIGTERM if termination == "signal" else 1,
    ), report


def _descendant_project(tmp_path: Path, *, block: bool) -> VfyProject:
    body = (
        "child = subprocess.Popen([sys.executable, 'descendant.py'], "
        "stdout=subprocess.PIPE, text=True)\n"
        "assert child.stdout.readline().strip() == 'ready'\n"
        "Path('mutant-started').touch()\n"
    )
    if block:
        body += "signal.pause()\n"
    project = _project(tmp_path, body)
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
    return project


def _assert_descendant_released(tmp_path: Path) -> None:
    import fcntl  # noqa: PLC0415 - this module is unavailable on the skipped Windows route

    with (tmp_path / "descendant.lock").open() as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _cleanup_descendant(tmp_path: Path) -> None:
    lock = tmp_path / "descendant.lock"
    if lock.exists():
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(lock.read_text()), signal.SIGKILL)


def test_completed_portable_mutation_terminates_its_background_descendants(tmp_path: Path) -> None:
    project = _descendant_project(tmp_path, block=False)
    try:
        completed = project.run_vfy(*_OPTIONS, env=_ENVIRONMENT)
        report = _report(completed)
        assert completed.returncode == 0, report
        assert report["summary"] == {"survived": 1}
        assert (tmp_path / "mutant-started").exists(), report
        _assert_descendant_released(tmp_path)
    finally:
        _cleanup_descendant(tmp_path)


def test_interrupted_portable_mutation_cleans_descendants_and_allows_reuse(tmp_path: Path) -> None:
    project = _descendant_project(tmp_path, block=True)
    command = project.start_vfy(*_OPTIONS, env=_ENVIRONMENT)
    try:
        project.wait_for_path("mutant-started", timeout=20)
        command.send_signal(signal.SIGINT)
        completed = command.wait(timeout=10)
        assert completed.returncode != 0, completed
        _assert_descendant_released(tmp_path)
        reused = project.run_vfy(
            "test", "--engine=native", "--all", "--source", "app.py", "-q", env=_ENVIRONMENT
        )
        assert reused.returncode == 0, reused
    finally:
        if command.poll() is None:
            command.kill()
            command.wait(timeout=5)
        _cleanup_descendant(tmp_path)


def test_thread_sensitive_skip_marker_preserves_protection_with_portable_mutations(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write("app.py", "def value(argument=1):\n    return argument + 1\n")
    project.write(
        "tests/test_app.py",
        "import signal, threading\nimport pytest\nimport app\n\n"
        "def test_a_default():\n"
        "    if app.value.__defaults__ == (2,):\n"
        "        signal.pause()\n\n"
        "@pytest.mark.skipif(threading.active_count() != 1, reason='requires one thread')\n"
        "def test_body():\n"
        "    assert app.value() == 2\n",
    )
    completed = project.run_vfy(*_OPTIONS, "--timeout", "2", env=_ENVIRONMENT)
    report = _report(completed)
    assert completed.returncode == 1, report
    assert report["summary"] == {"protected": 1, "timeout": 1}
    assert cast(dict[str, object], report["metrics"])["fast_results"] == 1
    body = next(
        row
        for row in cast(list[dict[str, object]], report["mutations"])
        if cast(dict[str, object], row["owner"])["phase"] == "body"
    )
    assert (body["status"], body["killer"], body["returncode"]) == (
        "protected",
        "tests/test_app.py::test_body",
        1,
    ), report


def _prestarted_project(tmp_path: Path, parent_action: str) -> VfyProject:
    project = _descendant_project(tmp_path, block=True)
    os.mkfifo(tmp_path / "portable-ready")
    tests = tmp_path / "tests/test_app.py"
    tests.write_text(
        tests.read_text().replace(
            "        Path('mutant-started').touch()\n",
            "        with Path('mutant-runs').open('a') as stream:\n"
            "            stream.write('run\\n')\n"
            "        Path('mutant-started').touch()\n"
            "        with open('portable-ready', 'w') as ready:\n"
            "            ready.write('ready')\n",
        )
    )
    project.write(
        "driver.py",
        "import os\nfrom vfy.cli import main\n"
        "os.environ['VFY_TEST_MUTATION_CONTROLLER_PID'] = str(os.getpid())\n"
        f"raise SystemExit(main({[*_OPTIONS, '--timeout', '2']!r}))\n",
    )
    project.write(
        "conftest.py",
        "import fcntl, os, threading\nfrom pathlib import Path\n\n"
        "def pytest_collection_modifyitems(session, config, items):\n"
        "    if str(os.getpid()) != os.getenv('VFY_TEST_MUTATION_CONTROLLER_PID'):\n"
        "        return\n"
        "    with open('portable-ready') as ready:\n"
        "        assert ready.read() == 'ready'\n"
        + "".join(f"    {line}\n" for line in parent_action.splitlines()),
    )
    return project


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="native deadline monitor uses Linux"
)
def test_failed_parent_collection_discards_prestarted_mutation_results(tmp_path: Path) -> None:
    project = _prestarted_project(
        tmp_path, "raise RuntimeError('parent collection failed after portable startup')"
    )
    try:
        completed = project.run(sys.executable, "driver.py", env=_ENVIRONMENT)
        report = _report(completed)
        assert (tmp_path / "mutant-started").exists(), report
        assert completed.returncode == 1, report
        assert report["summary"] == {"error": 1}
        mutation = _mutation(report)
        assert mutation["killer"] is None, report
        assert "parent collection failed after portable startup" in str(mutation["detail"]), report
        _assert_descendant_released(tmp_path)
    finally:
        _cleanup_descendant(tmp_path)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="native deadline monitor uses Linux"
)
def test_prestarted_watchdog_remains_active_while_parent_collection_is_blocked(
    tmp_path: Path,
) -> None:
    project = _prestarted_project(
        tmp_path,
        "with open('descendant.lock') as descendant:\n"
        "    fcntl.flock(descendant, fcntl.LOCK_EX)\n"
        "Path('parent-resumed-after-cancellation').touch()",
    )
    try:
        completed = project.run(sys.executable, "driver.py", env=_ENVIRONMENT)
        report = _report(completed)
        assert (tmp_path / "parent-resumed-after-cancellation").exists(), report
        assert completed.returncode == 1, report
        assert report["summary"] == {"timeout": 1}
        mutation = _mutation(report)
        assert (mutation["killer"], mutation["returncode"], mutation["duration_ms"]) == (
            None,
            None,
            2000,
        ), report
        _assert_descendant_released(tmp_path)
    finally:
        _cleanup_descendant(tmp_path)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="native deadline monitor uses Linux"
)
def test_collection_started_thread_does_not_repeat_a_prestarted_mutation(tmp_path: Path) -> None:
    project = _prestarted_project(
        tmp_path, "threading.Thread(target=threading.Event().wait, daemon=True).start()"
    )
    try:
        completed = project.run(sys.executable, "driver.py", env=_ENVIRONMENT)
        report = _report(completed)
        assert completed.returncode == 1, report
        assert report["summary"] == {"timeout": 1}
        assert (tmp_path / "mutant-runs").read_text().splitlines() == ["run"], report
        _assert_descendant_released(tmp_path)
    finally:
        _cleanup_descendant(tmp_path)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="native deadline monitor uses Linux"
)
def test_prestarted_mutation_reaped_by_collection_code_is_not_a_survivor(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        "with open('portable-ready', 'w') as ready:\n"
        "    ready.write('ready')\n"
        "raise AssertionError('mutated default detected')",
    )
    os.mkfifo(tmp_path / "portable-ready")
    project.write(
        "driver.py",
        "import os\nfrom vfy.cli import main\n"
        "os.environ['VFY_TEST_MUTATION_CONTROLLER_PID'] = str(os.getpid())\n"
        f"raise SystemExit(main({[*_OPTIONS, '--test-isolation', 'serial']!r}))\n",
    )
    # Collection code in the controller reaps any exited child, as a SIGCHLD
    # cleanup loop would. Waiting for the mutant to report readiness makes the
    # reap happen before vfy can read the exit status.
    project.write(
        "conftest.py",
        "import os, time\nfrom pathlib import Path\n\n"
        "def pytest_collection_modifyitems(session, config, items):\n"
        "    if str(os.getpid()) != os.getenv('VFY_TEST_MUTATION_CONTROLLER_PID'):\n"
        "        return\n"
        "    with open('portable-ready') as ready:\n"
        "        assert ready.read() == 'ready'\n"
        "    deadline = time.monotonic() + 20\n"
        "    while True:\n"
        "        waited, status = os.waitpid(-1, os.WNOHANG)\n"
        "        if waited:\n"
        "            code = os.waitstatus_to_exitcode(status)\n"
        "            Path('reaped-exit-code').write_text(str(code))\n"
        "            return\n"
        "        assert time.monotonic() < deadline, 'mutation child did not exit'\n"
        "        time.sleep(0.01)\n",
    )

    completed = project.run(sys.executable, "driver.py", env=_ENVIRONMENT)
    report = _report(completed)
    # The mutant's pytest run failed; only collection code observed that status.
    assert (tmp_path / "reaped-exit-code").read_text() == "1", report
    assert completed.returncode == 1, report
    assert report["summary"] == {"error": 1}, report
    mutation = _mutation(report)
    assert (mutation["status"], mutation["killer"], mutation["returncode"]) == (
        "error",
        None,
        None,
    ), report
    assert "is unavailable because another wait call" in str(mutation["detail"]), report
