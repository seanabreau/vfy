"""Controller cancellation preserves results published before interpreter shutdown."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX FIFOs and signal exit statuses")
@pytest.mark.parametrize(
    "mode", ["completed", "external", "unpublished", "malformed", "foreign", "other-signal"]
)
@pytest.mark.parametrize("policy", ["cases", "demand"])
def test_completed_worker_interrupted_during_interpreter_shutdown(
    tmp_path: Path, mode: str, policy: str
) -> None:
    project = VfyProject(tmp_path, timeout=15)
    prefix_a = "    pass"
    prefix_b = "    pass"
    if policy == "demand":
        prefix_a = "    from conftest import seeded_prefix\n    seeded_prefix('a')"
        prefix_b = "    from conftest import seeded_prefix\n    seeded_prefix('b')"
    project.write(
        "conftest.py",
        "import pytest\nfrom pathlib import Path\n"
        "@pytest.fixture(autouse=True)\n"
        "def finalized(request):\n"
        "    yield\n"
        "    Path(request.node.nodeid + '.finalized').touch()\n"
        + (
            """
import os, time
def seeded_prefix(name):
    Path(name + '-prefix.pid').write_text(str(os.getpid()))
    deadline = time.monotonic() + 5
    while not all(Path(name + '-prefix.pid').is_file() for name in ('a', 'b')):
        assert time.monotonic() < deadline, 'both seeded workers must enter their prefix'
        time.sleep(0.01)
def pytest_collection_modifyitems(items):
    by_id = {item.nodeid: item for item in items}
    items[:] = [by_id[nodeid] for nodeid in (
        'test_a.py::test_prefix', 'test_b.py::test_prefix',
        'test_a.py::test_control', 'test_b.py::test_control',
        'test_a.py::test_never', 'test_b.py::test_never')]
"""
            if policy == "demand"
            else ""
        ),
    )
    project.write(
        "test_a.py",
        f"""import signal, pytest
from pathlib import Path

def test_prefix():
{prefix_a}

def test_control():
    with open('peer-ready') as ready:
        assert ready.read() == 'ready'
    if {mode!r} == 'external':
        signal.pause()
    pytest.exit('actual primary shutdown control', returncode=42)

def test_never():
    Path('primary-never').touch()
""",
    )
    project.write(
        "test_b.py",
        f"""import atexit, json, os, signal, pytest
from pathlib import Path

def await_interrupt():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    if {mode!r} == 'other-signal':
        signal.signal(signal.SIGINT, lambda *_: os.kill(os.getpid(), signal.SIGKILL))
    with open('peer-ready', 'w') as ready:
        ready.write('ready')
    if {mode!r} == 'external':
        os.kill(os.getpid(), signal.SIGINT)
    signal.pause()

def shutdown(index):
    directory = Path(os.environ['VFY_NATIVE_PARALLEL_DIR'])
    result = directory / ('result.' + index + '.json')
    completion = directory / ('collection-finish.' + index + '.json')
    assert result.is_file() and completion.is_file()
    if {mode!r} == 'malformed':
        result.write_text('invalid result JSON')
    elif {mode!r} == 'foreign':
        document = json.loads(result.read_text())
        document['independent']['nonce'] = 'another invocation'
        result.write_text(json.dumps(document))
    Path('peer-published').touch()
    await_interrupt()

def test_prefix():
{prefix_b}

def test_control():
    index = os.environ['VFY_NATIVE_PARALLEL_INDEX'] if {policy!r} == 'demand' else '1'
    Path('peer-index').write_text(index)
    if {mode!r} == 'unpublished':
        await_interrupt()
    atexit.register(shutdown, index)
    pytest.exit('actual peer shutdown control', returncode=42)

def test_never():
    Path('peer-never').touch()
""",
    )
    os.mkfifo(tmp_path / "peer-ready")

    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        f"--native-scheduling={policy}",
        "--native-result-json=result.json",
        env={"VFY_WORKER_START": "exec"},
    )

    # An owned stop preserves the primary control without inventing an unpublished result.
    expected_status = 42 if mode in {"completed", "unpublished"} else 3
    assert result.returncode == expected_status, result.stdout + result.stderr
    assert not (tmp_path / "primary-never").exists()
    assert not (tmp_path / "peer-never").exists()
    if mode != "unpublished":
        assert (tmp_path / "peer-published").exists()
        assert (tmp_path / "test_b.py::test_control.finalized").exists()
    if mode == "completed":
        record = json.loads((tmp_path / "result.json").read_text())
        assert record["exitstatus"] == 42
        assert [case["nodeid"] for case in record["cases"]] == [
            "test_a.py::test_prefix",
            "test_b.py::test_prefix",
        ]
        assert (tmp_path / "test_a.py::test_control.finalized").exists()
    else:
        assert not (tmp_path / "result.json").exists()
