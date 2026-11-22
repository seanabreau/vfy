"""Sys capture preserves pytest's stream protocol and fixture precedence."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("fixture_name", ["capsys", "capsysbinary", "capfd", "capfdbinary"])
def test_sys_capture_streams_and_fixture_precedence_match_pytest(
    tmp_path: Path, fixture_name: str
) -> None:
    source = """import io
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

imported_stdout = sys.stdout
imported_stderr = sys.stderr

@pytest.fixture
def phases():
    print("sys-setup-sentinel")
    yield
    print("sys-teardown-sentinel", file=sys.stderr)

def test_streams(phases, request):
    assert sys.stdout is imported_stdout
    assert sys.stderr is imported_stderr
    assert not imported_stdout.closed
    assert sys.stdout.encoding.upper().replace("-", "") == "UTF8"
    assert not sys.stdout.isatty()
    with pytest.raises(io.UnsupportedOperation):
        sys.stdout.fileno()
    with pytest.raises(OSError):
        sys.stdin.read(1)
    print("sys-call-sentinel")
    sys.stderr.buffer.write("sys-bytes-sentinel\\n".encode())
    os.write(1, b"raw-fd-sentinel\\n")
    subprocess.run([sys.executable, "-c", "print('child-fd-sentinel')"], check=True)

def test_fixture(FIXTURE):
    print("fixture-text")
    sys.stderr.buffer.write(b"fixture-bytes\\n")
    captured = FIXTURE.readouterr()
    decode = lambda text: text.decode() if isinstance(text, bytes) else text
    observed = [decode(captured.out), decode(captured.err)]
    assert observed == ["fixture-text\\n", "fixture-bytes\\n"]
    with FIXTURE.disabled():
        print("disabled-sentinel", flush=True)
    print("fixture-after")
    assert decode(FIXTURE.readouterr().out) == "fixture-after\\n"
    Path("observed.json").write_text(json.dumps(observed))
""".replace("FIXTURE", fixture_name)
    observations = {}
    captures = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\naddopts = --capture=sys\n")
        project.write("test_capture.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "-rP",
            "--junitxml=result.xml",
            "-o",
            "junit_logging=out-err",
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        observations[engine] = json.loads((project.root / "observed.json").read_text())
        captures[engine] = [
            (case.attrib["name"], case.findtext("system-out"), case.findtext("system-err"))
            for case in ET.parse(project.root / "result.xml").iter("testcase")
        ]
        for _, stdout, _stderr in captures[engine]:
            assert "raw-fd-sentinel" not in (stdout or "")
            assert "child-fd-sentinel" not in (stdout or "")
            assert "disabled-sentinel" not in (stdout or "")
        for sentinel in (
            "sys-setup-sentinel",
            "sys-call-sentinel",
            "sys-bytes-sentinel",
            "sys-teardown-sentinel",
            "raw-fd-sentinel",
            "child-fd-sentinel",
            "disabled-sentinel",
        ):
            assert output.count(sentinel) == 1, output
        assert "fixture-text" not in output, output
        assert "fixture-after" not in output, output
    assert observations["pytest-native"] == observations["pytest"]
    assert captures["pytest-native"] == captures["pytest"]


def test_sys_capture_restores_streams_after_interrupt_and_reuse(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_interrupt.py", "def test_interrupt():\n    raise KeyboardInterrupt\n")
        project.write("test_pass.py", "def test_pass():\n    print('reused-capture')\n")
        project.write(
            "check.py",
            """import json
import sys
from pathlib import Path
import pytest

original = (sys.stdin, sys.stdout, sys.stderr)
arguments = ["--capture=sys", "-q"]
if sys.argv[1] == "pytest-native":
    arguments += ["-p", "no:cacheprovider", "-p", "vfy._pytest.native_pytest"]
first = pytest.main([*arguments, "test_interrupt.py"])
restored_first = all(a is b for a, b in zip(original, (sys.stdin, sys.stdout, sys.stderr)))
second = pytest.main([*arguments, "test_pass.py"])
restored_second = all(a is b for a, b in zip(original, (sys.stdin, sys.stdout, sys.stderr)))
observed = [int(first), int(second), restored_first, restored_second]
Path("observed.json").write_text(json.dumps(observed))
""",
        )
        result = project.run_python("check.py", engine, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
        result.expect(0)
        observations[engine] = json.loads((project.root / "observed.json").read_text())
    assert observations["pytest-native"] == observations["pytest"] == [2, 0, True, True]
