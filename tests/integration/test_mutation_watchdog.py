"""The public mutation watchdog uses clean history and observed child exits."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.history import read_history
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
@pytest.mark.parametrize("policy", ["adaptive", "fixed"])
def test_fixed_watchdog_allows_a_mutant_to_outlast_clean_history(
    tmp_path: Path, policy: str
) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "test_app.py",
        "import time\nfrom pathlib import Path\nimport app\n\n"
        "def test_value():\n"
        "    if app.value() != 1:\n"
        "        Path('mutant-started').touch()\n"
        "        time.sleep(2)\n"
        "        Path('mutant-finished').touch()\n"
        "    assert app.value() > 0\n",
    )
    arguments = () if policy == "adaptive" else ("--timeout-policy=fixed",)
    result = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--no-cache",
        "--workers=1",
        "--test-isolation=serial",
        "--timeout=3",
        "--format=json",
        *arguments,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert result.stdout, result
    report = json.loads(result.stdout)
    assert (tmp_path / "mutant-started").exists(), report
    assert report["metrics"]["fast_results"] == 1, report
    mutation = report["mutations"][0]
    if policy == "fixed":
        assert result.returncode == 0, (report, result.stderr)
        assert report["summary"] == {"survived": 1}, report
        assert (tmp_path / "mutant-finished").exists(), report
        assert mutation["duration_ms"] >= 2000, report
    else:
        assert result.returncode == 1, (report, result.stderr)
        assert report["summary"] == {"timeout": 1}, report
        assert mutation["duration_ms"] == 1000, report
        assert not (tmp_path / "mutant-finished").exists(), report
    assert mutation["killer"] is None, report


@pytest.mark.skipif(os.name != "posix", reason="collected mutation execution requires fork")
def test_collected_mutation_watchdog_reduces_the_configured_ceiling(tmp_path: Path) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "test_app.py",
        "import time\n"
        "from pathlib import Path\n"
        "import app\n"
        "def test_value():\n"
        "    if app.value() != 1:\n"
        "        Path('mutant-started').touch()\n"
        "        while True:\n"
        "            time.sleep(0.05)\n"
        "    assert app.value() == 1\n",
    )
    result = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--max-mutations",
        "1",
        "--no-cache",
        "--workers",
        "1",
        "--timeout",
        "60",
        "--format",
        "json",
    )
    assert result.returncode == 1, result
    report = json.loads(result.stdout)
    assert (tmp_path / "mutant-started").exists(), report
    assert report["summary"] == {"timeout": 1}
    mutation = report["mutations"][0]
    assert mutation["tests"] == ["test_app.py::test_value"]
    history = read_history(tmp_path / ".vfy/test-history.sqlite3")
    recorded_ms = history["tests"]["test_app.py::test_value"]["duration_ms"]
    assert recorded_ms > 0
    expected_seconds = min(60, max(1, recorded_ms / 1000 * 10 + 0.5))
    assert mutation["duration_ms"] == pytest.approx(expected_seconds * 1000)
    assert mutation["duration_ms"] < 60_000
    assert mutation["returncode"] is None
    assert mutation["killer"] is None


@pytest.mark.skipif(sys.platform != "linux", reason="the stalled controller uses Linux waitid")
def test_mutation_completed_before_a_late_deadline_check_keeps_its_result(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path, timeout=60)
    # The default needs a fresh process; the body runs in a collected fork.
    project.write("app.py", "def value(argument=1):\n    return argument + 1\n")
    os.mkfifo(tmp_path / "portable-ready")
    os.mkfifo(tmp_path / "portable-release")
    project.write(
        "tests/test_app.py",
        "import os\nfrom pathlib import Path\nimport app\n\n"
        "def test_value():\n"
        "    if app.value.__defaults__ == (2,):\n"
        "        Path('portable-pid').write_text(str(os.getpid()))\n"
        "        with open('portable-ready', 'w') as ready:\n"
        "            ready.write('ready')\n"
        "        with open('portable-release') as release:\n"
        "            assert release.read() == 'release'\n"
        "    assert app.value() == 2\n",
    )
    # The controller forks the collected mutant while the fresh mutant waits.
    # Its fork hook releases the fresh mutant, waits until both children have
    # exited without reaping them, then stalls past both deadlines before vfy
    # reaches its deadline check.
    project.write(
        "conftest.py",
        "import os, time\nfrom pathlib import Path\n\n"
        "owner = os.getenv('VFY_TEST_MUTATION_CONTROLLER_PID')\n"
        "def wait_for_exit(pid):\n"
        "    os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)\n"
        "def record_collected_child():\n"
        "    if str(os.getppid()) == owner:\n"
        "        Path('collected-pid.tmp').write_text(str(os.getpid()))\n"
        "        os.replace('collected-pid.tmp', 'collected-pid')\n"
        "def stall_controller():\n"
        "    if str(os.getpid()) != owner:\n"
        "        return\n"
        "    with open('portable-release', 'w') as release:\n"
        "        release.write('release')\n"
        "    wait_for_exit(int(Path('portable-pid').read_text()))\n"
        "    deadline = time.monotonic() + 20\n"
        "    while not Path('collected-pid').exists():\n"
        "        assert time.monotonic() < deadline, 'collected mutant did not start'\n"
        "        time.sleep(0.01)\n"
        "    wait_for_exit(int(Path('collected-pid').read_text()))\n"
        "    Path('controller-stalled').touch()\n"
        "    time.sleep(5.5)\n"
        "def pytest_collection_modifyitems(session, config, items):\n"
        "    if str(os.getpid()) != owner:\n"
        "        return\n"
        "    with open('portable-ready') as ready:\n"
        "        assert ready.read() == 'ready'\n"
        "    os.register_at_fork(\n"
        "        after_in_child=record_collected_child, after_in_parent=stall_controller\n"
        "    )\n",
    )
    arguments = [
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
        "--test-isolation",
        "serial",
        "--timeout",
        "5",
        "--format",
        "json",
    ]
    project.write(
        "driver.py",
        "import os\nfrom vfy.cli import main\n"
        "os.environ['VFY_TEST_MUTATION_CONTROLLER_PID'] = str(os.getpid())\n"
        f"raise SystemExit(main({arguments!r}))\n",
    )

    result = project.run(
        sys.executable,
        "driver.py",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "VFY_MUTATION_ENGINE": "auto"},
    )
    report = json.loads(result.stdout)
    assert (tmp_path / "controller-stalled").exists(), report
    assert result.returncode == 0, report
    assert report["summary"] == {"protected": 2}, report
    assert report["metrics"]["fast_results"] == 1, report
    assert report["metrics"]["portable_fallbacks"] == 1, report
