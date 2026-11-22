"""Watch interruption releases native workers and their descendants."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.skipif(os.name != "posix", reason="native workers use POSIX process groups")
@pytest.mark.parametrize("junit_source", ["configuration", "environment"])
def test_failed_watch_worker_preserves_accepted_junit_before_controller_publication(
    tmp_path: Path, junit_source: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(".vfy/.keep", "")
    environment = (
        {"PYTEST_ADDOPTS": "--junitxml=results.xml"} if junit_source == "environment" else {}
    )
    if junit_source == "configuration":
        project.write("pytest.ini", "[pytest]\naddopts = --junitxml=results.xml\n")
    project.write(
        "conftest.py",
        "import os, signal\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "def pytest_collection_modifyitems():\n"
        "    if (os.getenv('VFY_FAIL_WORKER')\n"
        "            and os.getenv('VFY_NATIVE_PARALLEL_INDEX') == '0'):\n"
        "        raise pytest.UsageError('worker-only collection failure')\n"
        "def pytest_sessionfinish(session):\n"
        "    if (os.getenv('VFY_FAIL_WORKER')\n"
        "            and 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ\n"
        "            and session.exitstatus == 4):\n"
        "        Path('.vfy/controller-finish').touch()\n"
        "        signal.pause()\n",
    )
    for name in ("a", "b"):
        project.write(f"tests/test_{name}.py", f"def test_{name}():\n    pass\n")

    baseline = project.run_vfy(
        "test",
        "--engine=native",
        "--workers=1",
        "--no-replay",
        "--no-cov",
        "-q",
        env=environment,
    )
    baseline.expect(0)
    report = tmp_path / "results.xml"
    accepted = report.read_bytes()
    command = project.start_vfy(
        "test",
        "--engine=native",
        "--watch",
        "--workers=2",
        "--no-replay",
        "--no-cov",
        "-q",
        env={**environment, "VFY_FAIL_WORKER": "1"},
    )
    try:
        try:
            project.wait_for_path(".vfy/controller-finish", timeout=30)
        except TimeoutError as exc:
            pytest.fail(f"{exc}\n{command.text('stdout')}\n{command.text()}")
        # The controller is blocked before watch can publish its staged report.
        assert report.read_bytes() == accepted
        command.send_signal(signal.SIGINT)
        completed = command.wait(timeout=15)
        completed.expect(2)
        assert report.read_bytes() == accepted
    finally:
        if command.poll() is None:
            command.send_signal(signal.SIGINT)
            command.wait(timeout=15)

    recovered = project.run_vfy(
        "test",
        "--engine=native",
        "--workers=2",
        "--no-replay",
        "--no-cov",
        "-q",
        env=environment,
    )
    recovered.expect(0)
    assert [case.attrib["name"] for case in ET.parse(report).findall(".//testcase")] == [
        "test_a",
        "test_b",
    ]


@pytest.mark.skipif(os.name != "posix", reason="native workers use POSIX process groups")
@pytest.mark.parametrize(("workers", "names"), [("2", ("a", "b")), ("auto", ("a",))])
def test_watch_interrupt_stops_native_worker_descendants(
    tmp_path: Path, workers: str, names: tuple[str, ...]
) -> None:
    import fcntl  # noqa: PLC0415 - POSIX-only lock proof

    project = VfyProject(tmp_path)
    project.write(".vfy/.keep", "")
    project.write(
        "descendant.py",
        "import fcntl, signal, sys\n"
        "with open(sys.argv[1], 'w') as locked:\n"
        "    fcntl.flock(locked, fcntl.LOCK_EX)\n"
        "    print('ready', flush=True)\n"
        "    signal.pause()\n",
    )
    for name in names:
        project.write(
            f"tests/test_{name}.py",
            "import fcntl, os, signal, subprocess, sys\n"
            "from pathlib import Path\n"
            "def test_wait():\n"
            f"    with open('.vfy/worker-{name}.lock', 'w') as locked:\n"
            "        fcntl.flock(locked, fcntl.LOCK_EX)\n"
            f"        child = subprocess.Popen([sys.executable, 'descendant.py', "
            f"'.vfy/descendant-{name}.lock'], stdout=subprocess.PIPE, text=True)\n"
            "        assert child.stdout.readline().strip() == 'ready'\n"
            f"        Path('.vfy/ready-{name}').write_text(str(os.getpid()))\n"
            "        signal.pause()\n",
        )
    command = project.start_vfy(
        "test", "--engine=native", "--watch", f"--workers={workers}", "--no-cov"
    )
    try:
        try:
            for name in names:
                project.wait_for_path(f".vfy/ready-{name}", timeout=30)
        except TimeoutError as exc:
            pytest.fail(f"{exc}\n{command.text('stdout')}\n{command.text()}")
        command.send_signal(signal.SIGINT)
        try:
            completed = command.wait(timeout=15)
        except subprocess.TimeoutExpired as exc:
            pytest.fail(f"{exc}\n{command.text('stdout')}\n{command.text()}")
        completed.expect(2)
        for name in names:
            for kind in ("worker", "descendant"):
                with (tmp_path / f".vfy/{kind}-{name}.lock").open() as locked:
                    fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if command.poll() is None:
            command.send_signal(signal.SIGINT)
            command.wait(timeout=15)


@pytest.mark.skipif(os.name != "posix", reason="native workers use POSIX process groups")
def test_watch_interrupt_preserves_coverage_until_a_fresh_worker_run(
    tmp_path: Path,
) -> None:
    # This proof forbids installed pytest runtestprotocol and checks native replay artifacts.
    import fcntl  # noqa: PLC0415 - POSIX-only lock proof

    project = VfyProject(tmp_path)
    project.write(".vfy/.keep", "")
    project.write(
        "conftest.py",
        "import _pytest.runner\n"
        "def _forbid_stock_protocol(*args, **kwargs):\n"
        "    raise AssertionError('stock pytest runtestprotocol was called')\n"
        "_pytest.runner.runtestprotocol = _forbid_stock_protocol\n",
    )

    def write_app(value: str) -> None:
        project.write(
            "app.py",
            f"def baseline():\n    return 'baseline'\n\ndef recovered():\n    return '{value}'\n",
        )

    project.write(
        "descendant.py",
        "import fcntl, signal, sys\n"
        "with open(sys.argv[1], 'w') as locked:\n"
        "    fcntl.flock(locked, fcntl.LOCK_EX)\n"
        "    print('ready', flush=True)\n"
        "    signal.pause()\n",
    )
    for name in ("a", "b"):
        project.write(
            f"tests/test_{name}.py",
            f"""import fcntl, os, signal, subprocess, sys
from pathlib import Path
from app import baseline, recovered

def test_mode():
    mode_path = Path(".vfy/mode")
    mode = mode_path.read_text(encoding="utf-8") if mode_path.exists() else "baseline"
    if mode == "baseline":
        assert baseline() == "baseline"
    else:
        assert recovered() == ("interrupted" if mode == "interrupt" else "recovered")
    if mode == "interrupt":
        with open(".vfy/worker-{name}.lock", "w") as locked:
            fcntl.flock(locked, fcntl.LOCK_EX)
            child = subprocess.Popen(
                [sys.executable, "descendant.py", ".vfy/descendant-{name}.lock"],
                stdout=subprocess.PIPE,
                text=True,
            )
            assert child.stdout.readline().strip() == "ready"
            Path(".vfy/ready-{name}").write_text(str(os.getpid()), encoding="utf-8")
            signal.pause()
""",
        )

    write_app("first")
    command = project.start_vfy(
        "--verbose",
        "test",
        "--engine=pytest-native",
        "--watch",
        "--workers=2",
        "--cov=app",
        "--cov-report=json:coverage.json",
    )
    try:
        try:
            command.wait_for_match(r"Watch run \d+: pytest exited 0", timeout=30)
        except TimeoutError as exc:
            pytest.fail(f"{exc}\n{command.text('stdout')}\n{command.text()}")

        accepted_names = (
            ".coverage",
            ".vfy/test-history.sqlite3",
            ".vfy/replay.coverage",
            ".vfy/test-run.json",
            "coverage.json",
        )
        accepted = {name: (tmp_path / name).read_bytes() for name in accepted_names}

        # Changing app.py triggers a fresh watch run; the mode file selects its
        # blocked branch without partially rewriting the collected test files.
        project.write(".vfy/mode", "interrupt")
        write_app("interrupted")
        try:
            for name in ("a", "b"):
                project.wait_for_path(f".vfy/ready-{name}", timeout=30)
        except TimeoutError as exc:
            pytest.fail(f"{exc}\n{command.text('stdout')}\n{command.text()}")

        command.send_signal(signal.SIGINT)
        try:
            completed = command.wait(timeout=15)
        except subprocess.TimeoutExpired as exc:
            pytest.fail(f"{exc}\n{command.text('stdout')}\n{command.text()}")
        completed.expect(2)
        assert {name: (tmp_path / name).read_bytes() for name in accepted_names} == accepted

        for name in ("a", "b"):
            for kind in ("worker", "descendant"):
                with (tmp_path / f".vfy/{kind}-{name}.lock").open() as locked:
                    fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if command.poll() is None:
            command.send_signal(signal.SIGINT)
            command.wait(timeout=15)

    project.write(".vfy/mode", "recovery")
    write_app("recovered")
    recovered = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--cov=app",
        "--cov-report=json:coverage.json",
        "-q",
    )
    recovered.expect(0)
    assert (tmp_path / ".coverage").read_bytes() != accepted[".coverage"]
    assert (tmp_path / "coverage.json").read_bytes() != accepted["coverage.json"]
    app_report = next(
        report
        for path, report in json.loads((tmp_path / "coverage.json").read_text(encoding="utf-8"))[
            "files"
        ].items()
        if Path(path).name == "app.py"
    )
    assert 5 in app_report["executed_lines"]
