"""Capture suspension and dynamic fixtures use real public runner boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("kind", ["capsys", "capfd"])
@pytest.mark.parametrize("workers", [1, 2])
def test_nested_capture_disabled_remains_suspended_until_outer_exit(
    tmp_path: Path, kind: str, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_capture.py",
        f"""import json, os, sys, pytest
from pathlib import Path

@pytest.fixture
def factory(request):
    def create():
        return request.getfixturevalue({kind!r})
    return create

def emit(label):
    print(label)
    print(label + ' error', file=sys.stderr)
    if {kind!r} == 'capfd':
        os.write(1, ('fd:' + label + '\\n').encode())
        os.write(2, ('fd:' + label + ' error\\n').encode())

def test_capture(factory, request):
    capture = factory()
    emit('before')
    with capture.disabled():
        emit('outer before')
        with capture.disabled():
            emit('inner')
        emit('outer after')
    emit('after')
    read = capture.readouterr()
    Path('reads-' + request.node.path.stem + '-' + str(os.getpid()) + '.json').write_text(
        json.dumps([read.out, read.err]))
    assert 'outer after' not in read.out + read.err
    assert 'before' in read.out and 'after' in read.out
    assert factory() is capture
""",
    )
    project.write("test_second.py", (tmp_path / "test_capture.py").read_text())
    stock = project.run_python("-m", "pytest", "-q")
    stock.expect(0)
    expected = _reads(tmp_path)
    project.write("stock-reads.json", json.dumps(expected))
    project.write("stock.stdout", stock.stdout)
    project.write("stock.stderr", stock.stderr)
    for path in tmp_path.glob("reads-*.json"):
        path.unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "--native-result-json=results.json",
    )
    project.write("candidate.stdout", candidate.stdout)
    project.write("candidate.stderr", candidate.stderr)
    actual = _reads(tmp_path)
    assert actual == expected
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert "outer after" in candidate.stdout
    assert "outer after error" in candidate.stderr
    if workers == 2:
        record = json.loads((tmp_path / "results.json").read_text())
        pids = {int(path.stem.rsplit("-", 1)[1]) for path in tmp_path.glob("reads-*.json")}
        assert pids == set(record["execution_topology"]["worker_pids"])
        assert len(pids) == 2


def _reads(root: Path) -> list[list[str]]:
    return [json.loads(path.read_text()) for path in sorted(root.glob("reads-*.json"))]
