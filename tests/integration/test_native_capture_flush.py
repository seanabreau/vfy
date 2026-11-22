"""An already-active capture resume leaves custom stream buffers untouched."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("capture_mode", ["fd", "sys"])
@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_active_capture_resume_does_not_flush_custom_stream(
    tmp_path: Path, stream_name: str, capture_mode: str
) -> None:
    source = f"""import json
import sys
from pathlib import Path

def test_buffer(request):
    manager = request.config.pluginmanager.getplugin('capturemanager')
    original = sys.{stream_name}
    class Buffered:
        def __init__(self):
            self.pending = []
            self.flushes = 0
        def __getattr__(self, name):
            return getattr(original, name)
        def write(self, text):
            self.pending.append(text)
            return len(text)
        def flush(self):
            self.flushes += 1
            original.write(''.join(self.pending))
            self.pending.clear()
            original.flush()
    stream = Buffered()
    sys.{stream_name} = stream
    try:
        stream.write('buffered message')
        manager.resume_global_capture()
        manager.resume_global_capture()
        evidence = {{'pending': ''.join(stream.pending), 'flushes': stream.flushes}}
        stream.flush()
    finally:
        sys.{stream_name} = original
    captured = manager.read_global_capture()
    evidence['captured'] = [captured.out, captured.err]
    evidence['flushes_after'] = stream.flushes
    Path('evidence.json').write_text(json.dumps(evidence))
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_buffer.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            f"--capture={capture_mode}",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        assert "1 passed" in result.stdout
        observed[engine] = json.loads((project.root / "evidence.json").read_text())
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == {
            "pending": "buffered message",
            "flushes": 0,
            "captured": ["buffered message", ""]
            if stream_name == "stdout"
            else ["", "buffered message"],
            "flushes_after": 1,
        }
    )
