"""Terminal deselection totals consume the canonical selected-session record."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CASE = """import json, os
from pathlib import Path
import pytest

@pytest.mark.chosen
def test_keep():
    with Path('bodies.jsonl').open('a') as output:
        output.write(json.dumps({'file': Path(__file__).name, 'pid': os.getpid()}) + '\\n')

def test_omit():
    raise AssertionError('a deselected body executed')
"""


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize(
    "selector",
    [
        ("-k", "keep"),
        ("-m", "chosen"),
        ("--deselect=test_a.py::test_omit", "--deselect=test_b.py::test_omit"),
    ],
    ids=["keyword", "marker", "nodeids"],
)
def test_terminal_deselections_follow_canonical_record_in_serial_and_workers(
    tmp_path: Path, workers: int, selector: tuple[str, ...]
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nmarkers = chosen: selected body\n")
    project.write("test_a.py", _CASE)
    project.write("test_b.py", _CASE)
    stock = project.run_pytest("-q", *selector, load_xdist=False)
    stock.expect(0)
    assert "2 passed, 2 deselected" in stock.stdout
    (project.root / "bodies.jsonl").unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "--native-result-json=native.json",
        "-q",
        *selector,
    )
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert "2 passed, 2 deselected" in candidate.stdout
    record = json.loads((project.root / "native.json").read_text())
    assert record["deselected_nodeids"] == ["test_a.py::test_omit", "test_b.py::test_omit"]
    assert [case["nodeid"] for case in record["cases"]] == [
        "test_a.py::test_keep",
        "test_b.py::test_keep",
    ]
    assert record["counts"]["passed"] == 2
    topology = record["execution_topology"]
    assert topology["requested_workers"] == topology["active_workers"] == workers
    pids = topology["worker_pids"]
    assert len(pids) == len(set(pids)) == workers
    bodies = [json.loads(line) for line in (project.root / "bodies.jsonl").read_text().splitlines()]
    assert len(bodies) == 2
    assert {body["file"] for body in bodies} == {"test_a.py", "test_b.py"}
    assert {body["pid"] for body in bodies} == set(pids)
