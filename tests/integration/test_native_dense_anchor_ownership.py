"""Compare anchored callspec lifetimes and ordinary Python protocols publicly."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native workers require Unix")

_COMMON = """\
import gc
import json
import os
from pathlib import Path
from _pytest.python import CallSpec2

FIELDS = ['params', 'indices', '_arg2scope', '_idlist', 'marks']

def snapshot(spec):
    return [
        type(spec).__name__, spec.params['value'], sorted(spec.params),
        dict(spec.indices), {key: value.value for key, value in spec._arg2scope.items()},
        type(spec._idlist).__name__, list(spec._idlist),
        type(spec.marks).__name__, [mark.name for mark in spec.marks], spec.id,
    ]

def record(value):
    Path(f'observation-{os.getpid()}.json').write_text(json.dumps(value))

def pytest_collection():
    gc.disable()
"""

_CYCLE_CASES = """\
import os
import weakref
import pytest

retained = None
resurrected = None
references = None
events = []

def snapshot(spec):
    return [
        spec.params['value'], sorted(spec.params), dict(spec.indices),
        {key: value.value for key, value in spec._arg2scope.items()},
        type(spec._idlist).__name__, list(spec._idlist),
        type(spec.marks).__name__, [mark.name for mark in spec.marks], spec.id,
    ]

class Payload:
    def __init__(self, owner, mode):
        self.owner = owner
        self.reference = weakref.ref(owner)
        self.mode = mode

    def __del__(self):
        global resurrected
        try:
            # Record the interpreter's weakref ordering for the pytest comparison.
            events.append(['finalized', self.reference() is None, snapshot(self.owner)])
            if self.mode == 'resurrect':
                resurrected = self.owner
        except BaseException as error:
            events.append(['error', type(error).__name__, str(error)])

def pytest_generate_tests(metafunc):
    global retained, references
    metafunc.parametrize('value', range(3))
    retained = metafunc._calls[0]
    # Discard before an Item exists: pytest's terminal reporter deliberately
    # roots Items passed to pytest_deselected in its reporting statistics.
    victim = metafunc._calls.pop(1)
    params = victim.params
    payload = Payload(victim, os.environ['ANCHOR_PROOF_MODE'])
    params['loop'] = payload
    references = [weakref.ref(victim), weakref.ref(payload)]

def test_case(value):
    assert value in (0, 2)
    assert retained.params['value'] == 0
    if resurrected is not None:
        assert resurrected.params['value'] == 1
        assert resurrected.params['loop'].owner is resurrected
"""

_CYCLE_OBSERVER = """\
def pytest_collection_finish(session):
    try:
        module = session.items[0].module
        # The generation hook has returned and no Item ever owned the victim.
        gc.collect()
        references_cleared = [reference() is None for reference in module.references]
        assert len(module.events) == 1, module.events
        assert module.events[0][0] == 'finalized', module.events
        assert module.retained.params['value'] == 0
        revived = module.resurrected
        after = module.snapshot(revived) if revived is not None else None
        if os.environ['ANCHOR_PROOF_MODE'] == 'resurrect':
            assert revived is not None
            assert revived.params['loop'].owner is revived
        else:
            assert revived is None
            assert all(references_cleared), 'removed sibling remained rooted'
        record([
            module.events, after, snapshot(module.retained), len(session.items),
            references_cleared,
        ])
    finally:
        gc.enable()
"""

_IMMEDIATE_CASES = """\
import gc
import weakref

retained = None
events = []

class Opaque:
    def __del__(self):
        events.append('released')

def pytest_generate_tests(metafunc):
    global retained
    metafunc.parametrize('value', range(3))
    retained = metafunc._calls[0]
    victim = metafunc._calls.pop(1)
    payload = Opaque()
    payload_reference = weakref.ref(payload)
    callspec_reference = weakref.ref(victim)
    # The payload has no back-reference. A row handle owning this dictionary
    # would introduce a new cycle and delay its destructor until cyclic GC.
    object.__setattr__(victim, 'params', {'value': payload})
    del payload
    assert not gc.isenabled()
    del victim
    assert callspec_reference() is None
    assert payload_reference() is None, 'opaque value waited for cyclic GC'
    assert events == ['released'], events

def test_case(value):
    assert value in (0, 2)
    assert retained.params['value'] == 0
    assert events == ['released']
"""

_IMMEDIATE_OBSERVER = """\
def pytest_collection_finish(session):
    try:
        module = session.items[0].module
        assert not gc.isenabled()
        assert module.events == ['released']
        record([module.events, snapshot(module.retained), len(session.items)])
    finally:
        gc.enable()
"""

_PROTOCOL_CASES = """\
import pytest

@pytest.mark.parametrize('value', range(7))
def test_case(value):
    assert 0 <= value < 1000
"""

_PROTOCOL_OBSERVER = """\
import copy
import dataclasses
import pickle

def clean(spec, extra=()):
    mapping = vars(spec)
    assert type(mapping) is dict and mapping is spec.__dict__ and mapping is vars(spec)
    assert list(mapping) == FIELDS + list(extra), list(mapping)
    return mapping

def pytest_collection_finish(session):
    try:
        calls = [item.callspec for item in session.items]
        assert len(calls) == 7
        observations = []

        original = calls[0]
        raw = object.__getattribute__(original, '__dict__')
        object.__setattr__(original, 'extra', 'kept')
        promoted = clean(original, ('extra',))
        assert promoted is raw, 'vars replaced the original dictionary'
        promoted['params']['value'] = 199
        assert original.params is promoted['params'] and original.params['value'] == 199
        observations.append(['vars', snapshot(original), promoted['extra']])

        original = calls[1]
        object.__setattr__(original, 'extra', 'shallow')
        copied = copy.copy(original)
        assert type(copied) is CallSpec2 and copied is not original
        assert copied.params is original.params and copied.marks is original.marks
        assert clean(copied, ('extra',))['extra'] == 'shallow'
        observations.append(['copy', snapshot(copied)])

        original = calls[2]
        original.params['owner'] = original
        copied = copy.deepcopy(original)
        assert type(copied) is CallSpec2 and copied is not original
        assert copied.params is not original.params
        assert copied.params['owner'] is copied and original.params['owner'] is original
        clean(copied)
        observations.append(['deepcopy', snapshot(copied)])

        original = calls[3]
        original.params['owner'] = original
        copied = pickle.loads(pickle.dumps(original, protocol=pickle.HIGHEST_PROTOCOL))
        assert type(copied) is CallSpec2 and copied is not original
        assert copied.params['owner'] is copied and original.params['owner'] is original
        clean(copied)
        observations.append(['pickle', snapshot(copied)])

        original = calls[4]
        as_mapping = dataclasses.asdict(original)
        as_sequence = dataclasses.astuple(original)
        assert list(as_mapping) == FIELDS
        assert as_mapping['params'] == original.params
        assert as_mapping['params'] is not original.params
        assert type(as_sequence) is tuple and as_sequence[0] == original.params
        replacement = dataclasses.replace(original, params={'value': 211})
        assert type(replacement) is CallSpec2 and replacement.params == {'value': 211}
        clean(replacement)
        assert dataclasses.replace(original) == original
        observations.append([
            'dataclass', repr(original), [field.name for field in dataclasses.fields(original)],
            [type(value).__name__ for value in as_sequence], snapshot(replacement),
        ])

        original = calls[5]
        errors = []
        for operation in ('set', 'delete', 'hash'):
            try:
                if operation == 'set':
                    original.params = {}
                elif operation == 'delete':
                    del original.params
                else:
                    hash(original)
            except Exception as error:
                errors.append([operation, type(error).__name__, str(error)])
            else:
                raise AssertionError(f'{operation} unexpectedly succeeded')
        observations.append(['errors', errors, snapshot(original)])

        original = calls[6]
        params = original.params
        object.__setattr__(original, 'indices', {'value': 27})
        object.__setattr__(original, '_idlist', ['replaced'])
        assert original.params is params and original.indices == {'value': 27}
        assert original.id == 'replaced'
        observations.append(['replacement', snapshot(original)])
        record(observations)
    finally:
        gc.enable()
"""

_RAW_CASES = """\
import pytest
from _pytest.scope import Scope

def pytest_generate_tests(metafunc):
    metafunc.parametrize('value', range(3))
    raw = object.__getattribute__(metafunc._calls[0], '__dict__')
    raw['params'] = {'value': 17}
    raw['marks'] = [pytest.mark.raw_first.mark]
    raw['_idlist'] = ['raw-id']
    raw['_arg2scope'] = {'value': Scope.Module}

def test_case(value, request):
    assert value in (17, 1, 2)
    assert bool(request.node.get_closest_marker('raw_first')) == (value == 17)
    assert request.node.callspec.id == ('raw-id' if value == 17 else str(value))
"""

_RAW_OBSERVER = """\
def pytest_collection_finish(session):
    try:
        record([snapshot(item.callspec) for item in session.items])
    finally:
        gc.enable()
"""


def _junit(project: VfyProject) -> list[tuple[object, ...]]:
    return [
        (
            case.attrib["classname"],
            case.attrib["name"],
            tuple(
                (child.tag, tuple(sorted(child.attrib.items())), child.text or "") for child in case
            ),
        )
        for case in ET.parse(project.root / "result.xml").iter("testcase")
    ]


def _compare_public_pair(
    tmp_path: Path, cases: str, observer: str, *, mode: str = "ordinary"
) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\nmarkers =\n    raw_first: raw mapping marker\n")
        project.write("test_cases.py", cases)
        project.write("conftest.py", _COMMON + observer)
        environment = {
            "VFY_WORKER_START": "exec",
            "ANCHOR_PROOF_ROUTE": route,
            "ANCHOR_PROOF_MODE": mode,
            "VFY_NATIVE_SCHEDULING": "cases",
        }
        if route == "pytest":
            result = project.run_pytest(
                "-q", "--junitxml=result.xml", load_xdist=False, env=environment
            )
        else:
            result = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--no-replay",
                "--no-cov",
                "-q",
                "--junitxml=result.xml",
                env=environment,
            )
        result.expect(0)
        records = [json.loads(path.read_text()) for path in project.root.glob("observation-*.json")]
        assert len(records) == (2 if route == "native" else 1), records
        assert all(record == records[0] for record in records), records
        observations[route] = records[0], _junit(project)
    assert observations["native"] == observations["pytest"]


@pytest.mark.parametrize("mode", ["collect", "resurrect"])
def test_exposed_sibling_cycle_collects_and_finalizers_read_anchored_fields(
    tmp_path: Path, mode: str
) -> None:
    _compare_public_pair(tmp_path, _CYCLE_CASES, _CYCLE_OBSERVER, mode=mode)


def test_anchored_callspec_preserves_vars_copy_pickle_and_dataclasses(
    tmp_path: Path,
) -> None:
    _compare_public_pair(tmp_path, _PROTOCOL_CASES, _PROTOCOL_OBSERVER)


def test_arbitrary_replacement_is_destroyed_without_cyclic_gc(tmp_path: Path) -> None:
    _compare_public_pair(tmp_path, _IMMEDIATE_CASES, _IMMEDIATE_OBSERVER)


def test_raw_callspec_fields_override_dense_consumers_and_bulk_fallback(
    tmp_path: Path,
) -> None:
    _compare_public_pair(tmp_path, _RAW_CASES, _RAW_OBSERVER)
