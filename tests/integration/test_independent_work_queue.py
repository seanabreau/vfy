"""Independent workers share pending cases and preserve their claimed lookahead."""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CASES = """import os, time, pytest
from pathlib import Path
def wait_for(name):
    deadline = time.monotonic() + 5
    while not Path(name).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert Path(name).exists(), 'another worker must reach ' + name
@pytest.mark.parametrize('case', range(64))
def test_case(case, resource):
    directory = Path('cases') / str(case)
    directory.mkdir()
    (directory / 'pid').write_text(str(os.getpid()))
    if case == 0:
        wait_for('ready16')
        assert Path('cases/16/pid').read_text() != str(os.getpid())
    if case == 1: Path('resumed1').touch()
    if case == 16:
        Path('ready16').touch()
        wait_for('resumed1')
"""

_RESOURCE = """import pytest
@pytest.fixture(scope='module')
def resource():
    yield
"""

_OBSERVERS = """import json, os, pytest
from pathlib import Path
events = []
@pytest.fixture(scope='module')
def resource():
    events.append(['fixture-setup'])
    yield
    events.append(['fixture-finish'])
def pytest_runtest_protocol(item, nextitem):
    events.append(['protocol', item.nodeid, None if nextitem is None else nextitem.nodeid])
def pytest_runtest_teardown(item, nextitem):
    events.append(['teardown', item.nodeid, None if nextitem is None else nextitem.nodeid])
def pytest_sessionfinish(session):
    Path(f'worker-{os.getpid()}.json').write_text(json.dumps(events))
"""


def _run(project: VfyProject, engine: str, *, observe: bool, worker_source: str = "cli") -> None:
    (project.root / "cases").mkdir(parents=True)
    source = _CASES
    if observe:
        # Hold the peer at 17 until the first worker claims across its reserved gap.
        source += "    if case == 17: wait_for('ready32')\n"
        source += "    if case == 32: Path('ready32').touch()\n"
    project.write("test_work.py", source)
    project.write("conftest.py", _OBSERVERS if observe else _RESOURCE)
    worker_arguments: tuple[str, ...] = ("--workers=2",)
    if worker_source == "config":
        project.write("pyproject.toml", "[tool.vfy.test]\nworkers = 2\n")
        worker_arguments = ()
    result = project.run_engine(
        engine,
        "-q",
        "-p",
        "no:cacheprovider",
        stock=("-p", "xdist.plugin", "-n", "2", "--dist=load"),
        independent=(*worker_arguments, "--native-result-json=result.json"),
    )
    result.expect(0)
    assert "64 passed" in result.stdout
    cases = sorted((project.root / "cases").iterdir(), key=lambda path: int(path.name))
    assert [int(path.name) for path in cases] == list(range(64))
    assert len({(path / "pid").read_text() for path in cases}) == 2
    if engine == "native":
        record = json.loads((project.root / "result.json").read_text())
        assert record["engine"] == "vfy-independent-v1"
        assert record["collected"] == 64
        assert [case["nodeid"] for case in record["cases"]] == [
            f"test_work.py::test_case[{index}]" for index in range(64)
        ]
        assert record["counts"]["passed"] == 64


@pytest.mark.parametrize("worker_source", ["cli", "config"])
def test_default_workers_reach_pending_cases_while_a_peer_waits(
    tmp_path: Path, worker_source: str
) -> None:
    for engine in ("stock", "native"):
        _run(VfyProject(tmp_path / engine), engine, observe=False, worker_source=worker_source)


def test_last_pending_cases_remain_available_while_a_peer_waits(tmp_path: Path) -> None:
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_tail.py",
            """import time, pytest
from pathlib import Path
@pytest.mark.parametrize('case', range(64))
def test_case(case):
    if case == 60:
        deadline = time.monotonic() + 5
        while not Path('later').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert Path('later').exists(), 'a peer must reach the remaining tail'
    if case > 60: Path('later').touch()
""",
        )
        result = project.run_engine(
            engine,
            "-q",
            stock=("-p", "xdist.plugin", "-n", "2", "--dist=load"),
            independent=("--workers=2", "--native-result-json=result.json"),
        )
        result.expect(0)
        assert "64 passed" in result.stdout
        if engine == "native":
            record = json.loads((project.root / "result.json").read_text())
            assert record["collected"] == record["counts"]["passed"] == 64
            assert [case["test_index"] for case in record["cases"]] == list(range(64))


def test_claimed_nextitem_keeps_module_fixtures_through_noncontiguous_work(
    tmp_path: Path,
) -> None:
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        _run(project, engine, observe=True)
        workers = []
        for path in project.root.glob("worker-*.json"):
            events = json.loads(path.read_text())
            protocol = [event[1:] for event in events if event[0] == "protocol"]
            if not protocol:
                continue
            workers.append(protocol)
            assert [event[1:] for event in events if event[0] == "teardown"] == protocol
            assert [entry[1] for entry in protocol] == [
                *[entry[0] for entry in protocol[1:]],
                None,
            ]
            assert events.count(["fixture-setup"]) == 1
            assert events.count(["fixture-finish"]) == 1
            assert events[-1] == ["fixture-finish"]
        assert len(workers) == 2
        assert any(
            int(after[0].split("[")[1][:-1]) != int(before[0].split("[")[1][:-1]) + 1
            for worker in workers
            for before, after in pairwise(worker)
        )
