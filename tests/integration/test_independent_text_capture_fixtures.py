"""Capture fixtures keep stock pytest's CaptureFixture type and text or bytes readouterr values."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

CONFTEST = """\
import json
from pathlib import Path

observations = []


def pytest_sessionfinish():
    Path("observations.json").write_text(json.dumps(observations))
"""

TEST_MODULE = """\
import os
import sys

import pytest

from conftest import observations


@pytest.mark.parametrize("name", ["capsys", "capfd", "capsysbinary", "capfdbinary"])
def test_capture_fixture(request, name):
    capture = request.getfixturevalue(name)
    assert isinstance(capture, pytest.CaptureFixture)
    print("text \\u00e9")
    if name.startswith("capfd"):
        os.write(1, b"raw \\xff\\n")
    sys.stderr.write("err\\n")
    value = capture.readouterr()
    observations.append(
        [name, type(value).__name__, repr(value.out), repr(value.err)]
    )
"""


@requires_native_pytest
@pytest.mark.parametrize("capture_mode", ["fd", "sys"])
def test_capture_fixtures_match_stock_types_and_readouterr(
    tmp_path: Path, capture_mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", CONFTEST)
    project.write("test_capture_fixture_types.py", TEST_MODULE)
    stock = project.run_pytest("-q", f"--capture={capture_mode}", load_xdist=False)
    stock.expect(0)
    observations = tmp_path / "observations.json"
    expected = json.loads(observations.read_text())
    observations.unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", f"--capture={capture_mode}")
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert json.loads(observations.read_text()) == expected
    assert [row[0] for row in expected] == ["capsys", "capfd", "capsysbinary", "capfdbinary"]


def _snapshot_pair(project: VfyProject) -> list[str]:
    stock = project.run_pytest("-q", "--capture=sys", load_xdist=False)
    stock.expect(0)
    observation = project.root / "snapshot.json"
    loaded: object = json.loads(observation.read_text())
    assert isinstance(loaded, list)
    expected: list[str] = []
    for value in loaded:
        assert isinstance(value, str)
        expected.append(value)
    observation.unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "--capture=sys")
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert json.loads(observation.read_text()) == expected
    return expected


@pytest.mark.skipif(os.name == "nt", reason="independent execution requires POSIX")
@pytest.mark.parametrize("kind", ["capsys", "capteesys", "capsysbinary"])
def test_reconfigured_capture_snapshots_match_stock_flush_boundaries(
    tmp_path: Path, kind: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_snapshot.py",
        f"""import json, sys
from pathlib import Path

def test_snapshot({kind}):
    capture = {kind}
    sys.stdout.reconfigure(write_through=False)
    sys.stderr.reconfigure(write_through=False)
    sys.stdout.write('out pending')
    sys.stderr.write('err pending')
    first = capture.readouterr()
    second = capture.readouterr()
    sys.stdout.reconfigure(write_through=True)
    sys.stderr.reconfigure(write_through=True)
    Path('snapshot.json').write_text(json.dumps([
        repr(first.out), repr(first.err), repr(second.out), repr(second.err),
    ]))
""",
    )

    expected = _snapshot_pair(project)

    assert expected == (
        ["b'out pending'", "b'err pending'", "b''", "b''"]
        if kind == "capsysbinary"
        else ["''", "''", "''", "''"]
    )


@pytest.mark.skipif(os.name == "nt", reason="independent execution requires POSIX")
def test_detached_capture_snapshot_raises_and_stream_can_be_restored(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_snapshot.py",
        """import io, json, sys
from pathlib import Path

def test_snapshot(capsys):
    stream = sys.stdout
    stream.write('before detach')
    buffer = stream.detach()
    try:
        try:
            stream.getvalue()
        except (ValueError, AssertionError) as error:
            observed = [type(error).__name__, str(error)]
        else:
            raise AssertionError('detached stream snapshot succeeded')
    finally:
        io.TextIOWrapper.__init__(stream, buffer, encoding='UTF-8', newline='', write_through=True)
    value = capsys.readouterr()
    Path('snapshot.json').write_text(json.dumps([*observed, value.out, value.err]))
""",
    )

    expected = _snapshot_pair(project)

    assert expected[0] in {"AssertionError", "ValueError"}
    assert expected[2:] == ["before detach", ""]


@pytest.mark.skipif(os.name == "nt", reason="independent execution requires POSIX")
@pytest.mark.parametrize("failure", ["stderr_decode", "stdout_truncate", "stderr_truncate"])
def test_capture_snapshot_failures_consume_channels_in_stock_order(
    tmp_path: Path, failure: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_snapshot.py",
        f"""import json, sys
from pathlib import Path

failure = {failure!r}
def test_snapshot(capsys):
    sys.stdout.write('out pending')
    if failure == 'stderr_truncate':
        sys.stderr.write('err pending')
    else:
        sys.stderr.buffer.write(bytes([255]))
    stream = sys.stdout if failure == 'stdout_truncate' else sys.stderr
    truncate = stream.truncate
    def broken_truncate(size=None):
        raise RuntimeError('truncate sentinel')
    if failure != 'stderr_decode':
        stream.truncate = broken_truncate
    try:
        try:
            capsys.readouterr()
        except (UnicodeDecodeError, RuntimeError) as error:
            observed = [type(error).__name__, str(error)]
        else:
            raise AssertionError('capture snapshot unexpectedly succeeded')
    finally:
        stream.truncate = truncate
        if failure != 'stderr_truncate':
            sys.stderr.seek(0)
            sys.stderr.truncate()
    remaining = capsys.readouterr()
    Path('snapshot.json').write_text(json.dumps([*observed, remaining.out, remaining.err]))
""",
    )

    expected = _snapshot_pair(project)

    assert expected[0] == ("UnicodeDecodeError" if failure == "stderr_decode" else "RuntimeError")
    assert (
        expected[2:]
        == {
            "stderr_decode": ["", ""],
            "stdout_truncate": ["out pending", ""],
            "stderr_truncate": ["", "err pending"],
        }[failure]
    )
