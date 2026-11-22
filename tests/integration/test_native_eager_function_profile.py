"""A profiler installed after the eager-item snapshot keeps stock collection behavior."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict

import pytest

from tests.support import VfyProject
from tests.support.native import run_parameter_suite

pytestmark: list[pytest.MarkDecorator] = [
    pytest.mark.skipif(
        sys.platform == "win32" or pytest.__version__ != "9.1.1",
        reason="eager Function batching requires Unix and pinned pytest 9.1.1",
    )
]


class _Observation(TypedDict):
    returncode: int
    collections: list[dict[str, object]]
    executed: list[str]


_CASES = """\
import gc
import json
import os
import sys
from pathlib import Path
from _pytest.mark.structures import NodeKeywords
from _pytest.python import Function

original_keywords_init = NodeKeywords.__init__
get_referents = gc.get_referents
armed = False
events = []

def changed_keywords_init(self, node):
    self.node = node
    self.parent = node.parent
    self._markers = {node.name: True, 'profile-constructor': True}

def profiler(frame, event, arg):
    if event == 'c_return' and arg is gc.isenabled:
        NodeKeywords.__init__.__code__ = changed_keywords_init.__code__
        sys.setprofile(None)
        events.append('profile-return')

def audit(event, args):
    global armed
    if armed and event == 'gc.get_referents' and original_keywords_init in args[0]:
        armed = False
        events.append('gc-checkpoint')
        sys.setprofile(profiler)

sys.addaudithook(audit)

def pytest_generate_tests(metafunc):
    global armed
    metafunc.parametrize('value', range(4))
    if metafunc.function.__name__ == 'test_control':
        return
    armed = True
    if os.environ['FINAL_PROOF_ROUTE'] == 'pytest':
        # Match the public audit and subsequent profiler callback checkpoints.
        get_referents(original_keywords_init)
        gc.isenabled()

def test_control(value):
    with Path('executed').open('a') as stream:
        stream.write(f'control:{value}\\n')

def test_victim(value, request):
    assert request.node.keywords['profile-constructor'] is True
    with Path('executed').open('a') as stream:
        stream.write(f'victim:{value}\\n')
"""

_OBSERVER = """\
import gc
import json
import os
import sys
from pathlib import Path

def pytest_collection():
    gc.disable()

def pytest_collection_finish(session):
    module = sys.modules['test_family']
    data = {
        'events': module.events,
        'profile_disabled': sys.getprofile() is None,
        'nodeids': [item.nodeid for item in session.items],
        'victim_keywords': [
            'profile-constructor' in item.keywords
            for item in session.items if item.originalname == 'test_victim'
        ],
    }
    Path(f'collection-{os.getpid()}.json').write_text(json.dumps(data))
    gc.enable()
"""


def test_eager_function_batch_rechecks_after_snapshot_profiler(tmp_path: Path) -> None:
    observations: dict[str, _Observation] = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\nfilterwarnings = error\n")
        project.write("conftest.py", _OBSERVER)
        project.write("test_family.py", _CASES)
        environment = {
            "VFY_WORKER_START": "exec",
            "FINAL_PROOF_ROUTE": route,
        }
        result = run_parameter_suite(project, route, env=environment)
        project.write("child-stdout.txt", result.stdout)
        project.write("child-stderr.txt", result.stderr)
        collections = [
            json.loads(path.read_text()) for path in sorted(project.root.glob("collection-*.json"))
        ]
        executed_path = project.root / "executed"
        observation: _Observation = {
            "returncode": result.returncode,
            "collections": collections,
            "executed": sorted(executed_path.read_text().splitlines())
            if executed_path.exists()
            else [],
        }
        project.write("oracle-observation.json", json.dumps(observation, indent=2) + "\n")
        observations[route] = observation
    stock = observations["pytest"]
    native = observations["native"]
    expected_nodeids = [
        f"test_family.py::test_{family}[{index}]"
        for family in ("control", "victim")
        for index in range(4)
    ]
    expected_execution = [
        f"{family}:{index}" for family in ("control", "victim") for index in range(4)
    ]
    assert len(stock["collections"]) == 1
    assert len(native["collections"]) == 2
    for observation in (stock, native):
        assert observation["executed"] == expected_execution, observations
        assert all(
            row["nodeids"] == expected_nodeids and row["victim_keywords"] == [True] * 4
            for row in observation["collections"]
        ), observations
        assert all(
            row["events"] == ["gc-checkpoint", "profile-return"] and row["profile_disabled"]
            for row in observation["collections"]
        )
    assert stock["returncode"] == native["returncode"] == 0, observations
    assert stock["executed"] == native["executed"], observations
    assert all(row["victim_keywords"] == [True] * 4 for row in native["collections"])
