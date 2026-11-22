"""Rewritten assertion temporaries are freed in the same order as under pytest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PRELUDE = """import json
from pathlib import Path

events = {}
current = []


class Value:
    def __init__(self, name, truth=True):
        self.name = name
        self.truth = truth
        current.append(['create', name])

    def __lt__(self, other):
        current.append(['lt', self.name, other.name])
        return self.truth

    def __eq__(self, other):
        current.append(['eq', self.name, other.name])
        return self.truth

    def __neg__(self):
        return Value('-' + self.name, self.truth)

    def __bool__(self):
        current.append(['bool', self.name])
        return self.truth

    def __call__(self, *arguments, **keywords):
        return Value(self.name + '()', self.truth)

    def __getitem__(self, key):
        return Value(self.name + '[]', self.truth)

    @property
    def child(self):
        return Value(self.name + '.child', self.truth)

    def __repr__(self):
        return '<' + self.name + '>'

    def __del__(self):
        current.append(['delete', self.name])


def make(name, truth=True):
    return Value(name, truth)


def checked(label, body):
    current.clear()
    events[label] = current
    body()
    Path('events.json').write_text(json.dumps(events))
"""

_NAMESPACE = """
class Trace(dict):
    def __contains__(self, key):
        if key == 'x':
            current.append(['contains', key])
        return super().__contains__(key)

    def __getitem__(self, key):
        if key == 'x':
            current.append(['get', key])
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        if key == 'x':
            current.append(['set', key, repr(value)])
        super().__setitem__(key, value)


class Meta(type):
    @classmethod
    def __prepare__(mcs, name, bases):
        return Trace()


def namespace():
    try:
        class Plain(metaclass=Meta):
            x = make('x0', False)
            assert x == make('y')
    except AssertionError:
        current.append(['caught-plain'])
    try:
        class Walrus(metaclass=Meta):
            x = make('x1')
            assert (x := make('x2', False)) == make('z')
    except AssertionError:
        current.append(['caught-walrus'])
"""

_OPERANDS = """
import gc


class Probe(Value):
    def __repr__(self):
        tuples = sum(type(owner) is tuple for owner in gc.get_referrers(self))
        current.append(['repr', self.name, tuples])
        return '<' + self.name + '>'


def operands():
    try:
        assert Probe('a', False) == Probe('b')
    except AssertionError:
        current.append(['caught'])
    try:
        assert Probe('c') < Probe('d', False) < Probe('e')
    except AssertionError:
        current.append(['caught-chain'])
"""

_SHAPES = {
    "chained": """
def chained():
    assert make('a') < make('b') < make('c')
    current.append(['after-pass'])
    try:
        assert make('d') < make('e', False) < make('f')
    except AssertionError:
        current.append(['caught'])
    current.append(['after-fail'])
    assert make('g') < make('h')
    current.append(['after-overwrite'])
    try:
        assert make('i') < make('j') < make('k', False) < make('l')
    except AssertionError:
        current.append(['caught'])
    assert make('m') < make('n')
""",
    "boolean": """
def boolean():
    assert make('a') and make('b')
    current.append(['after-and'])
    assert make('c') or make('d')
    current.append(['after-or'])
    assert not make('e', False)
    current.append(['after-not'])
    try:
        assert make('f') and make('g', False) and make('h')
    except AssertionError:
        current.append(['caught-and'])
    try:
        assert make('i', False) or make('j', False)
    except AssertionError:
        current.append(['caught-or'])
    try:
        assert not make('k')
    except AssertionError:
        current.append(['caught-not'])
    assert make('l') and (make('m', False) or make('n'))
    current.append(['after-nested'])
    assert make('o')
""",
    "call": """
def call():
    assert make('a')(make('b'), make('c')) == make('d')
    current.append(['after-pass'])
    try:
        assert make('e')(make('f'))(make('g')) == make('h', False)
    except AssertionError:
        current.append(['caught'])
    current.append(['after-fail'])
    assert make('i')(make('j'), k=make('k'))
    current.append(['after-keyword'])
    try:
        assert make('l', False)(make('m'))
    except AssertionError:
        current.append(['caught-bare'])
    assert make('n')
""",
    "member": """
def member():
    assert make('a').child == make('b')[0]
    current.append(['after-pass'])
    try:
        assert make('c').child.child == make('d', False)
    except AssertionError:
        current.append(['caught'])
    current.append(['after-fail'])
    assert make('e')[make('f')]
    current.append(['after-subscript'])
    try:
        assert make('g', False)[make('h')]
    except AssertionError:
        current.append(['caught-subscript'])
    assert make('i').child
""",
    "mixed": """
def mixed():
    assert -make('a') == make('b') or make('c')
    current.append(['after-unary'])
    try:
        assert (make('d', False) == make('e')) is False, make('message')
    except AssertionError:
        current.append(['caught-message'])
    assert (value := make('f')) == make('g') and value
    current.append(['after-walrus'])
    try:
        assert make('h')(*[make('i')], **{'k': make('j', False)}) == make('l', False)
    except AssertionError:
        current.append(['caught-starred'])
    assert make('m') == make('n') == make('o') or make('p')[0].child
""",
    "walrus": """
def note(label, *names):
    current.append([label, *names])


def walrus():
    x = make('x0')
    assert x and (x := make('x1')) < make('y')
    note('after-and', x.name)
    y = make('y0')
    assert (y := make('y1')) and y
    note('after-self', y.name)
    z = make('z0')
    assert z and (z := make('z1')) == make('w') and z
    note('after-three', z.name)
    a = make('a0')
    try:
        assert a and (a := make('a1', False)) < make('b')
    except AssertionError as error:
        note('caught-and', a.name, str(error))
    c = make('c0', False)
    assert c or (c := make('c1')) == make('d')
    note('after-or', c.name)
    e = make('e0')
    try:
        assert e and (e := make('e1')) < make('f', False) or (e := make('e2')) == make('g', False)
    except AssertionError as error:
        note('caught-or', e.name, str(error))
    h = make('h0')
    assert h and (h := make('h1'))(h, k=h)
    note('after-call', h.name)
    i = make('i0')
    assert i and (i := make('i1')) and i.child
    note('after-member', i.name)
    j = make('j0')
    assert (j := make('j1')) and (j := make('j2'))
    note('after-double', j.name)
    k = make('k0')
    try:
        assert k and (k := make('k1')) < make('l') < make('m', False), make('msg')
    except AssertionError as error:
        note('caught-chain', k.name, str(error))
    n = make('n0')
    assert n and (n := make('n1')) == make('o') and (n := make('n2')) == make('p') and n
    note('after-multi', n.name)
    q = make('q0')
    assert not (q and (q := make('q1', False)) < make('r'))
    note('after-not', q.name)
    assert make('s')
""",
}


type _Events = list[list[object]]


def _run_both(project: VfyProject, name: str, body: str) -> tuple[_Events, _Events]:
    project.write(
        f"test_{name}.py",
        _PRELUDE + body + f"\n\ndef test_{name}():\n    checked({name!r}, {name})\n",
    )
    stock = project.run_pytest("-q", f"test_{name}.py", load_xdist=False)
    stock.expect(0)
    expected = cast(_Events, json.loads((project.root / "events.json").read_text())[name])
    (project.root / "events.json").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q", f"test_{name}.py")
    independent.expect(0)
    actual = cast(_Events, json.loads((project.root / "events.json").read_text())[name])
    (project.root / "events.json").unlink()
    return expected, actual


def _observe(project: VfyProject, name: str, body: str) -> None:
    expected, actual = _run_both(project, name, body)
    assert any(event[0] == "delete" for event in expected)
    assert actual == expected


def test_chained_comparison_temporaries_follow_pytest_lifetimes(tmp_path: Path) -> None:
    _observe(VfyProject(tmp_path), "chained", _SHAPES["chained"])


def test_boolean_operator_temporaries_follow_pytest_lifetimes(tmp_path: Path) -> None:
    _observe(VfyProject(tmp_path), "boolean", _SHAPES["boolean"])


def test_call_temporaries_follow_pytest_lifetimes(tmp_path: Path) -> None:
    _observe(VfyProject(tmp_path), "call", _SHAPES["call"])


def test_mixed_expression_temporaries_follow_pytest_lifetimes(tmp_path: Path) -> None:
    _observe(VfyProject(tmp_path), "mixed", _SHAPES["mixed"])


def test_attribute_and_subscript_temporaries_follow_pytest_lifetimes(tmp_path: Path) -> None:
    _observe(VfyProject(tmp_path), "member", _SHAPES["member"])


def test_walrus_in_boolean_operators_follows_pytest(tmp_path: Path) -> None:
    _observe(VfyProject(tmp_path), "walrus", _SHAPES["walrus"])


def test_locals_are_checked_before_a_name_is_evaluated_for_its_repr(tmp_path: Path) -> None:
    expected, actual = _run_both(VfyProject(tmp_path), "namespace", _NAMESPACE)
    assert ["contains", "x"] in expected
    assert actual == expected


def test_comparison_tuple_is_released_before_operands_are_described(tmp_path: Path) -> None:
    expected, actual = _run_both(VfyProject(tmp_path), "operands", _OPERANDS)

    def descriptions(events: _Events) -> dict[object, object]:
        # Stock pytest also renders the operands for its comparison summary
        # while the tuple is alive; the last render is the interpolated one.
        return {event[1]: event[2] for event in events if event[0] == "repr"}

    def others(events: _Events) -> _Events:
        return [event for event in events if event[0] != "repr"]

    assert descriptions(expected) == {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}
    assert descriptions(actual) == descriptions(expected)
    assert others(actual) == others(expected)
