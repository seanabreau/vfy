"""Commands piped into a reader that exits early, such as ``| head -1``, end quietly."""

from __future__ import annotations

from pathlib import Path

import pytest
from coverage import CoverageData as ReferenceData

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

# The first test holds the run until the reader has closed the pipe, so the
# rest of the session's output meets a closed stdout.
_WAITING_TESTS = """\
import time
from pathlib import Path


def test_waits_for_the_reader():
    deadline = time.monotonic() + 60
    while not Path("stdout-closed").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_after_the_reader():
    pass
"""

# Collection output larger than a pipe buffer fails while it is still written.
_MANY_TESTS = """\
import pytest


@pytest.mark.parametrize("value", range(4000))
def test_value(value):
    pass
"""


@pytest.mark.parametrize(
    ("engine", "source", "arguments"),
    [
        pytest.param("native", _WAITING_TESTS, ("--no-cov",), id="run"),
        pytest.param(
            "native", _WAITING_TESTS, ("-v", "--cov=.", "--cov-report=term"), id="coverage"
        ),
        pytest.param(
            "native", _MANY_TESTS, ("--no-cov", "--collect-only", "-q"), id="collect-only"
        ),
        pytest.param(
            "pytest-native",
            _WAITING_TESTS,
            ("--no-cov",),
            id="pytest-native",
            marks=requires_native_pytest,
        ),
    ],
)
def test_test_command_ends_like_pytest_when_stdout_closes(
    tmp_path: Path, engine: str, source: str, arguments: tuple[str, ...]
) -> None:
    results = {}
    for route in ("pytest", "vfy"):
        project = VfyProject(tmp_path / route)
        project.write("test_closed.py", source)
        if route == "pytest":
            command = ["-m", "pytest", "-p", "no:cacheprovider", "-p", "pytest_cov.plugin"]
        else:
            # --quiet keeps vfy's own stderr notices out of the comparison.
            command = ["-m", "vfy", "--quiet", "test", f"--engine={engine}"]
        results[route] = project.run_closing_stdout(
            project.interpreter, *command, *arguments, env={"PYTHONUNBUFFERED": None}
        )

    reference, actual = results["pytest"], results["vfy"]
    assert (reference.returncode, reference.stderr) == (1, "")
    assert (actual.returncode, actual.stderr) == (1, "")
    assert actual.stdout.strip()


def test_coverage_report_ends_like_coverage_py_when_stdout_closes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    data = ReferenceData(str(tmp_path / ".coverage"))
    data.add_lines({str(project.write("module.py", "value = 1\n")): {1}})
    data.write()

    # vfy writes the report at once, so close the pipe before anything is written.
    # Coverage.py needs unbuffered output to observe the failure during its command.
    reference = project.run_closing_stdout(
        project.interpreter, "-u", "-m", "coverage", "report", lines=0
    )
    actual = project.run_closing_stdout(
        project.interpreter,
        "-m",
        "vfy",
        "coverage",
        "report",
        lines=0,
        env={"PYTHONUNBUFFERED": None},
    )

    # Coverage.py prints the BrokenPipeError traceback; vfy ends quietly.
    assert reference.returncode == 1
    assert "BrokenPipeError" in reference.stderr
    assert (actual.returncode, actual.stdout, actual.stderr) == (1, "", "")


@pytest.mark.parametrize("arguments", [("--help",), ("test", "--engine=native", "--version")])
def test_buffered_stdout_is_flushed_before_exit(tmp_path: Path, arguments: tuple[str, ...]) -> None:
    project = VfyProject(tmp_path)
    result = project.run_closing_stdout(
        project.interpreter,
        "-m",
        "vfy",
        "--quiet",
        *arguments,
        lines=0,
        env={"PYTHONUNBUFFERED": None},
    )
    assert (result.returncode, result.stdout, result.stderr) == (1, "", "")


def test_requested_exit_status_is_preserved_with_open_stdout(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_exit.py",
        "import pytest\n\ndef test_exits():\n    pytest.exit('requested exit', returncode=141)\n",
    )
    reference = project.run_pytest()
    actual = project.run_vfy("--quiet", "test", "--engine=native", "--no-cov")
    assert actual.returncode == reference.returncode == 141
