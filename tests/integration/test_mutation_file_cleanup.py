"""File-isolated pytest children always tear down acquired fixtures."""

from __future__ import annotations

import contextlib
import json
import os
import signal
from pathlib import Path
from typing import cast

import pytest

from tests.support.vfy_project import VfyProject


@pytest.mark.skipif(os.name != "posix", reason="file-isolated mutation execution requires fork")
@pytest.mark.parametrize("engine", ["auto", "portable"])
def test_cancelled_file_group_stops_descendants_before_earlier_group_continues(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write("app.py", "def value(argument=1):\n    return argument\n")
    for name in ("earlier-ready", "killer-ready"):
        os.mkfifo(tmp_path / name)
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
        "tests/test_a_earlier.py",
        "import fcntl\nimport app\n\n"
        "def test_earlier():\n"
        "    if app.value() == 1:\n        return\n"
        "    with open('earlier-ready') as ready:\n        ready.read()\n"
        "    with open('cancelled.lock') as group:\n"
        "        fcntl.flock(group, fcntl.LOCK_EX)\n"
        "        with open('descendant.lock') as descendant:\n"
        "            fcntl.flock(descendant, fcntl.LOCK_EX)\n",
    )
    project.write(
        "tests/test_b_killer.py",
        "import app\n\n"
        "def test_killer():\n"
        "    if app.value.__defaults__ != (1,):\n"
        "        with open('killer-ready') as ready:\n            ready.read()\n"
        "    assert app.value.__defaults__ == (1,)\n",
    )
    project.write(
        "tests/test_c_cancelled.py",
        "import fcntl, signal, subprocess, sys\nimport app\n\n"
        "def test_cancelled():\n"
        "    if app.value.__defaults__ == (1,):\n        return\n"
        "    with open('cancelled.lock', 'w') as group:\n"
        "        fcntl.flock(group, fcntl.LOCK_EX)\n"
        "        child = subprocess.Popen([sys.executable, 'descendant.py'], "
        "stdout=subprocess.PIPE, text=True)\n"
        "        assert child.stdout.readline().strip() == 'ready'\n"
        "        for name in ('earlier-ready', 'killer-ready'):\n"
        "            with open(name, 'w') as ready:\n                ready.write('ready')\n"
        "        signal.pause()\n",
    )
    try:
        completed = project.run_vfy(
            "mutate",
            "--engine=pytest-native",
            "--all",
            "--source",
            "app.py",
            "--operator",
            "integer_plus_one",
            "--no-cache",
            "--workers",
            "3",
            "--timeout",
            "8",
            "--test-isolation",
            "file",
            "--format",
            "json",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "VFY_MUTATION_ENGINE": engine},
        )
        completed.expect(0)
        report = json.loads(completed.stdout)
        assert report["summary"] == {"protected": 1}
        assert report["mutations"][0]["killer"] == "tests/test_b_killer.py::test_killer"
    finally:
        lock = tmp_path / "descendant.lock"
        if lock.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(lock.read_text()), signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="file-isolated mutation execution requires fork")
def test_native_file_group_setup_failure_tears_down_fixtures_and_allows_reuse(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path, timeout=60)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "conftest.py",
        "import os\n\n"
        "import pytest\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def record_finalizer():\n"
        "    yield\n"
        "    with open('finalizers.txt', 'a', encoding='utf-8') as stream:\n"
        "        stream.write('mutant\\n' if os.getenv('VFY_MUTATION') else 'baseline\\n')\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def failing_resource(record_finalizer):\n"
        "    if os.getenv('VFY_MUTATION'):\n"
        "        raise RuntimeError('fixture setup failed after dependency setup')\n",
    )
    project.write(
        "tests/test_app.py",
        "import app\n\ndef test_value():\n    assert app.value() == 1\n",
    )

    completed = project.run_vfy(
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
        "8",
        "--test-isolation",
        "file",
        "--format",
        "json",
        env={
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "VFY_MUTATION_ENGINE": "portable",
        },
    )
    completed.expect(0)
    report = cast(dict[str, object], json.loads(completed.stdout))
    assert report["summary"] == {"protected": 1}
    mutation = cast(list[dict[str, object]], report["mutations"])[0]
    assert mutation["status"] == "protected"
    assert mutation["phase"] == "setup"
    assert "mutant" in (tmp_path / "finalizers.txt").read_text().splitlines()

    reused = project.run_vfy(
        "test",
        "--engine=native",
        "--all",
        "--source",
        "app.py",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    reused.expect(0)


@pytest.mark.skipif(os.name != "posix", reason="file-isolated mutation execution requires fork")
def test_file_group_watchdog_timeout_has_no_pytest_return_code(tmp_path: Path) -> None:
    project = VfyProject(tmp_path, timeout=60)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "conftest.py",
        "import os\n\n"
        "def pytest_configure(config):\n"
        "    if os.getenv('VFY_MUTATION'):\n"
        "        os.environ['VFY_MUTATION_TEST_TIMEOUT'] = '0.05'\n",
    )
    project.write(
        "tests/test_app.py",
        "import time\n\n"
        "import app\n\n"
        "def test_value():\n"
        "    if app.value() != 1:\n"
        "        time.sleep(0.5)\n"
        "    assert app.value() == 1\n",
    )

    completed = project.run_vfy(
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
        "8",
        "--test-isolation",
        "file",
        "--format",
        "json",
        env={
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "VFY_MUTATION_ENGINE": "portable",
        },
    )
    completed.expect(1)
    report = cast(dict[str, object], json.loads(completed.stdout))
    assert report["summary"] == {"timeout": 1}
    mutation = cast(list[dict[str, object]], report["mutations"])[0]
    assert mutation["status"] == "timeout"
    assert mutation["returncode"] is None
    assert mutation["killer"] is None
