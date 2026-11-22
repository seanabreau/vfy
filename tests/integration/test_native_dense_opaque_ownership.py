"""Compare direct-produced opaque callspec ownership against stock pytest."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.integration.test_native_dense_anchor_ownership import _compare_public_pair

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native workers require Unix")

_CASES_COMMON = """\
import gc
import os
import weakref

def snapshot(spec):
    value = spec.params['value']
    return [
        type(spec).__name__, value.label if hasattr(value, 'label') else value,
        sorted(spec.params), dict(spec.indices),
        {key: value.value for key, value in spec._arg2scope.items()},
        type(spec._idlist).__name__, list(spec._idlist),
        type(spec.marks).__name__, [mark.name for mark in spec.marks], spec.id,
    ]
"""

_CYCLE_CASES = (
    _CASES_COMMON
    + """\
retained = None
resurrected = None
references = None
events = []

class Payload:
    def __init__(self, label):
        self.label = label
        self.owner = None
        self.reference = None

    def __del__(self):
        global resurrected
        if self.owner is None:
            return
        try:
            events.append(['finalized', self.reference() is None, snapshot(self.owner)])
            if os.environ['ANCHOR_PROOF_MODE'] == 'resurrect':
                resurrected = self.owner
        except BaseException as error:
            events.append(['error', type(error).__name__, str(error)])

def pytest_generate_tests(metafunc):
    global retained, references
    rows = [(Payload(label),) for label in ('keep', 'victim', 'run')]
    metafunc.parametrize(('value',), rows)
    retained = metafunc._calls[0]
    # Discard before Item construction, so deselection reporting cannot root it.
    victim = metafunc._calls.pop(1)
    payload = rows[1][0]
    payload.owner = victim
    payload.reference = weakref.ref(victim)
    references = [weakref.ref(victim), weakref.ref(payload)]

def test_case(value):
    assert value.label in ('keep', 'run')
    assert retained.params['value'].label == 'keep'
    if resurrected is not None:
        assert resurrected.params['value'].owner is resurrected
"""
)

_CYCLE_OBSERVER = """\
def pytest_collection_finish(session):
    try:
        if not session.items:
            return
        module = session.items[0].module
        gc.collect()
        references_cleared = [reference() is None for reference in module.references]
        assert len(module.events) == 1, module.events
        assert module.events[0][0] == 'finalized', module.events
        revived = module.resurrected
        if os.environ['ANCHOR_PROOF_MODE'] == 'resurrect':
            assert revived is not None
            assert revived.params['value'].owner is revived
        else:
            assert revived is None
            assert all(references_cleared), 'retained sibling rooted the discarded opaque cycle'
        record([
            module.events, module.snapshot(revived) if revived is not None else None,
            module.snapshot(module.retained), len(session.items), references_cleared,
        ])
    finally:
        gc.enable()
"""

_OVERWRITE_CASES = (
    _CASES_COMMON
    + """\
retained = None
current = None
events = []

class Payload:
    def __init__(self, label):
        self.label = label

    def __del__(self):
        if self.label != 'old':
            return
        try:
            events.append(['released', snapshot(current)])
        except BaseException as error:
            events.append(['error', type(error).__name__, str(error)])

def pytest_generate_tests(metafunc):
    global retained, current
    rows = [(Payload(label),) for label in ('keep', 'old', 'run')]
    reference = weakref.ref(rows[1][0])
    metafunc.parametrize(('value',), rows)
    retained = metafunc._calls[0]
    victim = metafunc._calls.pop(1)
    # ParameterSet inputs must not retain the old payload across this setter.
    del rows
    current = victim
    assert not gc.isenabled()
    object.__setattr__(victim, 'params', {'value': 'replacement'})
    assert reference() is None, 'field replacement delayed the old destructor'
    assert len(events) == 1 and events[0][0] == 'released', events
    assert events[0][1][1] == 'replacement', events
    current = None
    callspec_reference = weakref.ref(victim)
    del victim
    assert callspec_reference() is None, 'opaque row introduced a header cycle'

def test_case(value):
    assert value.label in ('keep', 'run')
    assert retained.params['value'].label == 'keep'
    assert len(events) == 1 and events[0][0] == 'released'
"""
)

_OVERWRITE_OBSERVER = """\
def pytest_collection_finish(session):
    try:
        if not session.items:
            return
        module = session.items[0].module
        assert not gc.isenabled()
        record([module.events, module.snapshot(module.retained), len(session.items)])
    finally:
        gc.enable()
"""

_DICT_REPLACE_CASES = (
    _CASES_COMMON
    + """\
from _pytest.scope import Scope

retained = None
events = []

class Payload:
    def __init__(self, label):
        self.label = label
        self.reference = None

    def __del__(self):
        if self.label != 'old':
            return
        try:
            owner = self.reference()
            assert owner is not None
            events.append(['released', snapshot(owner)])
        except BaseException as error:
            events.append(['error', type(error).__name__, str(error)])

def pytest_generate_tests(metafunc):
    global retained
    rows = [(Payload(label),) for label in ('keep', 'old', 'run')]
    payload = rows[1][0]
    reference = weakref.ref(payload)
    metafunc.parametrize(('value',), rows)
    retained = metafunc._calls[0]
    victim = metafunc._calls.pop(1)
    payload.reference = weakref.ref(victim)
    # This weak back-reference cannot manufacture an ownership cycle.
    del payload, rows
    replacement = {
        'params': {'value': 'new'}, 'indices': {'value': 91},
        '_arg2scope': {'value': Scope.Function},
        '_idlist': ['replacement'], 'marks': [],
    }
    assert not gc.isenabled()
    object.__setattr__(victim, '__dict__', replacement)
    assert reference() is None, 'dictionary replacement delayed the old destructor'
    assert len(events) == 1 and events[0][0] == 'released', events
    # The destructor must see the committed replacement, including its metadata.
    assert events[0][1] == snapshot(victim), events
    assert events[0][1][1] == 'new' and events[0][1][3] == {'value': 91}, events
    assert events[0][1][-1] == 'replacement', events
    assert vars(victim) is replacement
    callspec_reference = weakref.ref(victim)
    del victim
    assert callspec_reference() is None

def test_case(value):
    assert value.label in ('keep', 'run')
    assert retained.params['value'].label == 'keep'
    assert len(events) == 1 and events[0][0] == 'released'
"""
)

_SHARED_CASES = (
    _CASES_COMMON
    + """\
import sys

events = []
observations = None

class Payload:
    def __init__(self, label):
        self.label = label

    def __del__(self):
        if self.label == 'shared':
            events.append('shared released')

def pytest_generate_tests(metafunc):
    global observations
    shared = Payload('shared')
    reference = weakref.ref(shared)
    rows = [
        (shared, shared), (shared, shared),
        (Payload('run'), Payload('run')), (Payload('run'), Payload('run')),
    ]
    metafunc.parametrize(('left', 'right'), rows)
    first = metafunc._calls.pop(0)
    second = metafunc._calls.pop(0)
    del rows, shared
    assert not gc.isenabled()
    first_params = first.params
    assert first_params['left'] is first_params['right'] is reference()
    second_params = second.params
    assert second_params['left'] is second_params['right'] is first_params['left']
    # Once both public dictionaries exist, their real reference ownership must
    # match pytest exactly. Every key deletion releases one strong reference.
    counts = [sys.getrefcount(reference())]
    del first_params['left']
    counts.append(sys.getrefcount(reference()))
    del first_params['right']
    assert reference() is not None, 'one sibling released another sibling payload'
    counts.append(sys.getrefcount(reference()))
    del second_params['left']
    counts.append(sys.getrefcount(reference()))
    assert counts == list(range(counts[0], counts[0] - 4, -1)), counts
    del second_params['right']
    assert reference() is None, 'last public key failed to release the payload'
    assert events == ['shared released'], events
    observations = [counts, dict(first.indices), dict(second.indices), first.id, second.id]

def test_case(left, right):
    assert left.label == right.label == 'run'
    assert events == ['shared released']
"""
)

_SHARED_OBSERVER = """\
def pytest_collection_finish(session):
    try:
        if not session.items:
            return
        module = session.items[0].module
        assert not gc.isenabled()
        record([module.observations, module.events, len(session.items)])
    finally:
        gc.enable()
"""

_PROTOCOL_CASES = (
    _CASES_COMMON
    + """\
class Payload:
    def __init__(self, label):
        self.label = label
        self.owner = None

def pytest_generate_tests(metafunc):
    rows = [(Payload(label),) for label in ('copy', 'deepcopy', 'pickle')]
    metafunc.parametrize(('value',), rows)
    # Back-references are installed on the initial objects without observing
    # params; the native row must still own these arbitrary original values.
    for row, spec in zip(rows, metafunc._calls):
        row[0].owner = spec

def test_case(value):
    assert value.label in ('copy', 'deepcopy', 'pickle')
    assert value.owner.params['value'] is value
"""
)

_PROTOCOL_OBSERVER = """\
import copy
import pickle

def clean(spec):
    mapping = vars(spec)
    assert type(mapping) is dict and mapping is spec.__dict__
    assert list(mapping) == FIELDS, list(mapping)

def pytest_collection_finish(session):
    try:
        if not session.items:
            return
        module = session.items[0].module
        calls = [item.callspec for item in session.items]
        observations = []
        for name, original in zip(('copy', 'deepcopy', 'pickle'), calls):
            if name == 'copy':
                copied = copy.copy(original)
                assert copied.params is original.params
                assert copied.params['value'] is original.params['value']
                assert copied.params['value'].owner is original
            elif name == 'deepcopy':
                copied = copy.deepcopy(original)
                assert copied.params is not original.params
                assert copied.params['value'] is not original.params['value']
                assert copied.params['value'].owner is copied
            else:
                copied = pickle.loads(pickle.dumps(original, protocol=pickle.HIGHEST_PROTOCOL))
                assert copied.params is not original.params
                assert copied.params['value'] is not original.params['value']
                assert copied.params['value'].owner is copied
            assert type(copied) is CallSpec2 and copied is not original
            assert original.params['value'].owner is original
            clean(original)
            clean(copied)
            observations.append([name, module.snapshot(original), module.snapshot(copied)])
        record(observations)
    finally:
        gc.enable()
"""


@pytest.mark.parametrize("mode", ["collect", "resurrect"])
def test_initial_opaque_sibling_cycle_collects_and_resurrects(tmp_path: Path, mode: str) -> None:
    _compare_public_pair(tmp_path, _CYCLE_CASES, _CYCLE_OBSERVER, mode=mode)


def test_initial_opaque_field_overwrite_releases_after_committing(tmp_path: Path) -> None:
    _compare_public_pair(tmp_path, _OVERWRITE_CASES, _OVERWRITE_OBSERVER)


def test_initial_opaque_dict_replacement_releases_after_committing(tmp_path: Path) -> None:
    _compare_public_pair(tmp_path, _DICT_REPLACE_CASES, _OVERWRITE_OBSERVER)


def test_initial_opaque_shared_keys_and_rows_preserve_reference_ownership(
    tmp_path: Path,
) -> None:
    _compare_public_pair(tmp_path, _SHARED_CASES, _SHARED_OBSERVER)


def test_initial_opaque_copy_deepcopy_pickle_preserve_aliases_and_cycles(
    tmp_path: Path,
) -> None:
    _compare_public_pair(tmp_path, _PROTOCOL_CASES, _PROTOCOL_OBSERVER)
