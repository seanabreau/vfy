"""Compare direct native parameter generation with pytest through real workers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native workers require Unix")

_GENERATION = """\
from _pytest.python import CallSpec2
saved_calls = None
saved_vars = None
id_calls = 0
constructor_calls = 0

def pytest_generate_tests(metafunc):
    global saved_calls, saved_vars, constructor_calls
    name = metafunc.function.__name__
    if name == 'test_reordered':
        metafunc.parametrize('value', range(4))
        saved_calls = list(metafunc._calls)
        metafunc._calls.reverse()
    elif name == 'test_tuple':
        metafunc.parametrize(('left', 'right'), [(11, 12), (13, 14)])
    elif name == 'test_alias':
        metafunc.parametrize('value', range(3))
        saved_vars = vars(metafunc._calls[-1])
        saved_vars['params']['value'] = 99
        metafunc._calls.reverse()
    elif name == 'test_product':
        metafunc.parametrize('left', range(2))
        metafunc.parametrize('right', range(2))
    elif name == 'test_callback':
        def callback(value):
            global id_calls
            id_calls += 1
            return 'case' + str(value)
        metafunc.parametrize('value', [4, 5], ids=callback)
    elif name == 'test_constructor':
        original = CallSpec2.__init__
        def custom(self, *args, **kwargs):
            global constructor_calls
            constructor_calls += 1
            original(self, *args, **kwargs)
        CallSpec2.__init__ = custom
        try:
            metafunc.parametrize('value', range(2))
        finally:
            CallSpec2.__init__ = original

def test_reordered(value):
    assert value in range(4)

def test_tuple(left, right):
    assert (left, right) in ((11, 12), (13, 14))

def test_alias(value):
    assert value in (0, 1, 99)

def test_product(left, right):
    assert left in (0, 1) and right in (0, 1)

def test_callback(value):
    assert value in (4, 5)

def test_constructor(value):
    assert value in (0, 1)
"""

_OBSERVER = """\
import json
import os
from pathlib import Path
from _pytest.python import CallSpec2

def pytest_collection_finish(session):
    route = os.environ['DENSE_PROOF_ROUTE']
    module = session.items[0].module
    assert module.id_calls == 2
    assert module.constructor_calls == 3
    reordered = [item for item in session.items if item.originalname == 'test_reordered']
    assert [item.callspec.params['value'] for item in reordered] == [3, 2, 1, 0]
    assert [item.callspec.indices['value'] for item in reordered] == [0, 1, 2, 3]
    # Retained exact headers see native index recomputation in final _calls order.
    assert [spec.indices['value'] for spec in module.saved_calls] == [3, 2, 1, 0]
    aliases = [item for item in session.items if item.originalname == 'test_alias']
    assert vars(aliases[0].callspec) is module.saved_vars
    assert module.saved_vars['indices']['value'] == 0
    assert module.saved_vars['params']['value'] == 99
    assert all(type(item.callspec) is CallSpec2 for item in session.items)
    Path(f'proof-{os.getpid()}.json').write_text(json.dumps([
        module.id_calls, module.constructor_calls,
        [[item.nodeid, sorted(item.callspec.params.items()), sorted(item.callspec.indices.items())]
         for item in session.items],
    ]))
"""


def test_direct_generation_preserves_callbacks_aliases_products_and_order(tmp_path: Path) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", _OBSERVER)
        project.write("test_cases.py", _GENERATION)
        environment = {
            "VFY_WORKER_START": "exec",
            "DENSE_PROOF_ROUTE": route,
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
        assert "17 passed" in result.stdout
        proofs = [json.loads(path.read_text()) for path in project.root.glob("proof-*.json")]
        assert len(proofs) == (2 if route == "native" else 1), proofs
        assert all(proof == proofs[0] for proof in proofs)
        observations[route] = proofs[0]
    assert observations["native"] == observations["pytest"]
