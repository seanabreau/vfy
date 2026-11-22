"""Fresh serial mutation sessions do not inherit another pytest worker's identity."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject


@pytest.mark.skipif(sys.platform != "linux", reason="independent mutations require Linux")
def test_fresh_mutations_discard_inherited_xdist_worker_identity(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "app.py",
        "def decorate(function):\n"
        "    def wrapper():\n"
        "        return function()\n"
        "    return wrapper\n"
        "@decorate\n"
        "def value():\n"
        "    return 1\n",
    )
    project.write(
        "test_app.py",
        """import json
import os
from pathlib import Path
from app import value

def test_value(pytestconfig, worker_id):
    observed = value()
    with Path('sessions.jsonl').open('a') as output:
        output.write(json.dumps({
            'value': observed,
            'worker_id': worker_id,
            'workerinput': hasattr(pytestconfig, 'workerinput'),
            'environment': [os.environ.get(name) for name in (
                'PYTEST_XDIST_WORKER', 'PYTEST_XDIST_WORKER_COUNT', 'PYTEST_XDIST_TESTRUNUID'
            )],
        }) + '\\n')
    assert observed == 1
""",
    )
    completed = project.run_vfy(
        "mutate",
        "--engine=native",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--max-mutations=1",
        "--no-cache",
        "--format=json",
        "-p",
        "no:pytest-testmon",
        "-q",
        env={
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": None,
            "PYTEST_XDIST_WORKER": "gw7",
            "PYTEST_XDIST_WORKER_COUNT": "8",
            "PYTEST_XDIST_TESTRUNUID": "1234567890abcdef1234567890abcdef",
        },
    )
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}, report
    assert report["mutations"][0]["killer"] == "test_app.py::test_value", report
    observations = [
        json.loads(line) for line in (tmp_path / "sessions.jsonl").read_text().splitlines()
    ]
    assert {observation["value"] for observation in observations} == {1, 2}, observations
    assert all(
        observation["worker_id"] == "master"
        and observation["workerinput"] is False
        and observation["environment"] == [None] * 3
        for observation in observations
    ), observations
