"""Binary fixtures preserve raw reads and invocation-owned capture boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _write_binary_project(project: VfyProject, kind: str) -> None:
    project.write(
        "conftest.py",
        f"""import json, os, sys, pytest
from pathlib import Path
kind = {kind!r}
reads, reports, captures = [], [], []

def emit(data):
    sys.stdout.buffer.write(b'python:' + data)
    sys.stderr.buffer.write(b'error:' + data)
    if kind == 'capfdbinary':
        os.write(1, b'fd:' + data)
        os.write(2, b'fd-error:' + data)

def record(capture, label):
    value = capture.readouterr()
    assert isinstance(value, tuple)
    assert type(value).__name__ == 'CaptureResult'
    assert isinstance(value.out, bytes) and isinstance(value.err, bytes)
    assert value == (value.out, value.err)
    assert value._fields == ('out', 'err')
    reads.append([label, value.out.hex(), value.err.hex()])
    return value

@pytest.fixture(autouse=True)
def closed_read(request):
    def after_cleanup():
        value = record(captures.pop(), 'after cleanup')
        assert b'teardown pending' in value.out
    request.node.addfinalizer(after_cleanup)

@pytest.fixture
def stream(request):
    capture = request.getfixturevalue(kind)
    captures.append(capture)
    assert capture.request.fixturename == kind
    assert capture.request.scope == 'function'
    assert request.getfixturevalue(kind) is capture
    emit(b'setup\\xff\\x00\\xfe')
    record(capture, 'setup')
    emit(b'setup pending\\n')
    yield capture
    emit(b'teardown\\xff\\x00')
    record(capture, 'teardown')
    emit(b'teardown pending\\n')

def pytest_runtest_logreport(report):
    reports.append([report.nodeid, report.when, report.outcome,
                    report.sections, report.capstdout, report.capstderr])

def pytest_sessionfinish():
    Path('observations.json').write_text(json.dumps([reads, reports]))
""",
    )
    project.write(
        "test_bytes.py",
        """import gc, sys
from conftest import emit, record

def test_binary(stream):
    first = record(stream, 'call start')
    assert b'setup pending' in first.out
    assert record(stream, 'consumed') == (b'', b'')
    emit(b'\\xe2')
    emit(b'\\x82\\xac\\xff\\x00')
    print('text', end='')
    value = record(stream, 'arbitrary bytes')
    assert b'\\xff\\x00' in value.out and value.out.endswith(b'text')
    disabled = stream.disabled()
    emit(b'context not entered\\xff\\x00')
    assert b'context not entered\\xff\\x00' in record(stream, 'before disabled enter').out
    with disabled:
        print('disabled before')
        with stream.disabled():
            print('disabled nested', file=sys.stderr)
        print('disabled after')
    class Writer:
        def __del__(self):
            emit(b'destructor\\x00\\xff')
    value = Writer()
    del value
    gc.collect()
    assert b'destructor\\x00\\xff' in record(stream, 'destructor').out
    emit(b'call pending\\n')

def test_reuse(stream):
    assert b'setup pending' in record(stream, 'second call').out
    assert record(stream, 'second consumed') == (b'', b'')
""",
    )


@pytest.mark.parametrize("kind", ["capsysbinary", "capfdbinary"])
@pytest.mark.parametrize("capture", ["fd", "sys", "no", "tee-sys"])
def test_binary_reads_phases_disabled_and_destructors_match_pytest(
    tmp_path: Path, kind: str, capture: str
) -> None:
    project = VfyProject(tmp_path)
    _write_binary_project(project, kind)
    arguments = [f"--capture={capture}"]
    stock = project.run_pytest("-q", *arguments, load_xdist=False)
    stock.expect(0)
    observations = tmp_path / "observations.json"
    expected = json.loads(observations.read_text())
    observations.unlink()
    candidate = project.run_engine("native", *arguments)
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert json.loads(observations.read_text()) == expected
    assert "disabled before" in candidate.stdout
    assert "disabled after" in candidate.stdout
    assert "disabled nested" in candidate.stderr


def _write_conflict_project(project: VfyProject, first: str, second: str) -> None:
    project.write(
        "test_conflict.py",
        f"""import json, sys, pytest
from pathlib import Path

def test_conflict(request):
    capture = request.getfixturevalue({first!r})
    sys.stdout.buffer.write(b'before\\x00\\xff')
    with pytest.raises(pytest.FixtureLookupError) as error:
        request.getfixturevalue({second!r})
    assert 'cannot use {second} and {first} at the same time' in str(error.value)
    assert request.getfixturevalue({first!r}) is capture
    sys.stdout.buffer.write(b'after')
    result = capture.readouterr()
    assert result == (b'before\\x00\\xffafter', b'')
    Path('conflict.json').write_text(json.dumps([
        type(error.value).__name__, str(error.value.msg), result.out.hex(), result.err.hex()
    ]))

def test_after_failure(request):
    capture = request.getfixturevalue({second!r})
    print('recovered', end='')
    result = capture.readouterr()
    assert result.out in ('recovered', b'recovered') and not result.err
""",
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("capsysbinary", "capsys"),
        ("capsysbinary", "capfd"),
        ("capsysbinary", "capfdbinary"),
        ("capfdbinary", "capsys"),
        ("capfdbinary", "capfd"),
        ("capfdbinary", "capsysbinary"),
    ],
)
def test_binary_conflicting_fixture_failure_preserves_active_capture(
    tmp_path: Path, first: str, second: str
) -> None:
    project = VfyProject(tmp_path)
    _write_conflict_project(project, first, second)
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    observation = tmp_path / "conflict.json"
    expected = json.loads(observation.read_text())
    observation.unlink()
    candidate = project.run_engine("native")
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert json.loads(observation.read_text()) == expected


@pytest.mark.parametrize("kind", ["capsysbinary", "capfdbinary"])
@pytest.mark.parametrize("unblock", [False, True])
def test_binary_provider_requires_initialized_capture_plugin(
    tmp_path: Path, kind: str, unblock: bool
) -> None:
    project = VfyProject(tmp_path)
    if unblock:
        project.write(
            "conftest.py",
            """def pytest_configure(config):
    assert config.pluginmanager.unblock('capture')
    assert config.pluginmanager.getplugin('capture') is None
    assert config.pluginmanager.getplugin('capturemanager') is None
""",
        )
    project.write(
        "test_guard.py",
        f"""import pytest

def test_guard(request):
    with pytest.raises(pytest.FixtureLookupError):
        request.getfixturevalue({kind!r})
""",
    )
    arguments = ["-p", "no:capture"]
    stock = project.run_pytest("-q", *arguments, load_xdist=False)
    stock.expect(0)
    candidate = project.run_engine("native", *arguments)
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
