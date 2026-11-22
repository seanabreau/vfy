"""Coverage.py differentials for `[run] patch` values."""

from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path

import pytest

from tests.support import VfyProject


def _measured_lines(project: VfyProject, source: Path) -> list[int]:
    with closing(project.data()) as data:
        return sorted(data.lines(str(source)) or [])


def test_exit_patch_saves_data_before_os_exit_like_coverage_py(tmp_path: Path) -> None:
    measured: dict[str, list[int]] = {}
    for tool in ("coverage", "vfy"):
        project = VfyProject(tmp_path / tool)
        project.write(".coveragerc", "[run]\npatch = _exit\n")
        script = project.write(
            "script.py", "import os\ndef work():\n    return 1\nwork()\nos._exit(3)\n"
        )
        command = ("run", "script.py")
        collected = (
            project.run_coverage_py(*command)
            if tool == "coverage"
            else project.run_vfy("coverage", *command)
        )
        assert collected.returncode == 3, collected.stdout + collected.stderr
        measured[tool] = _measured_lines(project, script)
    assert measured["vfy"] == measured["coverage"] == [1, 2, 3, 4, 5]


def test_exit_patch_retains_stopped_data_after_owner_is_dropped(tmp_path: Path) -> None:
    measured: dict[str, list[int]] = {}
    for tool in ("coverage", "vfy"):
        project = VfyProject(tmp_path / tool)
        subject = project.write("subject.py", "def work():\n    return 42\n")
        project.write(
            "collect.py",
            "import gc\n"
            "import os\n"
            f"from {tool} import Coverage\n"
            "from subject import work\n"
            "cov = Coverage(config_file=False, source=['subject'])\n"
            "cov.set_option('run:patch', ['_exit'])\n"
            "cov.start()\n"
            "assert work() == 42\n"
            "cov.stop()\n"
            "del cov\n"
            "gc.collect()\n"
            "os._exit(3)\n",
        )
        result = project.run_python("collect.py")
        result.expect(3)
        measured[tool] = _measured_lines(project, subject)
    assert measured["vfy"] == measured["coverage"] == [2]


@pytest.mark.parametrize("stop", [False, True])
def test_exit_patch_wraps_current_exit_when_collection_starts_again(
    tmp_path: Path, stop: bool
) -> None:
    measured: dict[str, list[int]] = {}
    for tool in ("coverage", "vfy"):
        project = VfyProject(tmp_path / tool)
        subject = project.write("subject.py", "def work():\n    return 42\n")
        project.write(
            "collect.py",
            "import os\n"
            f"from {tool} import Coverage\n"
            "from subject import work\n"
            "original_exit = os._exit\n"
            "cov = Coverage(config_file=False, source=['subject'])\n"
            "cov.set_option('run:patch', ['_exit'])\n"
            "cov.start()\n"
            "assert work() == 42\n"
            f"{'cov.stop()' if stop else ''}\n"
            "os._exit = original_exit\n"
            "cov.start()\n"
            "assert work() == 42\n"
            "os._exit(3)\n",
        )
        result = project.run_python("collect.py")
        result.expect(3)
        measured[tool] = _measured_lines(project, subject)
    assert measured["vfy"] == measured["coverage"] == [2]


def test_exit_patch_preserves_exit_status_when_saving_fails(tmp_path: Path) -> None:
    for tool in ("coverage", "vfy"):
        project = VfyProject(tmp_path / tool)
        project.write("subject.py", "def work():\n    return 42\n")
        project.write(
            "collect.py",
            "import os\n"
            "from pathlib import Path\n"
            f"from {tool} import Coverage\n"
            "from subject import work\n"
            "class BrokenSave(Coverage):\n"
            "    def save(self):\n"
            "        Path('save-attempted').write_text('yes')\n"
            "        raise RuntimeError('save failed')\n"
            "cov = BrokenSave(config_file=False, source=['subject'])\n"
            "cov.set_option('run:patch', ['_exit'])\n"
            "cov.start()\n"
            "assert work() == 42\n"
            "os._exit(3)\n",
        )
        result = project.run_python("collect.py")
        result.expect(3)
        assert (project.root / "save-attempted").read_text() == "yes"
        assert not result.stderr


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM hooks require POSIX")
def test_failed_signal_hook_leaves_exit_unpatched_before_successful_retry(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    subject = project.write("subject.py", "def work():\n    return 42\n")
    project.write(".coveragerc", "[run]\npatch = _exit\nsigterm = true\nsource = subject\n")
    project.write(
        "collect.py",
        "import os\n"
        "import signal\n"
        "import sys\n"
        "from vfy import Coverage\n"
        "from subject import work\n"
        "original_exit = os._exit\n"
        "original_signal = signal.signal\n"
        "def fail_signal(*args):\n"
        "    raise RuntimeError('signal registration failed')\n"
        "cov = Coverage()\n"
        "signal.signal = fail_signal\n"
        "try:\n"
        "    cov.start()\n"
        "except RuntimeError as error:\n"
        "    assert str(error) == 'signal registration failed'\n"
        "else:\n"
        "    raise AssertionError('signal registration did not fail')\n"
        "finally:\n"
        "    signal.signal = original_signal\n"
        "assert os._exit is original_exit\n"
        "assert sys.monitoring.get_tool(sys.monitoring.COVERAGE_ID) is None\n"
        "cov.start()\n"
        "assert work() == 42\n"
        "os._exit(3)\n",
    )
    result = project.run_python("collect.py")
    result.expect(3)
    assert _measured_lines(project, subject) == [2]


@pytest.mark.parametrize("patch", ["unknown", "fork", "execv"])
def test_api_rejects_unsupported_patch_before_acquisition_and_can_retry(
    tmp_path: Path, patch: str
) -> None:
    for tool in ("coverage", "vfy") if patch == "unknown" else ("vfy",):
        project = VfyProject(tmp_path / tool)
        subject = project.write("subject.py", "def work():\n    return 42\n")
        project.write(
            "collect.py",
            "import os\n"
            f"from {tool} import Coverage\n"
            "from subject import work\n"
            "original_exit = os._exit\n"
            "cov = Coverage(config_file=False, source=['subject'])\n"
            f"cov.set_option('run:patch', [{patch!r}])\n"
            "try:\n"
            "    cov.start()\n"
            "except Exception as error:\n"
            "    assert type(error).__name__ == 'ConfigError', error\n"
            f"    assert {patch!r} in str(error), error\n"
            "else:\n"
            "    raise AssertionError('unsupported patch was accepted')\n"
            "assert os._exit is original_exit\n"
            "assert Coverage.current() is None\n"
            "cov.set_option('run:patch', ['_exit'])\n"
            "cov.start()\n"
            "assert work() == 42\n"
            "os._exit(3)\n",
        )
        result = project.run_python("collect.py")
        result.expect(3)
        assert _measured_lines(project, subject) == [2]
