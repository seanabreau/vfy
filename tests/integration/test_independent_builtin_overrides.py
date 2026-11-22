"""Capture overrides retain the resolved builtin through every real worker phase."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("kind", ["capsys", "capfd"])
@pytest.mark.parametrize("workers", [1, 2])
def test_capture_override_preserves_builtin_identity_across_phases(
    tmp_path: Path, kind: str, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"""import pytest

@pytest.fixture
def {kind}({kind}):
    print('wrapper setup')
    yield {kind}
    print('wrapper teardown')
""",
    )
    source = f"""import json, os, sys
from pathlib import Path

def test_capture({kind}):
    print('call output')
    print('call error', file=sys.stderr)
    read = {kind}.readouterr()
    Path(__file__).with_suffix('.reads.json').write_text(json.dumps([read.out, read.err]))
    Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))
    assert read.out == 'wrapper setup\\ncall output\\n'
    assert read.err == 'call error\\n'
"""
    for name in ["test_first.py", "test_second.py"]:
        project.write(name, source)
    stock = project.run_python("-m", "pytest", "-q")
    stock.expect(0)
    expected = {path.name: json.loads(path.read_text()) for path in tmp_path.glob("*.reads.json")}
    assert len(expected) == 2
    for path in [*tmp_path.glob("*.reads.json"), *tmp_path.glob("*.pid")]:
        path.unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "--native-result-json=result.json",
    )
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    actual = {path.name: json.loads(path.read_text()) for path in tmp_path.glob("*.reads.json")}
    assert actual == expected
    result = json.loads((tmp_path / "result.json").read_text())
    topology = result["execution_topology"]
    pids = {int(path.read_text()) for path in tmp_path.glob("*.pid")}
    assert topology["requested_workers"] == topology["active_workers"] == workers
    assert set(topology["worker_pids"]) == pids
    assert len(pids) == workers
    assert result["counts"]["passed"] == 2
