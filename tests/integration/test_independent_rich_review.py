"""Default assertion differences preserve builtin and custom-object boundaries."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()


def test_default_builtin_differences_keep_newlines_and_disjoint_string_edits(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_differences.py",
        """import json
from pathlib import Path

def test_observe():
    pairs = [
        ('terminal newline', 'a\\n', 'a'),
        ('disjoint edits', 'abcXYdefZghi', 'abcUVdefWghi'),
        ('list', [1, 2], [1, 3]),
        ('tuple', (1, 2, 3), (1,)),
        ('dictionary', {'same': 1, 'changed': 2}, {'same': 1, 'changed': 3}),
        ('set', {1, 2}, {2, 3}),
        ('nested', [{'value': [1]}], [{'value': [2]}]),
    ]
    observed = {}
    for name, left, right in pairs:
        try:
            assert left == right
        except AssertionError as error:
            observed[name] = str(error)
    Path('observed.json').write_text(json.dumps(observed))
""",
    )
    oracle = project.run_pytest("-q", "test_differences.py", load_xdist=False)
    oracle.expect(0)
    expected = json.loads((project.root / "observed.json").read_text())
    (project.root / "observed.json").unlink()
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "test_differences.py",
        env={"PYTHONPATH": _SOURCE},
    )
    actual.expect(0)
    assert json.loads((project.root / "observed.json").read_text()) == expected


def test_unsupported_container_values_keep_raw_comparison_side_effects(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_custom.py",
        """import json
from pathlib import Path

def test_observe():
    events = []
    class Unknown:
        def __init__(self, label):
            self.label = label
        def __eq__(self, other):
            events.append('element equality')
            return False
        def __repr__(self):
            return self.label
    class CustomList(list):
        def __eq__(self, other):
            events.append('subclass equality')
            return False
    observed = {}
    pairs = [('nested', [Unknown('left')], [Unknown('right')]),
             ('subclass', CustomList([1]), [2])]
    for name, left, right in pairs:
        events.clear()
        try:
            assert left == right
        except AssertionError as error:
            observed[name] = [events[:], str(error)]
    Path('observed.json').write_text(json.dumps(observed))
""",
    )
    project.write(
        "plain.py",
        "import runpy\nrunpy.run_path('test_custom.py')['test_observe']()\n",
    )
    baseline = project.run_python("-S", "plain.py")
    baseline.expect(0)
    expected = json.loads((project.root / "observed.json").read_text())
    (project.root / "observed.json").unlink()
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "test_custom.py",
        env={"PYTHONPATH": _SOURCE},
    )
    actual.expect(0)
    observed = json.loads((project.root / "observed.json").read_text())
    assert {name: value[0] for name, value in observed.items()} == {
        name: value[0] for name, value in expected.items()
    }
    assert observed == {
        "nested": [["element equality"], "assert [left] == [right]"],
        "subclass": [["subclass equality"], "assert [1] == [2]"],
    }


def test_rewritten_temporaries_follow_pytest_lifetimes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_lifetimes.py",
        """import json
from pathlib import Path

events = []
class Value:
    def __init__(self, name, result):
        self.name = name
        self.result = result
        events.append(['create', name])
    def __eq__(self, other):
        events.append(['compare', self.name, other.name])
        if self.result == 'error':
            raise ValueError('comparison failed')
        return self.result
    def __del__(self):
        events.append(['delete', self.name])

def make(name, result):
    return Value(name, result)

def test_lifetime():
    assert make('pass-left', True) == make('pass-right', True)
    events.append(['after-pass'])
    try:
        assert make('fail-left', False) == make('fail-right', False)
    except AssertionError:
        events.append(['caught-failure'])
    events.append(['after-failure'])
    try:
        assert make('error-left', 'error') == make('error-right', True)
    except ValueError:
        events.append(['caught-error'])
    events.append(['after-error'])
    Path('events.json').write_text(json.dumps(events))
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((project.root / "events.json").read_text())
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((project.root / "events.json").read_text()) == expected


def test_rewritten_short_circuit_and_traceback_keep_pytest_lifetimes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_rewrite_lifetimes.py",
        """import gc
import json
from pathlib import Path
import weakref

events = []
references = {}
class Value:
    def __init__(self, label, truth):
        self.label = label
        self.truth = truth
        events.append(['create', label])
    def __bool__(self):
        return self.truth
    def __eq__(self, other):
        return False
    def __repr__(self):
        return self.label

def make(label, truth):
    value = Value(label, truth)
    references[label] = weakref.ref(value)
    return value

def observe(stage):
    events.append([stage, sorted(name for name, ref in references.items() if ref() is not None)])

def failed_traceback():
    try:
        assert make('left', True) == make('right', True)
    except AssertionError as error:
        return error.__traceback__
    raise RuntimeError('expected failure')

def test_observe():
    assert make('short', True) or make('unreachable', True)
    observe('after-short')
    tb = failed_traceback()
    gc.collect()
    observe('traceback-retained')
    tb = None
    gc.collect()
    observe('traceback-released')
    Path('events.json').write_text(json.dumps(events))
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((project.root / "events.json").read_text())
    assert [event[1] for event in expected if event[0] == "create"] == ["short", "left", "right"]
    assert [event for event in expected if event[0] != "create"] == [
        ["after-short", []],
        ["traceback-retained", ["left", "right"]],
        ["traceback-released", []],
    ]
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((project.root / "events.json").read_text()) == expected


def test_rewritten_nested_boolean_preserves_evaluation_order(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_order.py",
        """import json
from pathlib import Path

events = []
def probe(label, value):
    events.append(label)
    return value

def test_order():
    assert probe('first', True) and (probe('second', False) or probe('third', True))
    events.append('passed')
    try:
        assert probe('fourth', True) and (probe('fifth', False) or probe('sixth', False))
    except AssertionError:
        events.append('caught')
    Path('events.json').write_text(json.dumps(events))
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((project.root / "events.json").read_text())
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((project.root / "events.json").read_text()) == expected
