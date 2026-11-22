"""Mutation completion preserves process ownership and releases readiness FDs."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not hasattr(os, "pidfd_open"),
    reason="Linux pidfd readiness",
)
@pytest.mark.parametrize("fallback", [False, True], ids=["pidfd", "unavailable"])
def test_repeated_mutation_commands_close_readiness_fds_and_preserve_sigchld(
    tmp_path: Path, fallback: bool
) -> None:
    project = VfyProject(tmp_path, timeout=45)
    project.write("app.py", "def first():\n    return 1\n\ndef second():\n    return 1\n")
    project.write(
        "tests/test_app.py",
        "import json\nfrom pathlib import Path\nimport app\n\n"
        "def test_values():\n"
        "    values = (app.first(), app.second())\n"
        "    inherited = []\n"
        "    for path in Path('/proc/self/fd').iterdir():\n"
        "        try:\n"
        "            if path.readlink().as_posix() == 'anon_inode:[pidfd]':\n"
        "                inherited.append(path.name)\n"
        "        except FileNotFoundError:\n"
        "            continue\n"
        "    stage = 'first' if values[0] == 2 else 'second' if values[1] == 2 else 'baseline'\n"
        "    Path(stage + '-pidfds.json').write_text(json.dumps(inherited))\n"
        "    assert values == (1, 1)\n",
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
        "--format",
        "json",
    ]
    project.write(
        "driver.py",
        "import contextlib, errno, fcntl, gc, io, json, os, signal\n"
        "from pathlib import Path\nfrom vfy.cli import main\n\n"
        "real_pidfd_open = os.pidfd_open\n"
        "requests, opened = [], []\n"
        "def open_pidfd(pid, flags=0):\n"
        "    requests.append(pid)\n"
        f"    if {fallback!r}:\n"
        "        raise OSError(errno.ENOSYS, 'pidfd unavailable')\n"
        "    fd = real_pidfd_open(pid, flags)\n"
        "    try:\n"
        "        high_fd = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 2048)\n"
        "    finally:\n"
        "        os.close(fd)\n"
        "    opened.append([pid, high_fd])\n"
        "    return high_fd\n"
        "os.pidfd_open = open_pidfd\n"
        "def child_signal(signum, frame):\n"
        "    pass\n"
        "signal.signal(signal.SIGCHLD, child_signal)\n"
        "counts, reports = [], []\n"
        "for repetition in range(3):\n"
        "    with contextlib.redirect_stdout(io.StringIO()) as output:\n"
        f"        code = main({arguments!r})\n"
        "    report = json.loads(output.getvalue())\n"
        "    assert code == 0, report\n"
        "    assert signal.getsignal(signal.SIGCHLD) is child_signal\n"
        "    reports.append([report['summary'], report['metrics']['fast_results']])\n"
        "    gc.collect()\n"
        "    counts.append(len(os.listdir('/proc/self/fd')))\n"
        "    for path in Path('/proc/self/fd').iterdir():\n"
        "        try:\n"
        "            target = path.readlink().as_posix()\n"
        "        except FileNotFoundError:\n"
        "            continue\n"
        "        assert target != 'anon_inode:[pidfd]', (repetition, path, target)\n"
        "print(json.dumps({\n"
        "    'counts': counts, 'reports': reports, 'requests': requests, 'opened': opened\n"
        "}))\n",
    )

    completed = project.run(sys.executable, "driver.py", env={"VFY_MUTATION_ENGINE": "auto"})

    assert completed.returncode == 0, completed
    report = json.loads(completed.stdout)
    assert report["requests"], report
    assert bool(report["opened"]) is not fallback, report
    assert all(fd >= 2048 for _pid, fd in report["opened"]), report
    assert report["reports"] == [[{"protected": 2}, 2]] * 3, report
    assert report["counts"] == [report["counts"][0]] * 3, report
    for stage in ("first", "second"):
        assert json.loads((tmp_path / f"{stage}-pidfds.json").read_text()) == [], stage


@pytest.mark.skipif(os.name != "posix", reason="forked mutation leader and signal cleanup")
def test_result_pipe_eof_does_not_complete_a_live_mutation_child(tmp_path: Path) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write(
        "app.py",
        "def first():\n    return 1\n\ndef second():\n    return 1\n",
    )
    project.write(
        "conftest.py",
        "import os, signal\nfrom pathlib import Path\nimport app\n\n"
        "real_exit = os._exit\n"
        "def exit_after_cleanup(status):\n"
        "    if os.getenv('VFY_MUTATION_JOB_GROUP') == str(os.getpid()) and app.first() == 2:\n"
        "        Path('exit-cleanup-started').write_text(str(os.getpid()))\n"
        "        while True:\n"
        "            signal.pause()\n"
        "    real_exit(status)\n"
        "os._exit = exit_after_cleanup\n",
    )
    project.write(
        "tests/test_app.py",
        "import app\n\n"
        "def test_values():\n"
        "    assert app.first() == 1\n"
        "    assert app.second() == 1\n",
    )

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
        "2",
        "--timeout",
        "2",
        "--format",
        "json",
        env={"VFY_MUTATION_ENGINE": "auto"},
    )

    assert completed.stdout, completed
    report = json.loads(completed.stdout)
    assert completed.returncode == 1, (report, completed.stderr)
    assert report["summary"] == {"protected": 1, "timeout": 1}, report
    assert report["metrics"]["fast_results"] == 2, report
    first, second = report["mutations"]
    assert (first["status"], first["killer"], first["returncode"]) == (
        "timeout",
        None,
        None,
    ), first
    assert second["status"] == "protected", second
    pid = int((tmp_path / "exit-cleanup-started").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
