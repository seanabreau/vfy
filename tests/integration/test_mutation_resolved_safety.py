"""Resolved mutation modes own their environment and late fork eligibility."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject

_WORKER_ENVIRONMENT = (
    "VFY_MUTATION_TEST_ISOLATION",
    "VFY_MUTATION_WORKER_POOL",
    "VFY_MUTATION_WORKERS",
    "VFY_MUTATION_TEST_TIMEOUT",
)


@pytest.mark.skipif(sys.platform != "linux", reason="independent mutations require Linux")
@pytest.mark.parametrize("threaded_entry", [False, True], ids=["explicit-serial", "default-serial"])
def test_resolved_serial_masks_and_restores_inherited_worker_environment(
    tmp_path: Path, threaded_entry: bool
) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "test_app.py",
        "import os\nimport app\n\n"
        "def test_value():\n"
        f"    for name in {_WORKER_ENVIRONMENT!r}:\n"
        "        assert name not in os.environ, name\n"
        "    assert app.value() == 1\n",
    )
    arguments = [
        "mutate",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--workers",
        "1",
        "--no-cache",
        "--format",
        "json",
    ]
    if not threaded_entry:
        arguments.extend(("--test-isolation", "serial"))
    inherited = {
        "VFY_MUTATION_TEST_ISOLATION": "file",
        "VFY_MUTATION_WORKER_POOL": str(tmp_path / "missing-parent-pool"),
        "VFY_MUTATION_WORKERS": "1",
        "VFY_MUTATION_TEST_TIMEOUT": "0.001",
    }
    project.write(
        "driver.py",
        "import json, os, threading\nfrom pathlib import Path\nfrom vfy.cli import main\n"
        f"inherited = {inherited!r}\n"
        "os.environ.update(inherited)\n"
        "stop = threading.Event()\n"
        "thread = None\n"
        f"if {threaded_entry!r}:\n"
        "    thread = threading.Thread(target=stop.wait, daemon=True)\n"
        "    thread.start()\n"
        "try:\n"
        f"    result = main({arguments!r})\n"
        "    restored = {name: os.environ.get(name) for name in inherited}\n"
        "    Path('restored-environment.json').write_text(json.dumps(restored))\n"
        "    assert restored == inherited\n"
        "finally:\n"
        "    stop.set()\n"
        "    if thread is not None:\n"
        "        thread.join()\n"
        "raise SystemExit(result)\n",
    )

    completed = project.run_python("driver.py", env={"VFY_MUTATION_ENGINE": "auto"})
    completed.expect(0)
    assert json.loads(completed.stdout)["summary"] == {"protected": 1}
    assert json.loads((tmp_path / "restored-environment.json").read_text()) == inherited


@pytest.mark.skipif(os.name != "posix", reason="file-isolated mutation execution requires fork")
def test_file_group_rechecks_threads_after_worker_lease_acquisition(tmp_path: Path) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "conftest.py",
        "import os, sys, threading\nfrom pathlib import Path\n"
        "started = False\n"
        "stop = threading.Event()\n"
        "def audit(event, args):\n"
        "    global started\n"
        "    if started or event != 'open':\n"
        "        return\n"
        "    path = args[0]\n"
        "    pool = os.environ.get('VFY_MUTATION_WORKER_POOL')\n"
        "    if not isinstance(path, str) or pool is None:\n"
        "        return\n"
        "    if path.startswith(pool + os.sep) and path.rsplit(os.sep, 1)[-1].isdigit():\n"
        "        started = True\n"
        "        threading.Thread(target=stop.wait, daemon=True).start()\n"
        "        Path('late-thread-started').touch()\n"
        "def before_fork():\n"
        "    if started:\n"
        "        Path('unsafe-forked').touch()\n"
        "sys.addaudithook(audit)\n"
        "os.register_at_fork(before=before_fork)\n",
    )
    project.write(
        "test_app.py",
        "from pathlib import Path\nimport app\n"
        "def test_value():\n"
        "    Path('test-executed').touch()\n"
        "    assert app.value() == 1\n",
    )

    completed = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--test-isolation",
        "file",
        "--workers",
        "1",
        "--no-cache",
        "--format",
        "json",
    )
    assert completed.returncode == 1, completed
    assert "automatic mutation baseline collection failed" in completed.stderr
    assert completed.stdout == ""
    assert (tmp_path / "late-thread-started").is_file()
    assert not (tmp_path / "unsafe-forked").exists()
    assert not (tmp_path / "test-executed").exists()
    assert not (tmp_path / ".vfy/mutation-baseline.json").exists()
    assert not (tmp_path / ".vfy/test-run.json").exists()
