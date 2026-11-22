"""Explicit independent scheduling preserves ordered results and worker fixtures."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_SOURCE = """import json, os, pytest
from pathlib import Path

def note(event):
    with Path(f'events-{os.getpid()}.jsonl').open('a') as stream:
        stream.write(json.dumps(event) + '\\n')

@pytest.fixture(scope='session')
def resource():
    note(['setup'])
    yield
    note(['finalizer'])

@pytest.mark.parametrize('value', range(12))
def test_case(value, resource, request):
    note(['call', request.node.nodeid])
    if value == 1:
        pytest.skip('selected skip')
    assert value != 7
"""


def _outcomes(path: Path) -> list[tuple[str, str]]:
    return [
        (case.attrib["name"], next((child.tag for child in case), "passed"))
        for case in ET.parse(path).findall("./testsuite/testcase")
    ]


@requires_native_pytest
@pytest.mark.parametrize("scheduling", [None, "cases"])
def test_independent_case_scheduling_preserves_results_and_session_fixture_lifetimes(
    tmp_path: Path, scheduling: str | None
) -> None:
    project = VfyProject(tmp_path / "candidate")
    project.write("test_cases.py", _SOURCE)
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=4",
        *([f"--native-scheduling={scheduling}"] if scheduling else []),
        "--native-result-json=result.json",
        "--junitxml=result.xml",
        "-q",
        env={"VFY_NATIVE_SCHEDULING": "file" if scheduling else None},
    )
    result.expect(1)
    record = json.loads((project.root / "result.json").read_text())
    inventory = [f"test_cases.py::test_case[{value}]" for value in range(12)]
    assert [case["nodeid"] for case in record["cases"]] == inventory
    assert record["counts"]["passed"] == 10
    assert record["counts"]["failed"] == record["counts"]["skipped"] == 1
    workers = record["execution_topology"]["worker_pids"]
    assert record["execution_topology"]["requested_workers"] == 4
    # Demand and static case scheduling both split one file across workers.
    assert len(workers) == record["execution_topology"]["active_workers"] == 4
    expected_outcomes: list[tuple[str, str]] = []
    assignments: list[list[str]] = []
    for index, pid in enumerate(workers):
        events = (project.root / f"events-{pid}.jsonl").read_text()
        rows = [json.loads(line) for line in events.splitlines()]
        assignment = [row[1] for row in rows if row[0] == "call"]
        assignments.append(assignment)
        assert rows[0] == ["setup"]
        assert rows[-1] == ["finalizer"]
        reference = VfyProject(tmp_path / f"reference-{index}")
        reference.write("test_cases.py", _SOURCE)
        oracle = reference.run_python("-m", "pytest", *assignment, "--junitxml=result.xml", "-q")
        assert oracle.returncode in {0, 1}, oracle.stdout + oracle.stderr
        assert [path.read_text() for path in reference.root.glob("events-*.jsonl")] == [events]
        expected_outcomes.extend(_outcomes(reference.root / "result.xml"))
    assert sorted(nodeid for ids in assignments for nodeid in ids) == sorted(inventory)
    if scheduling == "cases":
        assert sorted(assignments, key=lambda ids: inventory.index(ids[0])) == [
            inventory[index : index + 3] for index in range(0, 12, 3)
        ]
    assert sorted(_outcomes(project.root / "result.xml")) == sorted(expected_outcomes)


@pytest.mark.parametrize(
    ("arguments", "environment"),
    [
        (("--native-scheduling=demand",), {}),
        ((), {"VFY_NATIVE_SCHEDULING": "demand"}),
    ],
)
def test_independent_demand_scheduling_collects_and_executes_the_inventory(
    tmp_path: Path, arguments: tuple[str, ...], environment: dict[str, str]
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_collected.py",
        "from pathlib import Path\nimport pytest\nPath('imported').touch()\n"
        "@pytest.mark.parametrize('value', range(8))\n"
        "def test_value(value):\n    Path(f'executed-{value}').mkdir()\n",
    )
    previous = project.write("result.xml", "previous result").read_bytes()
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--workers=4",
        "--no-cov",
        "--junitxml=result.xml",
        "--native-result-json=result.json",
        *arguments,
        env=environment,
    )
    result.expect(0)
    assert (tmp_path / "imported").exists()
    assert sorted(path.name for path in tmp_path.glob("executed-*")) == [
        f"executed-{value}" for value in range(8)
    ]
    assert (tmp_path / "result.xml").read_bytes() != previous
    assert len(_outcomes(tmp_path / "result.xml")) == 8
    record = json.loads((tmp_path / "result.json").read_text())
    assert [case["nodeid"] for case in record["cases"]] == [
        f"test_collected.py::test_value[{value}]" for value in range(8)
    ]
    assert record["execution_topology"]["active_workers"] == 4
