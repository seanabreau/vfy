"""Late project callbacks cannot send threaded mutation parents through fork."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(os.name != "posix", reason="mutation forks require POSIX")


@pytest.mark.parametrize("trigger", ["module_filename", "worker_lease"])
def test_late_project_thread_uses_fresh_mutation_execution(tmp_path: Path, trigger: str) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_app.py",
        "import app\n\ndef test_value():\n    assert app.value() == 1\n",
    )
    project.write(
        "conftest.py",
        "import os, sys, threading\nfrom pathlib import Path\nfrom types import ModuleType\n\n"
        "owner = int(os.environ['VFY_TEST_CONTROLLER_PID'])\n"
        f"trigger = {trigger!r}\n"
        "started = False\n"
        "def start_thread():\n"
        "    global started\n"
        "    if started:\n"
        "        return\n"
        "    started = True\n"
        "    threading.Thread(target=threading.Event().wait, daemon=True).start()\n"
        "    Path('late-thread-started').write_text(trigger)\n"
        "    Path('late-thread-pid').write_text(str(os.getpid()))\n"
        "def record_unsafe_fork():\n"
        "    if os.getpid() == owner and threading.active_count() != 1:\n"
        "        Path('unsafe-threaded-fork').touch()\n"
        "class DeferredModule(ModuleType):\n"
        "    @property\n"
        "    def __file__(self):\n"
        "        start_thread()\n"
        "        return None\n"
        "def audit(event, arguments):\n"
        "    if event != 'open' or not isinstance(arguments[0], str):\n"
        "        return\n"
        "    pool = os.getenv('VFY_MUTATION_WORKER_POOL')\n"
        "    if pool is not None and Path(arguments[0]).parent == Path(pool):\n"
        "        start_thread()\n"
        "def pytest_collection_modifyitems(session, config, items):\n"
        "    if os.getpid() != owner:\n"
        "        return\n"
        "    os.register_at_fork(before=record_unsafe_fork)\n"
        "    Path('controller-pid').write_text(str(owner))\n"
        "    if trigger == 'module_filename':\n"
        "        sys.modules['deferred_project_module'] = "
        "DeferredModule('deferred_project_module')\n"
        "    else:\n"
        "        sys.addaudithook(audit)\n",
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
        "1",
        "--test-isolation",
        "file",
        "--format",
        "json",
    ]
    project.write(
        "driver.py",
        "import os\nfrom vfy.cli import main\n"
        "os.environ['VFY_TEST_CONTROLLER_PID'] = str(os.getpid())\n"
        f"raise SystemExit(main({arguments!r}))\n",
    )

    completed = project.run(sys.executable, "driver.py", env={"VFY_MUTATION_ENGINE": "auto"})
    assert completed.returncode == 0, completed
    report = json.loads(completed.stdout)
    assert (tmp_path / "late-thread-started").read_text() == trigger, report
    assert not (tmp_path / "unsafe-threaded-fork").exists(), report
    assert report["summary"] == {"protected": 1}, report
    if trigger == "module_filename":
        # The native runner reads module filenames only inside the fork child, so the
        # late thread never exists in a forking parent and the retained route stays valid.
        assert (tmp_path / "late-thread-pid").read_text() != (
            tmp_path / "controller-pid"
        ).read_text(), report
        assert report["metrics"]["fast_results"] == 1, report
        assert report["metrics"]["portable_fallbacks"] == 0, report
    else:
        assert report["metrics"]["fast_results"] == 0, report
        assert report["metrics"]["portable_fallbacks"] == 1, report
