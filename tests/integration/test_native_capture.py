"""Public capture lifecycle compatibility for the native pytest runner."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.skipif(sys.platform == "win32", reason="native test execution requires POSIX")
def test_imported_module_keeps_collection_capture_stream_open(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("sample.py", "value = 1\n")
    project.write(
        "test_stream.py",
        "import sys\n"
        "from sample import value\n"
        "imported_stderr = sys.stderr\n"
        "def test_stream():\n"
        "    assert not imported_stderr.closed\n"
        "    assert value == 1\n",
    )

    stock = project.run_pytest("-q", load_xdist=False)
    native = project.run_vfy(
        "test", "--engine=native", "--no-replay", "--cov=sample", "--cov-report=term", "-q"
    )

    stock.expect(0)
    native.expect(0)
    assert "1 passed" in native.stdout


@pytest.mark.skipif(
    os.name != "posix" or version("pytest") != "9.1.1",
    reason="native failure display requires Unix and pinned pytest 9.1.1",
)
@pytest.mark.parametrize("capture_mode", ["fd", "sys"])
@pytest.mark.parametrize("show_capture", ["all", "stdout", "stderr", "log", "no"])
def test_failure_traceback_and_capture_display_match_pytest(
    tmp_path: Path, show_capture: str, capture_mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_failure.py",
        """import logging
import sys


def test_failure():
    print("captured stdout sentinel")
    print("captured stderr sentinel", file=sys.stderr)
    logging.warning("captured log sentinel")
    assert False, "failure detail sentinel"
""",
    )
    traceback = """def test_failure():
        print("captured stdout sentinel")
        print("captured stderr sentinel", file=sys.stderr)
        logging.warning("captured log sentinel")
>       assert False, "failure detail sentinel"
E       AssertionError: failure detail sentinel
E       assert False

test_failure.py:9: AssertionError"""
    captured = {
        "stdout": "captured stdout sentinel\n",
        "stderr": "captured stderr sentinel\n",
        "log": "WARNING  root:test_failure.py:8 captured log sentinel\n",
    }
    record = project.root / "native-result.json"

    for engine in ("pytest", "pytest-native"):
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--color=no",
            "--tb=auto",
            f"--show-capture={show_capture}",
            f"--capture={capture_mode}",
            env={"PYTEST_NATIVE_RESULT": str(record)} if engine == "pytest-native" else None,
        )
        terminal = result.stdout + result.stderr
        assert result.returncode == 1, terminal
        assert traceback in terminal
        for stream, body in captured.items():
            visible = show_capture in {"all", stream}
            assert (f"Captured {stream}" in terminal) == visible, terminal
            assert (f"\n{body}" in terminal) == visible, terminal
            if visible:
                heading = f"Captured {stream} call"
                assert heading in terminal
                section = terminal.partition(heading)[2].partition("\n")[2]
                assert section.startswith(body), terminal

    # Display selection must not discard captured data from the native record.
    raw = json.loads(record.read_text())
    call = next(phase for phase in raw["cases"][0]["phases"] if phase["phase"] == "call")
    assert call["stdout"] == captured["stdout"]
    assert call["stderr"] == captured["stderr"]
    assert call["log"] + "\n" == captured["log"]


@pytest.mark.skipif(sys.platform == "win32", reason="native test execution requires POSIX")
@pytest.mark.parametrize("show_capture", ["all", "stdout", "stderr", "no"])
def test_failed_case_retains_setup_and_teardown_capture(tmp_path: Path, show_capture: str) -> None:
    source = """import sys
import pytest

@pytest.fixture
def phases():
    print('setup-stdout-sentinel')
    print('setup-stderr-sentinel', file=sys.stderr)
    yield
    print('teardown-stdout-sentinel')
    print('teardown-stderr-sentinel', file=sys.stderr)

def test_failure(phases):
    print('call-stdout-sentinel')
    print('call-stderr-sentinel', file=sys.stderr)
    assert False
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_capture.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            f"--show-capture={show_capture}",
        )
        output = result.stdout + result.stderr
        assert result.returncode == 1, output
        for phase in ("setup", "call", "teardown"):
            for stream in ("stdout", "stderr"):
                visible = show_capture in ("all", stream)
                assert output.count(f"\n{phase}-{stream}-sentinel\n") == int(visible), output


def _summary(output: str) -> str:
    summaries = re.findall(r"(?m)^\d+ .+ in [0-9.]+s$", output)
    assert summaries, output
    return re.sub(r" in [0-9.]+s$", "", summaries[-1])


def _passing_capture_summary(output: str) -> str:
    lines = output.splitlines()
    start = next((index for index, line in enumerate(lines) if "PASSES" in line), None)
    if start is None:
        return ""
    for index in range(start + 1, len(lines)):
        if re.fullmatch(
            r"=+ (?:slowest durations|short test summary info|ERRORS|FAILURES) =+",
            lines[index].strip(),
        ):
            return "\n".join(lines[start:index])
        if re.fullmatch(r"\d+ passed(?:, .+)? in [0-9.]+s", lines[index].strip()):
            return "\n".join(lines[start:index])
    raise AssertionError(output)


@pytest.mark.skipif(
    sys.platform != "darwin" or version("pytest") != "9.1.1",
    reason="the doctest capture regression exercises pytest's Darwin lifecycle on pytest 9.1.1",
)
def test_native_capture_resumes_after_doctest_suspends_global_capture(tmp_path: Path) -> None:
    doctest = """\
>>> print("doctest capture trigger")
doctest capture trigger
"""
    source = """\
import os


def test_output_after_doctest_is_still_captured():
    print("doctest-followup-stdout")
    os.write(1, b"doctest-followup-fd\\n")
    assert False, "controlled post-doctest capture failure"
"""
    projects = {engine: VfyProject(tmp_path / engine) for engine in ("pytest", "pytest-native")}
    for project in projects.values():
        project.write("test_a_capture_doctest.txt", doctest)
        project.write("test_z_capture_followup.py", source)

    results = {}
    result_path = projects["pytest-native"].root / "native-result.json"
    for engine, project in projects.items():
        arguments = (
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--show-capture=all",
            "test_a_capture_doctest.txt",
            "test_z_capture_followup.py",
        )
        if engine == "pytest-native":
            results[engine] = project.run_vfy(
                *arguments,
                env={"PYTEST_NATIVE_RESULT": str(result_path)},
            )
        else:
            results[engine] = project.run_vfy(*arguments)

    stock = results["pytest"]
    native = results["pytest-native"]
    assert stock.returncode == native.returncode == 1, (
        stock.stdout + stock.stderr + native.stdout + native.stderr
    )
    assert _summary(native.stdout) == _summary(stock.stdout)
    stock_capture = (stock.stdout + stock.stderr).rsplit("Captured stdout call", 1)
    assert len(stock_capture) == 2, stock.stdout + stock.stderr
    assert "doctest-followup-stdout" in stock_capture[1]
    assert "doctest-followup-fd" in stock_capture[1]

    raw = json.loads(result_path.read_text(encoding="utf-8"))
    case = next(
        case
        for case in raw["cases"]
        if case["nodeid"].endswith(
            "test_z_capture_followup.py::test_output_after_doctest_is_still_captured"
        )
    )
    call = next(phase for phase in case["phases"] if phase["phase"] == "call")
    assert "doctest-followup-stdout" in call["stdout"]
    assert "doctest-followup-fd" in call["stdout"]


@pytest.mark.skipif(
    os.name != "posix" or version("pytest") != "9.1.1",
    reason="the delayed descriptor capture regression requires POSIX and pytest 9.1.1",
)
def test_capfd_does_not_reuse_a_closed_fixture_for_late_child_output(tmp_path: Path) -> None:
    source = r'''import os
import subprocess
import sys

import pytest

children = []
release_fds = []


@pytest.fixture(scope="module", autouse=True)
def _stop_late_children():
    yield
    for fd in release_fds:
        try:
            os.close(fd)
        except OSError:
            pass
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_a_child_inherits_first_fixture_stdout(capfd):
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    release_fds.append(release_write)
    child_source = """import os, sys
ready_fd, release_fd = map(int, sys.argv[1:])
os.write(ready_fd, b'R')
os.close(ready_fd)
os.read(release_fd, 1)
os.close(release_fd)
os.write(1, b'late-first-fixture-output\\n')
"""
    try:
        child = subprocess.Popen(
            [sys.executable, "-c", child_source, str(ready_write), str(release_read)],
            pass_fds=(ready_write, release_read),
            stdout=None,
            stderr=subprocess.DEVNULL,
        )
        children.append(child)
    finally:
        os.close(ready_write)
        os.close(release_read)

    with os.fdopen(ready_read, "rb") as ready:
        assert ready.read(1) == b"R"


def test_b_late_output_stays_with_closed_fixture(capfd):
    release_fd = release_fds.pop(0)
    with os.fdopen(release_fd, "wb", buffering=0) as release:
        release.write(b"R")
    assert children[0].wait(timeout=5) == 0

    captured = capfd.readouterr()
    assert captured.out == "", repr(captured)
    assert captured.err == "", repr(captured)
'''
    projects = {engine: VfyProject(tmp_path / engine) for engine in ("pytest", "pytest-native")}
    for project in projects.values():
        project.write("test_capfd_lifecycle.py", source)

    results = {}
    for engine, project in projects.items():
        results[engine] = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--tb=no",
            "test_capfd_lifecycle.py",
        )

    stock = results["pytest"]
    native = results["pytest-native"]
    assert stock.returncode == native.returncode == 0, (
        stock.stdout + stock.stderr + native.stdout + native.stderr
    )
    assert _summary(stock.stdout) == _summary(native.stdout) == "2 passed"


@pytest.mark.skipif(
    os.name != "posix" or version("pytest") != "9.1.1",
    reason="native descriptor capture requires POSIX and pytest 9.1.1",
)
def test_global_capture_preserves_large_output_and_text_replacement(tmp_path: Path) -> None:
    source = r"""import os


def _write_all(fd, payload):
    remaining = memoryview(payload)
    while remaining:
        remaining = remaining[os.write(fd, remaining):]


def test_large_descriptor_output():
    payload = b"x" * 131071 + b"\xe2\x82\xac\xff" + b"z" * 65536
    _write_all(1, payload)
    _write_all(2, payload)
    assert False, "controlled capture failure"


def test_next_phase_starts_empty():
    print("next-phase-output")
"""
    projects = {engine: VfyProject(tmp_path / engine) for engine in ("pytest", "pytest-native")}
    for project in projects.values():
        project.write("test_large_capture.py", source)

    result_path = projects["pytest-native"].root / "native-result.json"
    results = {
        engine: project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--tb=auto",
            "-rP",
            "test_large_capture.py",
            env={"PYTEST_NATIVE_RESULT": str(result_path)} if engine == "pytest-native" else None,
        )
        for engine, project in projects.items()
    }

    payload = b"x" * 131071 + b"\xe2\x82\xac\xff" + b"z" * 65536
    expected = payload.decode("utf-8", errors="replace")
    for result in results.values():
        assert result.returncode == 1, result.stdout + result.stderr
        assert result.stdout.count(expected) == 2
        assert "next-phase-output" in result.stdout
    assert _summary(results["pytest"].stdout) == _summary(results["pytest-native"].stdout)

    record = json.loads(result_path.read_text(encoding="utf-8"))
    calls = [
        phase for case in record["cases"] for phase in case["phases"] if phase["phase"] == "call"
    ]
    assert calls[0]["stdout"] == calls[0]["stderr"] == expected
    assert calls[1]["stdout"] == "next-phase-output\n"
    assert calls[1]["stderr"] == ""


@pytest.mark.skipif(
    sys.platform == "win32" or version("pytest") != "9.1.1",
    reason="native passing capture summary follows the pinned Unix pytest reporter",
)
@pytest.mark.parametrize(
    ("report_args", "visible_phases"),
    [
        (["-rP", "--show-capture=all"], ("setup", "call", "teardown")),
        (
            ["-rP", "--show-capture=all", "--durations=0", "--durations-min=0"],
            ("setup", "call", "teardown"),
        ),
        (["-rP", "--show-capture=stdout"], ("stdout",)),
        (["-rP", "--show-capture=no"], ()),
        ([], ()),
    ],
)
def test_native_passing_capture_summary_matches_pytest(
    tmp_path: Path, report_args: list[str], visible_phases: tuple[str, ...]
) -> None:
    source = """import logging
import sys

import pytest

logger = logging.getLogger("passing-capture")

@pytest.fixture
def capture_phases():
    print("capture-setup-stdout")
    print("capture-setup-stderr", file=sys.stderr)
    logger.warning("capture-setup-log")
    yield
    print("capture-teardown-stdout")
    print("capture-teardown-stderr", file=sys.stderr)
    logger.warning("capture-teardown-log")

@pytest.mark.parametrize("case", ["one", "two"])
def test_passing_capture(capture_phases, case):
    print("capture-call-stdout")
    print("capture-call-stderr", file=sys.stderr)
    logger.warning("capture-call-log")

def test_silent_pass():
    pass
"""
    output = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_passing_capture.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            *report_args,
        )
        result.expect(0)
        output[engine] = result.stdout + result.stderr

    assert _summary(output["pytest-native"]) == _summary(output["pytest"]) == "3 passed"
    assert _passing_capture_summary(output["pytest-native"]) == _passing_capture_summary(
        output["pytest"]
    )
    if "--durations=0" in report_args:
        for engine in ("pytest", "pytest-native"):
            assert output[engine].count("slowest durations") == 1, output[engine]
    stock = output["pytest"]
    if "-rP" not in report_args:
        assert "PASSES" not in stock
        assert "capture-" not in stock
        return

    expected = {
        "setup": ("capture-setup-stdout", "capture-setup-stderr", "capture-setup-log"),
        "call": ("capture-call-stdout", "capture-call-stderr", "capture-call-log"),
        "teardown": (
            "capture-teardown-stdout",
            "capture-teardown-stderr",
            "capture-teardown-log",
        ),
    }
    visible = set(visible_phases)
    if visible_phases == ("stdout",):
        visible_tokens = {values[0] for values in expected.values()}
    else:
        visible_tokens = {
            token for phase, tokens in expected.items() if phase in visible for token in tokens
        }
    for phase, tokens in expected.items():
        for token in tokens:
            assert stock.count(token) == (2 if token in visible_tokens else 0), stock
        if phase in visible:
            assert f"Captured stdout {phase}" in stock
            if visible_phases != ("stdout",):
                assert f"Captured stderr {phase}" in stock
                assert f"Captured log {phase}" in stock


@pytest.mark.skipif(
    sys.platform == "win32" or version("pytest") != "9.1.1",
    reason="native passing capture summary follows the pinned Unix pytest reporter",
)
@pytest.mark.parametrize(
    ("source", "teardown_token"),
    [
        ("def test_silent_pass():\n    pass\n", None),
        (
            "import pytest\n\n"
            "@pytest.fixture\n"
            "def teardown_output():\n"
            "    yield\n"
            "    print('teardown-only-output')\n\n"
            "def test_teardown_only(teardown_output):\n"
            "    pass\n",
            "teardown-only-output",
        ),
    ],
    ids=["silent-only", "teardown-only"],
)
def test_native_passing_capture_summary_handles_empty_call_sections(
    tmp_path: Path, source: str, teardown_token: str | None
) -> None:
    output = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_passing_capture.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "-rP",
            "--show-capture=all",
        )
        result.expect(0)
        output[engine] = result.stdout + result.stderr

    assert _summary(output["pytest-native"]) == _summary(output["pytest"]) == "1 passed"
    native_summary = _passing_capture_summary(output["pytest-native"])
    assert native_summary == _passing_capture_summary(output["pytest"])
    assert "PASSES" in native_summary
    if teardown_token is None:
        assert "test_silent_pass" not in native_summary
        assert "Captured " not in native_summary
    else:
        assert teardown_token in native_summary
        assert "Captured stdout teardown" in native_summary
        assert "test_teardown_only" not in native_summary


@pytest.mark.skipif(
    sys.platform == "win32" or version("pytest") != "9.1.1",
    reason="native passing capture summary follows the pinned Unix pytest reporter",
)
def test_native_passing_summary_keeps_call_pass_when_teardown_fails(tmp_path: Path) -> None:
    source = """import pytest

@pytest.fixture
def failing_teardown():
    yield
    print("teardown-error-output")
    raise RuntimeError("controlled teardown failure")

def test_call_passes(failing_teardown):
    print("call-passed-output")
"""
    output = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_passing_capture.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "-rP",
            "--show-capture=all",
        )
        result.expect(1)
        output[engine] = result.stdout + result.stderr

    native_summary = _passing_capture_summary(output["pytest-native"])
    assert native_summary == _passing_capture_summary(output["pytest"])
    assert _summary(output["pytest-native"]) == _summary(output["pytest"]) == "1 passed, 1 error"
    assert "call-passed-output" in native_summary
    assert "teardown-error-output" not in native_summary


@pytest.mark.skipif(
    os.name != "posix" or version("pytest") != "9.1.1",
    reason="selected native stream identity follows pytest 9.1.1 fd capture",
)
def test_native_selected_run_preserves_collection_capture_stream_identity(tmp_path: Path) -> None:
    source = r"""import os
import sys

original_stdout = sys.stdout
original_stderr = sys.stderr


def test_stream_identity():
    assert sys.stdout is original_stdout
    assert sys.stderr is original_stderr
    print("python-stdout-sentinel")
    print("python-stderr-sentinel", file=sys.stderr)
    os.write(1, b"fd-stdout-sentinel\n")
    os.write(2, b"fd-stderr-sentinel\n")
"""
    output = {}
    result_path = tmp_path / "pytest-native" / "native-result.json"
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_stream_identity.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "-rP",
            "--show-capture=all",
            "-k",
            "stream_identity",
            env={"PYTEST_NATIVE_RESULT": str(result_path)} if engine == "pytest-native" else None,
        )
        result.expect(0)
        output[engine] = result.stdout + result.stderr

    expected_stdout = "python-stdout-sentinel\nfd-stdout-sentinel\n"
    expected_stderr = "python-stderr-sentinel\nfd-stderr-sentinel\n"
    for terminal in output.values():
        assert _summary(terminal) == "1 passed"
        capture = _passing_capture_summary(terminal)
        assert expected_stdout in capture, terminal
        assert expected_stderr.rstrip("\n") in capture, terminal

    record = json.loads(result_path.read_text(encoding="utf-8"))
    call = next(phase for phase in record["cases"][0]["phases"] if phase["phase"] == "call")
    assert call["stdout"] == expected_stdout
    assert call["stderr"] == expected_stderr


@pytest.mark.skipif(
    os.name != "posix" or version("pytest") != "9.1.1",
    reason="capture bridge rollback follows pytest 9.1.1 fd capture",
)
def test_native_stream_bridge_rolls_back_partial_fd_redirect(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    state_path = tmp_path / "bridge-state.json"
    project.write(
        "test_bridge_setup_failure.py",
        r"""import json
import os
import sys
from pathlib import Path

original_stdout = sys.stdout
original_stderr = sys.stderr
captured_stdout = sys.stdout
captured_stderr = sys.stderr
real_dup2 = os.dup2
dup2_calls = 0
bridge_baseline_stdout = None
bridge_baseline_stderr = None


def fail_second_bridge_redirect(source, destination, *args, **kwargs):
    global bridge_baseline_stderr, bridge_baseline_stdout, dup2_calls
    dup2_calls += 1
    if dup2_calls == 3:
        bridge_baseline_stdout = sys.stdout
        bridge_baseline_stderr = sys.stderr
    if dup2_calls == 4:
        raise OSError("injected stream bridge redirect failure")
    result = real_dup2(source, destination, *args, **kwargs)
    if dup2_calls == 6:
        state = {
            "stdout_identity": sys.stdout is bridge_baseline_stdout,
            "stderr_identity": sys.stderr is bridge_baseline_stderr,
            "stdout_fd_open": _fd_is_open(captured_stdout),
            "stderr_fd_open": _fd_is_open(captured_stderr),
        }
        Path(os.environ["VFY_BRIDGE_STATE"]).write_text(json.dumps(state))
    return result


def _fd_is_open(stream):
    try:
        os.fstat(stream.fileno())
    except OSError:
        return False
    return True


os.dup2 = fail_second_bridge_redirect


def test_bridge_setup_is_interrupted_before_test_execution():
    raise AssertionError("native execution should stop during bridge setup")
""",
    )
    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=1",
        "--no-cov",
        "-q",
        env={"VFY_BRIDGE_STATE": str(state_path)},
    )
    terminal = result.stdout + result.stderr
    assert result.returncode != 0, terminal
    assert "injected stream bridge redirect failure" in terminal
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state == {
        "stdout_identity": True,
        "stderr_identity": True,
        "stdout_fd_open": True,
        "stderr_fd_open": True,
    }


@pytest.mark.skipif(sys.platform == "win32", reason="native test execution requires POSIX")
@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
@pytest.mark.parametrize("transition", ["suspend", "resume", "close"])
def test_capture_stream_destructor_can_reenter_capture(
    tmp_path: Path, stream_name: str, transition: str
) -> None:
    source = f"""import io
import sys
from pathlib import Path

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

def test_reentrant_capture(request):
    manager = request.config.pluginmanager.getplugin('capturemanager')
    class Stream(io.StringIO):
        def __del__(self):
            record('destructor-entered')
            manager.read_global_capture()
            manager.suspend_global_capture()
            record('destructor-finished')
    if {transition!r} == 'resume':
        manager.suspend_global_capture()
    sys.{stream_name} = Stream()
    if {transition!r} == 'suspend':
        manager.suspend_global_capture()
        manager.resume_global_capture()
    elif {transition!r} == 'resume':
        manager.resume_global_capture()
"""
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine, timeout=10)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_capture.py", source)
        try:
            result = project.run_vfy(
                "test",
                f"--engine={engine}",
                "--workers=1",
                "--no-cov",
                "-q",
                env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            )
        except RuntimeError as error:
            if not isinstance(error.__cause__, subprocess.TimeoutExpired):
                raise
            events_path = project.root / "events.txt"
            events = events_path.read_text() if events_path.exists() else ""
            pytest.fail(f"{engine} capture did not finish; events={events!r}: {error}")
        result.expect(0)
        observations[engine] = (project.root / "events.txt").read_text()
    assert (
        observations["pytest-native"]
        == observations["pytest"]
        == ("destructor-entered\ndestructor-finished\n")
    )
