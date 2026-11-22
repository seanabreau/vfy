"""Compare native progress at the installed vfy command boundary."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="native progress requires Unix",
)


@pytest.mark.parametrize("verbosity", [[], ["-q"], ["-v"], ["-vv"]])
@pytest.mark.parametrize("style", ["progress", "count", "classic", "times"])
def test_native_progress_matches_pytest(tmp_path: Path, verbosity: list[str], style: str) -> None:
    output = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", f"[pytest]\nconsole_output_style = {style}\n")
        project.write(
            "test_progress.py",
            """import pytest

def test_pass():
    pass

def test_failure():
    assert False, 'intentional failure'

@pytest.mark.skip(reason='[NOTRUN] Skipped: unavailable')
def test_skip():
    pass

@pytest.mark.xfail(reason='[NOTRUN] expected')
def test_xfail():
    assert False
""",
        )
        project.write("test_second.py", "def test_second_file():\n    pass\n")
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "--tb=no", *verbosity
        )
        result.expect(1)
        output[engine] = [
            re.sub(r" \d+(?:\.\d+)?(?:us|ms|s)$", " <duration>", line)
            for line in [re.sub(r" +", " ", line.strip()) for line in result.stdout.splitlines()]
            if line.startswith(("test_progress.py", "test_second.py", ".Fsx"))
        ]
    assert output["pytest"], output
    assert output["pytest-native"] == output["pytest"]


def test_native_progress_trims_long_skip_reason_at_single_verbosity(tmp_path: Path) -> None:
    output = {}
    reason = "long reason " + "segment " * 20
    source = f"import pytest\n@pytest.mark.skip(reason={reason!r})\ndef test_long_skip(): pass\n"
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_skip.py", source)
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "-v", "--tb=no"
        )
        result.expect(0)
        lines = [line.strip() for line in result.stdout.splitlines()]
        output[engine] = next(
            line for line in lines if line.startswith("test_skip.py::test_long_skip SKIPPED")
        )

    assert output["pytest-native"] == output["pytest"]
    assert "..." in output["pytest"]
    assert reason not in output["pytest"]


@pytest.mark.parametrize("engine", ["pytest", "pytest-native"])
def test_progress_is_visible_before_the_suite_finishes(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path / engine)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_live.py",
        """import time
from pathlib import Path

def test_first():
    pass

def test_wait_for_parent():
    deadline = time.monotonic() + 30
    while not Path('release').exists():
        assert time.monotonic() < deadline, 'parent never observed progress'
        time.sleep(0.01)
""",
    )
    process = project.start_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-v")
    try:
        process.wait_for("test_live.py::test_first PASSED", stream="stdout")
        assert process.poll() is None
    finally:
        project.write("release", "ready")
        result = process.wait(timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


_WORKER_SOURCES = {
    "test_alpha.py": "import pytest\n\ndef test_pass():\n    pass\n\n"
    "def test_failure():\n    assert False\n",
    "test_beta.py": "import pytest\n\n@pytest.mark.skip(reason='unavailable')\n"
    "def test_skip():\n    pass\n\n@pytest.mark.xfail(reason='expected')\n"
    "def test_xfail():\n    assert False\n",
}


@pytest.mark.parametrize(
    "start",
    [
        pytest.param(
            "fork",
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="collected fork requires Linux"
            ),
        ),
        "exec",
    ],
)
@pytest.mark.parametrize("verbosity", [[], ["-q"], ["-v"]])
def test_worker_progress_reports_every_case(
    tmp_path: Path, start: str, verbosity: list[str]
) -> None:
    progress = {}
    for workers in ("1", "2"):
        project = VfyProject(tmp_path / workers)
        for name, source in _WORKER_SOURCES.items():
            project.write(name, source)
        result = project.run_vfy(
            "test",
            "--engine=pytest-native",
            f"--workers={workers}",
            "--no-cov",
            "--tb=no",
            *verbosity,
            env={"VFY_WORKER_START": start} if workers == "2" else None,
        )
        result.expect(1)
        lines = [re.sub(r" +", " ", line.strip()) for line in result.stdout.splitlines()]
        if verbosity == ["-v"]:
            progress[workers] = sorted(
                re.sub(r" \[ *\d+%\]$", "", line)
                for line in lines
                if line.startswith(("test_alpha.py::", "test_beta.py::"))
            )
        else:
            # Workers interleave files, so their progress omits file prefixes.
            pattern = re.compile(r"(?:test_\w+\.py )?([.FEsxX]+)(?: \[ *\d+%\])?")
            progress[workers] = sorted(
                "".join(
                    match.group(1)
                    for line in lines
                    if (match := pattern.fullmatch(line)) is not None
                )
            )
    assert progress["2"] == progress["1"]
    assert progress["2"]


@pytest.mark.parametrize(
    "engine", ["stock", pytest.param("pytest-native", marks=requires_native_pytest), "native"]
)
def test_worker_progress_is_visible_before_workers_finish(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path / engine)
    project.write(
        "test_first.py",
        """import time
from pathlib import Path

def test_first():
    deadline = time.monotonic() + 30
    while not Path('slow_started').exists():
        assert time.monotonic() < deadline, 'peer did not start'
        time.sleep(0.01)
""",
    )
    project.write(
        "test_wait.py",
        """import time
from pathlib import Path

def test_wait_for_parent():
    Path('slow_started').touch()
    deadline = time.monotonic() + 30
    while not Path('release').exists():
        assert time.monotonic() < deadline, 'parent never observed progress'
        time.sleep(0.01)
""",
    )
    process = (
        project.start_python(
            "-B",
            "-m",
            "pytest",
            "-p",
            "xdist",
            "-n",
            "2",
            "--dist=loadfile",
            "-p",
            "no:cacheprovider",
            "-v",
            capture_partial_output=True,
        )
        if engine == "stock"
        else project.start_vfy(
            "test",
            f"--engine={engine}",
            "--workers=2",
            "--no-cov",
            "-v",
            capture_partial_output=True,
        )
    )
    try:
        marker = (
            "PASSED test_first.py::test_first"
            if engine == "stock"
            else "test_first.py::test_first PASSED"
        )
        process.wait_for(marker, stream="stdout")
        assert process.poll() is None
    finally:
        project.write("release", "ready")
        result = process.wait(timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


def test_default_worker_output_replay_starts_after_the_final_progress_line(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            f"def test_body():\n    print('worker output {name}', flush=True)\n",
        )

    result = project.run_vfy("test", "--engine=native", "--workers=2", "--no-cov", "-v", "-s")

    result.expect(0)
    lines = result.stdout.splitlines()
    progress = [
        index for index, line in enumerate(lines) if line.startswith(("test_a.py::", "test_b.py::"))
    ]
    assert len(progress) == 2, result.stdout
    for index in progress:
        assert re.fullmatch(r"test_[ab]\.py::test_body PASSED(?: +\[\s*\d+%\])?", lines[index])
    for name in ("a", "b"):
        assert lines.index(f"worker output {name}") > max(progress), result.stdout
