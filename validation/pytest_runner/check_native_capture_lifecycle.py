"""Check native capture against pytest through VfyProject."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support import VfyProject

NOISY_CONFTST = """\
import resource


def pytest_configure(config):
    del config
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    limit = 128 * 1024
    if soft != resource.RLIM_INFINITY:
        limit = min(limit, soft)
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, hard))
"""

NOISY_TESTS = r"""\
import os
import subprocess
import sys

import pytest


PAYLOAD = b"x" * (32 * 1024)
background_children = []
release_fds = []


@pytest.fixture(scope="module", autouse=True)
def _stop_late_children():
    yield
    for child in background_children:
        if child.poll() is None:
            child.kill()
    for fd in release_fds:
        try:
            os.close(fd)
        except OSError:
            pass
    for child in background_children:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("case", range(12))
def test_session_output_spool_is_reused(case):
    os.write(1, PAYLOAD)
    os.write(2, PAYLOAD)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'child-out' + bytes([10])); "
            "os.write(2, b'child-err' + bytes([10]))",
        ]
    )
    assert child.wait() == 0, case


def test_capfd_reads_inherited_subprocess_output(capfd, request):
    with open("capture-engine.txt", "w", encoding="utf-8") as stream:
        stream.write(type(request).__module__)
    sys.stdout.write("buffered-out")
    sys.stderr.write("buffered-err")
    captured = capfd.readouterr()
    assert captured.out == "buffered-out"
    assert captured.err == "buffered-err"
    os.write(1, b"before-out\n")
    os.write(2, b"before-err\n")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'child-out' + bytes([10])); "
            "os.write(2, b'child-err' + bytes([10]))",
        ]
    )
    assert child.wait() == 0
    captured = capfd.readouterr()
    assert captured.out == "before-out\nchild-out\n"
    assert captured.err == "before-err\nchild-err\n"
    os.write(1, b"after-out\n")
    os.write(2, b"after-err\n")
    assert capfd.readouterr() == ("after-out\n", "after-err\n")


def test_phase_output_is_still_reported():
    print("native-spool-visible-out")
    print("native-spool-visible-err", file=sys.stderr)
    assert False, "controlled capture display failure"


def test_a_background_child_finishes_after_its_case_capture_boundary():
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    release_fds.append(release_write)
    child_source = '''import os, sys
ready_fd, release_fd = map(int, sys.argv[1:])
os.write(ready_fd, b'R')
os.close(ready_fd)
os.read(release_fd, 1)
os.close(release_fd)
os.write(1, b'background-child-out\\n')
os.write(2, b'background-child-err\\n')
'''
    try:
        child = subprocess.Popen(
            [sys.executable, "-c", child_source, str(ready_write), str(release_read)],
            pass_fds=(ready_write, release_read),
            stdout=None,
            stderr=None,
        )
        background_children.append(child)
    finally:
        os.close(ready_write)
        os.close(release_read)

    with os.fdopen(ready_read, "rb") as ready:
        assert ready.read(1) == b"R"


def test_b_background_child_output_is_retained_for_the_next_case():
    release_fd = release_fds.pop(0)
    with os.fdopen(release_fd, "wb", buffering=0) as release:
        release.write(b"R")
    assert background_children[0].wait(timeout=5) == 0
    assert False, "controlled background output display failure"
"""

DOCTEST_TRIGGER = """\
>>> print("doctest capture trigger")
doctest capture trigger
"""

DOCTEST_FOLLOWUP = """\
import os


def test_output_after_doctest_is_still_captured():
    print("doctest-followup-stdout")
    os.write(1, b"doctest-followup-fd\\n")
    assert False, "controlled post-doctest capture failure"
"""

INTERRUPTED_TEST = """\
import pytest


@pytest.fixture
def interrupted_resource():
    print("fixture acquisition before interruption")
    raise KeyboardInterrupt()


def test_interrupted_acquisition(interrupted_resource):
    del interrupted_resource
"""

REUSE_TEST = """\
from pathlib import Path


def test_capture_can_be_acquired_again(request):
    Path("reuse-engine.txt").write_text(type(request).__module__, encoding="utf-8")
    print("capture reuse output")
"""

SAME_PROCESS_REUSE = """\
import sys
import pytest

original_stdin = sys.stdin
options = ["-q", "--tb=no", "-p", "no:cacheprovider"]
if "--native" in sys.argv:
    options.extend(("-p", "vfy.asyncio_plugin", "-p", "vfy._pytest.native_pytest"))
first = pytest.main([*options, "test_interrupted.py"])
stdin_after_interruption = sys.stdin is original_stdin
second = pytest.main([*options, "test_reuse.py"])
stdin_after_reuse = sys.stdin is original_stdin
print(f"capture-state:{stdin_after_interruption}:{stdin_after_reuse}")
if (int(first), int(second), stdin_after_interruption, stdin_after_reuse) != (2, 0, True, True):
    raise SystemExit(10)
"""


def run_noisy_project(output: Path, name: str, python: Path) -> dict[str, object]:
    project = VfyProject(output / f"noisy-{name}")
    project.write("conftest.py", NOISY_CONFTST)
    project.write("test_capture.py", NOISY_TESTS)
    command = [
        str(python.absolute()),
        "-m",
        "pytest",
        "-q",
        "--tb=auto",
        "--show-capture=all",
        "-p",
        "no:cacheprovider",
    ]
    if name == "candidate":
        command.extend(("-p", "vfy.asyncio_plugin", "-p", "vfy._pytest.native_pytest"))
    command.append("test_capture.py")
    completed = project.run(*command)
    if completed.returncode != 1:
        raise RuntimeError(
            f"{name}: noisy capture run should have only the controlled failure (exit 1), "
            f"got {completed.returncode}\n{completed.stdout}\n{completed.stderr}"
        )
    for marker in (
        "controlled capture display failure",
        "native-spool-visible-out",
        "native-spool-visible-err",
        "background-child-out",
        "background-child-err",
    ):
        if marker not in completed.stdout + completed.stderr:
            raise RuntimeError(f"{name}: noisy capture output lost marker {marker!r}")
    summary = completed.stdout + completed.stderr
    if "14 passed" not in summary or "2 failed" not in summary:
        raise RuntimeError(
            f"{name}: large writes or capfd assertions failed under the file-size limit\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    engine = (project.root / "capture-engine.txt").read_text(encoding="utf-8")
    return {
        "returncode": completed.returncode,
        "engine": engine,
        "fixture_output_assertions_passed": True,
        "large_output_completed_under_file_limit": True,
        "post_doctest_output_is_in_its_call_capture": True,
    }


def run_doctest_followup_capture(output: Path, name: str, python: Path) -> dict[str, object]:
    project = VfyProject(output / f"doctest-{name}")
    project.write("test_a_capture_doctest.txt", DOCTEST_TRIGGER)
    project.write("test_z_capture_followup.py", DOCTEST_FOLLOWUP)
    command = [
        str(python.absolute()),
        "-m",
        "pytest",
        "-q",
        "--tb=auto",
        "--show-capture=all",
        "-p",
        "no:cacheprovider",
    ]
    if name == "candidate":
        command.extend(("-p", "vfy.asyncio_plugin", "-p", "vfy._pytest.native_pytest"))
    command.extend(("test_a_capture_doctest.txt", "test_z_capture_followup.py"))
    native_result = project.root / "native-result.json"
    completed = project.run(*command, env={"PYTEST_NATIVE_RESULT": str(native_result)})
    if completed.returncode != 1:
        raise RuntimeError(
            f"{name}: doctest follow-up should have the controlled failure (exit 1), "
            f"got {completed.returncode}\n{completed.stdout}\n{completed.stderr}"
        )
    output_text = completed.stdout + completed.stderr
    markers = ("doctest-followup-stdout", "doctest-followup-fd")
    if name == "candidate":
        raw = json.loads(native_result.read_text(encoding="utf-8"))
        if raw.get("engine") != "pytest-runner-native-v1":
            raise RuntimeError(f"{name}: follow-up run used a non-native engine")
        followup = next(
            case
            for case in raw["cases"]
            if case["nodeid"].endswith(
                "test_z_capture_followup.py::test_output_after_doctest_is_still_captured"
            )
        )
        call = next(phase for phase in followup["phases"] if phase["phase"] == "call")
        if any(marker not in call["stdout"] for marker in markers):
            raise RuntimeError(
                f"{name}: post-doctest output escaped its call capture phase\n"
                f"{json.dumps(call, indent=2)}\n{completed.stdout}\n{completed.stderr}"
            )
    else:
        captured = output_text.rsplit("Captured stdout call", 1)
        if len(captured) != 2 or any(marker not in captured[1] for marker in markers):
            raise RuntimeError(
                f"{name}: stock pytest did not report the post-doctest call capture\n"
                f"{completed.stdout}\n{completed.stderr}"
            )
    return {"returncode": completed.returncode, "post_doctest_output_is_in_call_capture": True}


def run_interrupted_reuse(output: Path, name: str, python: Path) -> dict[str, object]:
    project = VfyProject(output / f"reuse-{name}")
    project.write("test_interrupted.py", INTERRUPTED_TEST)
    project.write("test_reuse.py", REUSE_TEST)
    command = [str(python.absolute()), "-c", SAME_PROCESS_REUSE]
    if name == "candidate":
        command.append("--native")
    completed = project.run(*command)
    if completed.returncode != 0 or "capture-state:True:True" not in completed.stdout:
        raise RuntimeError(
            f"{name}: capture state was not restored after fixture interruption and reuse\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    engine = (project.root / "reuse-engine.txt").read_text(encoding="utf-8")
    return {
        "returncode": completed.returncode,
        "engine": engine,
        "stdin_restored_after_interruption_and_reuse": True,
        "reuse_output_visible": "capture-state:True:True" in completed.stdout,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-python", required=True, type=Path)
    parser.add_argument("--candidate-python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    observations: dict[str, dict[str, object]] = {}
    for name, python in (
        ("stock", args.stock_python),
        ("candidate", args.candidate_python),
    ):
        noisy = run_noisy_project(output, name, python)
        doctest_capture = run_doctest_followup_capture(output, name, python)
        reuse = run_interrupted_reuse(output, name, python)
        expected_engine = "vfy._native._pytest" if name == "candidate" else "_pytest.fixtures"
        if noisy["engine"] != expected_engine or reuse["engine"] != expected_engine:
            raise RuntimeError(
                f"{name}: run used unexpected request implementation: "
                f"{noisy['engine']}, {reuse['engine']}"
            )
        observations[name] = {
            "noisy_capture": noisy,
            "doctest_followup_capture": doctest_capture,
            "interrupted_reuse": reuse,
        }

    if (
        observations["stock"]["noisy_capture"]["returncode"]
        != observations["candidate"]["noisy_capture"]["returncode"]
    ):
        raise RuntimeError(f"noisy capture exit status differs: {observations}")

    report = output / "native-capture-lifecycle.json"
    report.write_text(json.dumps(observations, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(f"{report}\n")


if __name__ == "__main__":
    main()
