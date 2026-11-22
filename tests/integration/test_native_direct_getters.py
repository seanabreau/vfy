"""Preserve customized generation getters through the real pytest lifecycle."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native workers require Unix")

_CASES = """\
from _pytest.python import Metafunc
meta_events = []
descriptor_events = []
config_events = []

def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ == 'test_meta':
        metafunc.parametrize(('left', 'right'), [(11, 12), (13, 14)])
        original = Metafunc.__getattribute__
        def observed(self, name):
            if name in ('config', '_calls', '_params_directness'):
                meta_events.append(name)
            return original(self, name)
        Metafunc.__getattribute__ = observed
        try:
            metafunc._recompute_direct_params_indices()
        finally:
            Metafunc.__getattribute__ = original
        # A normal data descriptor must keep the original per-argument reads.
        def calls(self):
            descriptor_events.append('_calls')
            return object.__getattribute__(self, '__dict__')['_calls']
        Metafunc._calls = property(calls)
        try:
            metafunc._recompute_direct_params_indices()
        finally:
            del Metafunc._calls
        # The original helper never reads config and accepts its absence.
        config = metafunc.config
        del metafunc.config
        try:
            metafunc._recompute_direct_params_indices()
        finally:
            metafunc.config = config
    else:
        original = metafunc.config
        class ObservedConfig(type(original)):
            def __getattribute__(self, name):
                if name == 'stash':
                    config_events.append(name)
                return super().__getattribute__(name)
        view = object.__new__(ObservedConfig)
        view.__dict__.update(original.__dict__)
        metafunc.config = view
        try:
            metafunc.parametrize(('left', 'right'), [(21, 22), (23, 24)])
        finally:
            metafunc.config = original

def test_meta(left, right):
    assert (left, right) in ((11, 12), (13, 14))

def test_config(left, right):
    assert (left, right) in ((21, 22), (23, 24))
"""

_OBSERVER = """\
import json
import os
from pathlib import Path

def pytest_collection_finish(session):
    if os.environ['GETTER_PROOF_ROUTE'] == 'native':
        assert os.environ['VFY_NATIVE_PARALLEL_INDEX'] in ('0', '1')
    module = session.items[0].module
    assert module.meta_events == ['_params_directness', '_calls', '_calls']
    assert module.descriptor_events == ['_calls', '_calls']
    result = [module.meta_events, module.descriptor_events, module.config_events,
              [[item.nodeid, sorted(item.callspec.indices.items())] for item in session.items]]
    Path(f'getters-{os.getpid()}.json').write_text(json.dumps(result))
"""


def test_direct_admission_preserves_metafunc_getter_order(tmp_path: Path) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_cases.py", _CASES)
        project.write("conftest.py", _OBSERVER)
        environment = {
            "VFY_WORKER_START": "exec",
            "GETTER_PROOF_ROUTE": route,
            "VFY_NATIVE_SCHEDULING": "cases",
        }
        if route == "pytest":
            result = project.run_pytest("-q", load_xdist=False, env=environment)
        else:
            result = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--no-replay",
                "--no-cov",
                "-q",
                env=environment,
            )
        result.expect(0)
        assert "4 passed" in result.stdout
        proofs = [json.loads(path.read_text()) for path in project.root.glob("getters-*.json")]
        assert len(proofs) == (2 if route == "native" else 1), proofs
        assert all(proof == proofs[0] for proof in proofs)
        observations[route] = proofs[0]
    assert observations["native"] == observations["pytest"]
