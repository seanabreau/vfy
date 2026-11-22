"""Assertion adapters preserve evaluation and import-hook lifetimes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


def _assert_adapter_matches_pytest(tmp_path: Path, source: str) -> dict[str, object]:
    project = VfyProject(tmp_path)
    project.write("test_observe.py", source)
    oracle = project.run_pytest("-q", "test_observe.py", load_xdist=False)
    oracle.expect(0)
    expected = json.loads((project.root / "observed.json").read_text())
    (project.root / "observed.json").unlink()
    result = project.run_vfy("test", "--engine=native", "--no-cov", "test_observe.py")
    result.expect(0)
    decoded = json.loads((project.root / "observed.json").read_text())
    assert isinstance(decoded, dict)
    observed: dict[str, object] = {}
    for key, value in decoded.items():
        assert isinstance(key, str)
        observed[key] = value
    assert observed == expected
    return observed


@requires_native_pytest
def test_rewritten_assertions_keep_messages_lazy_and_boolean_calls_short_circuited(
    tmp_path: Path,
) -> None:
    observed = _assert_adapter_matches_pytest(
        tmp_path,
        """import json
from pathlib import Path

def test_observe():
    observed = {}
    events = []
    def operand(label, value):
        events.append(label)
        return value
    def message():
        events.append('message')
        return 'custom message'
    class Rejected:
        def __bool__(self):
            events.append('bool')
            return False
        def __repr__(self):
            events.append('repr')
            return 'rejected'
    for scenario in ['and', 'or', 'pass', 'fail']:
        events.clear()
        try:
            if scenario == 'and':
                assert operand('left', False) and operand('right', True), message()
            if scenario == 'or':
                assert operand('left', True) or operand('right', False), message()
            if scenario == 'pass':
                assert operand('pass', True), message()
            if scenario == 'fail':
                rejected = Rejected()
                assert rejected, message()
        except AssertionError as error:
            # Call explanations contain function addresses; the failure message
            # and side effects identify this contract independently of those.
            observed[scenario] = [str(error).splitlines()[0], events[:]]
        else:
            observed[scenario] = [None, events[:]]
    Path('observed.json').write_text(json.dumps(observed))
""",
    )
    assert observed["fail"] == ["custom message", ["bool", "message", "repr"]]
    assert observed["or"] == [None, ["left"]]


@requires_native_pytest
def test_rewritten_assertions_keep_repr_failures_safe_and_interrupts_visible(
    tmp_path: Path,
) -> None:
    observed = _assert_adapter_matches_pytest(
        tmp_path,
        """import json
import re
from pathlib import Path

def test_observe():
    observed = {}
    class Broken:
        def __init__(self, kind):
            self.kind = kind
        def __bool__(self):
            return False
        def __repr__(self):
            raise self.kind('repr failure')
    for kind in [ValueError, GeneratorExit, KeyboardInterrupt, SystemExit]:
        value = Broken(kind)
        try:
            assert value
        except BaseException as error:
            text = re.sub(r'0x[0-9a-fA-F]+', '<address>', str(error))
            observed[kind.__name__] = [type(error).__name__, text]
    Path('observed.json').write_text(json.dumps(observed))
""",
    )
    assert observed["KeyboardInterrupt"] == ["KeyboardInterrupt", "repr failure"]
    generator_exit = observed["GeneratorExit"]
    assert isinstance(generator_exit, list)
    assert generator_exit[0] == "AssertionError"


@requires_native_pytest
def test_assertion_messages_use_safe_repr_before_explaining_operands(tmp_path: Path) -> None:
    observed = _assert_adapter_matches_pytest(
        tmp_path,
        """import json
from pathlib import Path

def test_observe():
    observed = {}
    class Value:
        def __bool__(self):
            return False
        def __repr__(self):
            return 'VALUE'
    class Message:
        def __str__(self):
            return 'STR'
        def __repr__(self):
            return 'REPR'
    for name, message in [('object', Message()), ('multiline', 'line1\\nline2')]:
        value = Value()
        try:
            assert value, message
        except AssertionError as error:
            observed[name] = str(error)
    Path('observed.json').write_text(json.dumps(observed))
""",
    )
    assert observed["object"] == "REPR\nassert VALUE"
    assert observed["multiline"] == "line1\n  line2\nassert VALUE"


@requires_native_pytest
def test_registered_helpers_rewrite_descendants_and_respect_module_docstrings(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest
pytest.register_assert_rewrite('helper_pkg', 'optout', 'marker_variable')
""",
    )
    project.write("helper_pkg/__init__.py", "")
    helper = "def check():\n    assert 1 == 2\n"
    project.write("helper_pkg/child.py", helper)
    project.write("helper_pkg_extra.py", helper)
    project.write(
        "optout.py",
        '"""Keep PYTEST_DONT_REWRITE assertions plain."""\n'
        "from __future__ import annotations\n" + helper,
    )
    project.write("marker_variable.py", 'marker = "PYTEST_DONT_REWRITE"\n' + helper)
    project.write("already_plain.py", helper)
    project.write("already_optout.py", '"""PYTEST_DONT_REWRITE"""\n' + helper)
    project.write(
        "test_helpers.py",
        """import importlib
import json
import warnings
from pathlib import Path
import pytest

def test_observe():
    observed = {}
    for name in ['helper_pkg.child', 'helper_pkg_extra', 'optout', 'marker_variable']:
        module = importlib.import_module(name)
        try:
            module.check()
        except AssertionError as error:
            observed[name] = str(error)
    import already_plain
    import already_optout
    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter('always')
        pytest.register_assert_rewrite('already_plain', 'already_optout')
    observed['warnings'] = [[record.category.__name__, str(record.message)]
                            for record in records]
    Path('observed.json').write_text(json.dumps(observed))
""",
    )
    oracle = project.run_pytest("-q", "test_helpers.py", load_xdist=False)
    oracle.expect(0)
    expected = json.loads((project.root / "observed.json").read_text())
    (project.root / "observed.json").unlink()
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "test_helpers.py",
    )
    actual.expect(0)
    assert json.loads((project.root / "observed.json").read_text()) == expected
    assert expected == {
        "helper_pkg.child": "assert 1 == 2",
        "helper_pkg_extra": "",
        "optout": "",
        "marker_variable": "assert 1 == 2",
        "warnings": [],
    }
    for result in [oracle, actual]:
        assert "Module already imported so cannot be rewritten; already_plain" in (
            result.stdout + result.stderr
        )


@requires_native_pytest
@pytest.mark.parametrize("collection_fails", [False, True])
def test_runner_restores_import_hooks_after_success_and_collection_failure(
    tmp_path: Path, collection_fails: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import pytest\npytest.register_assert_rewrite('lifecycle_helper')\n",
    )
    project.write("lifecycle_helper.py", "def check():\n    assert 1 == 2\n")
    activation = """from pathlib import Path
from lifecycle_helper import check

def activate():
    try:
        check()
    except AssertionError as error:
        Path('activation.txt').write_text(str(error))

"""
    project.write(
        "test_case.py",
        activation
        + (
            "activate()\nraise RuntimeError('collection failure')\n"
            if collection_fails
            else "def test_pass():\n    activate()\n"
        ),
    )
    project.write(
        "sitecustomize.py",
        """import atexit, json, sys
from pathlib import Path
before = tuple(sys.meta_path)

def record():
    Path('lifecycle.json').write_text(json.dumps(tuple(sys.meta_path) == before))

atexit.register(record)
""",
    )
    for engine in ("stock", "native"):
        for _ in range(2):
            (project.root / "activation.txt").unlink(missing_ok=True)
            result = project.run_engine(
                engine, "-q", "test_case.py", env={"PYTHONPATH": str(project.root)}
            )
            assert result.returncode == (2 if collection_fails else 0), result.stdout
            assert (project.root / "activation.txt").read_text() == "assert 1 == 2"
            assert json.loads((project.root / "lifecycle.json").read_text()) is True


def test_rewrite_registration_validates_all_arguments(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

observed = []
for arguments in [(), ('package', 'package.child'), ('',), ('a..b',),
                  (None,), (['package'],), ('package', None)]:
    try:
        result = pytest.register_assert_rewrite(*arguments)
    except TypeError as error:
        observed.append(['TypeError', str(error)])
    else:
        observed.append(result)
Path('registration.json').write_text(json.dumps(observed))
""",
    )
    project.write("test_case.py", "def test_pass():\n    pass\n")
    expected = None
    for engine in ("stock", "native"):
        result = project.run_engine(engine, "-q")
        result.expect(0)
        observed = json.loads((project.root / "registration.json").read_text())
        (project.root / "registration.json").unlink()
        if expected is None:
            expected = observed
        assert observed == expected


@requires_native_pytest
def test_expression_rebinding_access_and_generator_side_effects_match_pytest(
    tmp_path: Path,
) -> None:
    _assert_adapter_matches_pytest(
        tmp_path,
        """import json
from pathlib import Path

events = []
value = 1

def mutate():
    global value
    events.append('mutate')
    value = 2
    return 2

def old_call(argument):
    events.append('old call')
    return 1

def new_call(argument):
    events.append('new call')
    return 2

chosen = old_call

def change_call():
    global chosen
    events.append('argument')
    chosen = new_call
    return 0

def change_star():
    change_call()
    return [0]

def change_mapping():
    change_call()
    return {'argument': 0}

class Access:
    def __init__(self):
        self.count = 0
    @property
    def item(self):
        self.count += 1
        events.append('attribute:' + str(self.count))
        return self.count
    def __getitem__(self, key):
        events.append('subscript:' + str(key))
        return key
    def __repr__(self):
        events.append('owner repr')
        return 'owner'

class Stream:
    def __iter__(self):
        events.append('iter')
        for value in [1, 0, 3]:
            events.append('yield:' + str(value))
            yield value

def visit(value):
    events.append('visit:' + str(value))
    return value

def bump():
    events.append('bump')
    return 0

def index():
    events.append('index')
    return len(events)

def test_observe():
    global value, chosen
    observed = {}
    for scenario in ['name', 'callee', 'starred', 'mapping', 'missing', 'attribute', 'subscript',
                     'generator', 'comprehension', 'walrus right', 'walrus plain',
                     'walrus compare', 'walrus boolean', 'walrus chain']:
        events.clear()
        value, chosen = 1, old_call
        local = 1
        owner = Access()
        try:
            if scenario == 'name':
                assert value == mutate()
            if scenario == 'callee':
                assert chosen(change_call()) == 2
            if scenario == 'starred':
                assert chosen(*change_star()) == 2
            if scenario == 'mapping':
                assert chosen(**change_mapping()) == 2
            if scenario == 'missing':
                assert missing_name == mutate()
            if scenario == 'attribute':
                assert owner.item == owner.item
            if scenario == 'subscript':
                assert owner[index()] == owner[index()]
            if scenario == 'generator':
                assert all(visit(value) for value in Stream())
            if scenario == 'comprehension':
                assert [visit(value) for value in Stream()] == []
            if scenario == 'walrus right':
                assert local == (local := 2)
            if scenario == 'walrus plain':
                assert (local := bump())
            if scenario == 'walrus compare':
                assert (local := bump()) == 1
            if scenario == 'walrus boolean':
                assert (local := bump()) and False
            if scenario == 'walrus chain':
                assert 0 < (local := bump()) < 3
        except BaseException as error:
            outcome = type(error).__name__
        else:
            outcome = 'passed'
        observed[scenario] = [outcome, events[:], value, local]
    Path('observed.json').write_text(json.dumps(observed))
""",
    )


@requires_native_pytest
def test_rewritten_expressions_keep_user_source_locations(tmp_path: Path) -> None:
    _assert_adapter_matches_pytest(
        tmp_path,
        '''"""Retain nested source positions after the future import."""
from __future__ import annotations
import json
import linecache
from pathlib import Path

class Box:
    @property
    def value(self):
        raise RuntimeError('attribute failure')

def mismatch():
    assert (
        1 == 2
    )

def missing():
    assert (
        absent_name == 1
    )

def division():
    assert (
        1 /
        0
    )

def attribute():
    assert (
        Box().value
    )

def nested():
    def inner(values: list[Missing]):
        for value in values:
            if value:
                assert (
                    [part for part in (lambda: [value])()]
                    == [value + 1]
                )
    inner([1])

def cross_suites():
    value = 1
    if True:
        assert (
            int(value) == 2
        )
    try:
        assert (value := 3) == 4
    finally:
        pass

def test_observe():
    observed = {}
    for function in [mismatch, missing, division, attribute, nested, cross_suites]:
        try:
            function()
        except BaseException as error:
            frames = []
            traceback = error.__traceback__
            while traceback is not None:
                filename = traceback.tb_frame.f_code.co_filename
                if Path(filename).name == 'test_observe.py':
                    frames.append([
                        traceback.tb_frame.f_code.co_name,
                        traceback.tb_lineno,
                        linecache.getline(filename, traceback.tb_lineno).strip(),
                    ])
                traceback = traceback.tb_next
            observed[function.__name__] = [type(error).__name__, frames]
    Path('observed.json').write_text(json.dumps(observed))
''',
    )


@requires_native_pytest
def test_comparison_walrus_target_preserves_the_original_name(tmp_path: Path) -> None:
    _assert_adapter_matches_pytest(
        tmp_path,
        """import json
from pathlib import Path

def test_observe():
    value = 1
    try:
        assert value == (value := 2)
    except AssertionError as error:
        observed = {'outcome': str(error), 'value': value}
    else:
        observed = {'outcome': 'passed', 'value': value}
    Path('observed.json').write_text(json.dumps(observed))
""",
    )


@requires_native_pytest
def test_call_and_attribute_explanations_match_pytest(tmp_path: Path) -> None:
    _assert_adapter_matches_pytest(
        tmp_path,
        """import json
from pathlib import Path

def identity(value):
    return value

def pair(left, *, right):
    return left

class Box:
    value = 1
    def __repr__(self):
        return 'box'

box = Box()

def test_observe():
    observed = {}
    for scenario in ['call', 'nested', 'siblings', 'keyword', 'starred', 'mapping',
                     'attribute', 'binary', 'boolean', 'unary']:
        try:
            if scenario == 'call':
                assert identity(1) == 2
            if scenario == 'nested':
                assert identity(identity(1)) == 2
            if scenario == 'siblings':
                assert identity(1) == identity(2)
            if scenario == 'keyword':
                assert pair(1, right=2) == 2
            if scenario == 'starred':
                assert identity(*[1]) == 2
            if scenario == 'mapping':
                assert identity(**{'value': 1}) == 2
            if scenario == 'attribute':
                assert box.value == 2
            if scenario == 'binary':
                assert identity(1) + identity(2) == 4
            if scenario == 'boolean':
                assert identity(False) or identity(False)
            if scenario == 'unary':
                assert not identity(True)
        except AssertionError as error:
            observed[scenario] = str(error)
    Path('observed.json').write_text(json.dumps(observed))
""",
    )


@requires_native_pytest
def test_string_equality_diffs_match_pytest_at_default_verbosity(tmp_path: Path) -> None:
    _assert_adapter_matches_pytest(
        tmp_path,
        """import json
from pathlib import Path

def identity(value):
    return value

def test_observe():
    observed = {}
    pairs = [
        ('foo', 'FOO'), ('abc', 'abd'), ('abcde', 'abcXde'), ('abcXde', 'abcde'),
        ('a\\n', 'a'), ('abcXYdefZghi', 'abcUVdefWghi'),
        ('', 'value'), ('value', ''), ('a\\nb\\nc', 'a\\nx\\nc'),
        ('a' * 40 + 'x', 'a' * 40 + 'y'),
        ('a' * 43 + 'x', 'a' * 43 + 'y'),
        ('x' + 'a' * 100, 'y' + 'a' * 100),
        ('b' * 50 + 'x' + 'a' * 100, 'b' * 50 + 'y' + 'a' * 100),
        ('prefix' * 60 + 'enabled = true\\n\\n[server' + 'tail' * 100,
         'prefix' * 60 + 'enabled = TRUE\\n\\n[server' + 'tail' * 100),
        ('\u03b1' * 50 + 'x', '\u03b1' * 50 + 'y'),
    ]
    for index, (left, right) in enumerate(pairs):
        try:
            assert left == right
        except AssertionError as error:
            observed[str(index)] = str(error)
    try:
        assert identity('foo') == 'FOO', 'string call'
    except AssertionError as error:
        observed['call'] = str(error)
    Path('observed.json').write_text(json.dumps(observed))
""",
    )


@requires_native_pytest
@pytest.mark.parametrize("on_ci", [False, True], ids=["terminal", "ci"])
def test_builtin_container_equality_diffs_match_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, on_ci: bool
) -> None:
    # Both interpreters must use the same iteration order for mixed-type sets.
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    monkeypatch.delenv("BUILD_NUMBER", raising=False)
    if on_ci:
        monkeypatch.setenv("CI", "true")
    else:
        monkeypatch.delenv("CI", raising=False)
    _assert_adapter_matches_pytest(
        tmp_path,
        """import json
from pathlib import Path

def test_observe():
    observed = {}
    pairs = [
        ([1, 2], [1, 3]), ([1], [1, 2, 3]), ([1, 2, 3], [4]), ([], [1]),
        ((1, 2), (1, 3)), ((1,), (1, 2, 3)), ([1], (1,)),
        ([[1]], [[2]]), ([1, 2], [1.0, 3]),
        ({'a': 1, 'b': 2}, {'a': 1, 'b': 3}), ({'a': 1}, {'b': 2}),
        ({'a': 1, 'b': 2}, {'a': 2, 'c': 3}),
        ({'z': 1, 'b': 2, 'a': 3}, {}),
        ({1, 2}, {2, 3}), ({1, 2, 3}, {3, 4, 5}), (set(), {1}),
        (frozenset([1]), frozenset([2])),
        (list(range(20)), list(range(19))),
        (tuple(range(20)), tuple(range(19))),
        ({index: 1 for index in range(12)}, {index: 2 for index in range(12)}),
        (set(range(24)), set()),
        (["long word " * 14], ["long word " * 13]),
        ([b"segment" * 15], [b"segment" * 14]),
        ({1, "left"}, {1, "right"}),
        ({("a", 1): [1]}, {("a", 1): [2]}),
    ]
    for index, (left, right) in enumerate(pairs):
        try:
            assert left == right
        except AssertionError as error:
            observed[str(index)] = str(error)
    Path('observed.json').write_text(json.dumps(observed))
""",
    )


@requires_native_pytest
def test_deeply_nested_call_explanations_match_pytest(tmp_path: Path) -> None:
    expression = "identity(" * 80 + "1" + ")" * 80
    _assert_adapter_matches_pytest(
        tmp_path,
        "import json\nfrom pathlib import Path\n"
        "def identity(value):\n    return value\n"
        "def test_observe():\n"
        "    try:\n"
        f"        assert {expression} == 2\n"
        "    except AssertionError as error:\n"
        "        Path('observed.json').write_text(json.dumps({'deep': str(error)}))\n",
    )
