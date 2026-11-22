"""Logging requests retain pytest's serial file and live-stream semantics."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")), reason="native workers require POSIX"
)


def _write_logging_project(project: VfyProject) -> None:
    project.write(
        "conftest.py",
        "import logging, os\n"
        "def pytest_configure(config):\n"
        "    os.cpu_count = lambda: 2\n"
        "    os.sched_getaffinity = lambda pid: {0, 1}\n"
        "def pytest_sessionfinish(session):\n"
        "    logging.warning('session-end')\n",
    )
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            "import logging, os\nfrom pathlib import Path\n"
            f"def test_{name}():\n"
            "    worker = os.getenv('VFY_NATIVE_PARALLEL_INDEX', 'serial')\n"
            f"    Path('executed-{name}').write_text(worker)\n"
            f"    logging.warning('call-{name}')\n",
        )


@pytest.mark.parametrize(
    ("workers", "mode", "source"),
    [
        ("1", "w", "argument"),
        ("auto", "w", "configuration"),
        ("logical", "a", "argument"),
        ("auto", "a", "configuration"),
    ],
)
def test_file_logging_worker_policy_preserves_pytest_write_and_append(
    tmp_path: Path, workers: str, mode: str, source: str
) -> None:
    supports_file_mode = tuple(map(int, pytest.version_tuple[:2])) >= (8, 1)
    if mode == "a" and not supports_file_mode:
        pytest.skip("pytest 8.1 introduces log_file_mode")
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        _write_logging_project(project)
        arguments = ["--log-file=pytest.log"] if source == "argument" else []
        if source == "argument" and supports_file_mode:
            arguments.append(f"--log-file-mode={mode}")
        configuration = "log_file = pytest.log\n" if source == "configuration" else ""
        if source == "configuration" and supports_file_mode:
            configuration += f"log_file_mode = {mode}\n"
        project.write("pytest.ini", "[pytest]\nlog_file_format = %(message)s\n" + configuration)
        log_path = project.write("pytest.log", "previous-run\n")
        if engine == "pytest":
            result = project.run_pytest("-q", *arguments, load_xdist=False)
        else:
            result = project.run_vfy(
                "test",
                "--engine=pytest-native",
                f"--workers={workers}",
                "--no-replay",
                "--no-cov",
                "-q",
                *arguments,
            )
            result.expect(0)
            for name in ("a", "b"):
                assert (project.root / f"executed-{name}").read_text() == "serial"
            if workers != "1":
                assert "native pytest uses one worker for file logging" in result.stderr
        result.expect(0)
        observed[engine] = log_path.read_text()
    expected = "call-a\ncall-b\nsession-end\n"
    if mode == "a":
        expected = "previous-run\n" + expected
    assert observed["pytest"] == expected
    assert observed["pytest-native"] == observed["pytest"]


@pytest.mark.parametrize(
    ("workers", "source"), [("auto", "argument"), ("logical", "configuration")]
)
def test_automatic_workers_preserve_live_logging_before_the_suite_finishes(
    tmp_path: Path, workers: str, source: str
) -> None:
    project = VfyProject(tmp_path)
    _write_logging_project(project)
    project.write(
        "test_a.py",
        "import logging, os, time\nfrom pathlib import Path\n"
        "def test_a():\n"
        "    Path('executed-a').write_text(os.getenv('VFY_NATIVE_PARALLEL_INDEX', 'serial'))\n"
        "    logging.warning('live-before-release')\n"
        "    deadline = time.monotonic() + 30\n"
        "    while not Path('release').exists():\n"
        "        assert time.monotonic() < deadline\n"
        "        time.sleep(0.01)\n",
    )
    project.write(
        "pytest.ini",
        "[pytest]\nlog_cli_format = LIVE %(message)s\n"
        + ("log_cli = true\nlog_cli_level = WARNING\n" if source == "configuration" else ""),
    )
    arguments = ["--log-cli-level=WARNING"] if source == "argument" else []
    command = project.start_vfy(
        "test",
        "--engine=pytest-native",
        f"--workers={workers}",
        "--no-replay",
        "--no-cov",
        "-q",
        *arguments,
    )
    try:
        command.wait_for("LIVE live-before-release", stream="stdout")
        assert command.poll() is None
    finally:
        project.write("release", "ready")
        result = command.wait(timeout=10)
    result.expect(0)
    assert "native pytest uses one worker for live logging" in result.stderr
    assert result.stdout.count("LIVE live-before-release") == 1
    assert "LIVE call-b" in result.stdout
    for name in ("a", "b"):
        assert (project.root / f"executed-{name}").read_text() == "serial"


@pytest.mark.parametrize("source", ["argument", "configuration"])
@pytest.mark.parametrize("logging_kind", ["file", "live"])
def test_explicit_workers_reject_logging_before_tests_or_log_replacement(
    tmp_path: Path, source: str, logging_kind: str
) -> None:
    project = VfyProject(tmp_path)
    _write_logging_project(project)
    settings = {
        "file": (["--log-file=pytest.log"], "log_file = pytest.log\n"),
        "live": (["--log-cli-level=WARNING"], "log_cli = true\n"),
    }
    arguments, configuration = settings[logging_kind]
    project.write("pytest.ini", "[pytest]\n" + (configuration if source == "configuration" else ""))
    previous_log = project.write("pytest.log", "previous-run\n")
    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "--no-cov",
        "-q",
        *(arguments if source == "argument" else []),
    )
    result.expect(4)
    assert f"native pytest requires --workers=1 with {logging_kind} logging" in result.stderr
    assert not list(project.root.glob("executed-*"))
    assert previous_log.read_text() == "previous-run\n"


def test_disabled_logging_plugin_keeps_explicit_workers_available(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            "import os\nfrom pathlib import Path\n"
            f"def test_{name}():\n"
            f"    Path('executed-{name}').write_text(os.environ['VFY_NATIVE_PARALLEL_INDEX'])\n",
        )
    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "--no-cov",
        "-q",
        "-p",
        "no:logging",
    )
    result.expect(0)
    assert {(project.root / f"executed-{name}").read_text() for name in ("a", "b")} == {"0", "1"}


@pytest.mark.parametrize("priority", ["", "@pytest.hookimpl(tryfirst=True)\n"])
def test_conftest_file_logging_rejection_preserves_existing_log(
    tmp_path: Path, priority: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        "import pytest\n"
        + priority
        + "def pytest_configure(config):\n    config.option.log_file = 'pytest.log'\n",
    )
    previous = project.write("pytest.log", "previous-run\n")
    for name in ("a", "b"):
        project.write(f"test_{name}.py", "def test_case():\n    raise AssertionError('ran test')\n")
    result = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=2", "--no-replay", "--no-cov", "-q"
    )
    result.expect(4)
    assert "native pytest requires --workers=1 with file logging" in result.stderr
    assert "ran test" not in result.stdout
    assert previous.read_text() == "previous-run\n"
