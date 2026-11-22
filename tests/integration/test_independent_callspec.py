"""Public collection mutations feed native requests and fixture caching."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _reference(project: VfyProject, expected_status: int = 0) -> object:
    stock = project.run_python("-m", "pytest", "-q")
    project.write("stock.stdout", stock.stdout)
    project.write("stock.stderr", stock.stderr)
    assert stock.returncode == expected_status, stock.stdout + stock.stderr
    expected = json.loads((project.root / "observed.json").read_text())
    project.write("stock-observed.json", json.dumps(expected, indent=2))
    (project.root / "observed.json").unlink()
    return expected


def _compare(project: VfyProject, expected_status: int = 0) -> None:
    expected = _reference(project, expected_status)
    candidate = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-q", "-p", "vfy.independent.outcomes"
    )
    project.write("independent.stdout", candidate.stdout)
    project.write("independent.stderr", candidate.stderr)
    assert candidate.returncode == expected_status, candidate.stdout + candidate.stderr
    assert json.loads((project.root / "observed.json").read_text()) == expected


def _project(tmp_path: Path) -> VfyProject:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nmarkers = note\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
events = []
def pytest_sessionfinish():
    Path('observed.json').write_text(json.dumps(events))
""",
    )
    return project


def test_callspec_preserves_all_parameter_kinds_and_objects(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write(
        "conftest.py",
        """import dataclasses
import json
from pathlib import Path
from _pytest.python import CallSpec2
from _pytest.scope import Scope
def pytest_itemcollected(item):
    if item.originalname == 'test_plain':
        assert not hasattr(item, 'callspec')
        return
    cs = item.callspec
    assert type(cs) is CallSpec2
    assert type(cs.params) is dict and type(cs.indices) is dict
    assert [f.name for f in dataclasses.fields(cs)] == [
        'params', 'indices', '_arg2scope', '_idlist', 'marks']
    assert all(type(value) is Scope for value in cs._arg2scope.values())
    assert all(mark is item.function.parameter_mark.mark for mark in cs.marks)
    assert all(any(own is mark for own in item.own_markers) for mark in cs.marks)
    assert cs.params['direct'] is item.function.direct_value
    assert cs.params['indirect'] is item.function.indirect_value
def pytest_collection_modifyitems(items):
    records = []
    for item in items:
        if not hasattr(item, 'callspec'):
            continue
        cs = item.callspec
        records.append([item.nodeid, list(cs.params), cs.indices,
            {key: value.value for key, value in cs._arg2scope.items()},
            list(cs._idlist), cs.id, [mark.name for mark in cs.marks], item.fixturenames])
    Path('observed.json').write_text(json.dumps(records))
""",
    )
    project.write(
        "test_metadata.py",
        """import pytest
D, I, F = object(), object(), object()
note = pytest.mark.note
@pytest.fixture(params=[pytest.param(F, id='fixture-with-hyphens')], scope='module')
def resource(request):
    assert request.param is F
    return request.param
@pytest.fixture
def direct(): raise AssertionError('direct parameter did not override fixture')
@pytest.fixture
def indirect(request):
    assert request.param is I
    return request.param
@pytest.mark.parametrize('direct,indirect', [pytest.param(D, I, marks=note)],
    ids=['direct-with-hyphens'], indirect=['indirect'])
def test_values(direct, indirect, resource):
    assert direct is D and indirect is I and resource is F
test_values.direct_value = D
test_values.indirect_value = I
test_values.parameter_mark = note
def test_plain(): pass
""",
    )
    _compare(project)


def test_resource_grouping_uses_live_hook_fields_and_preserves_plain_blocks(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest
from _pytest.scope import Scope
events = []
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    events.append(['before', [item.nodeid for item in items]])
    for item in items:
        if hasattr(item, 'callspec') and 'value' in item.callspec.params:
            item.callspec.indices['value'] = 1 - item.callspec.indices['value']
            item.callspec._arg2scope['value'] = Scope.Module
def pytest_collection_finish(session):
    events.append(['collected', [item.nodeid for item in session.items]])
def pytest_sessionfinish():
    Path('observed.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_order.py",
        """import pytest
from conftest import events
@pytest.mark.parametrize('value', [0, 1])
def test_first(value): events.append(['first', value])
def test_plain(): events.append('plain')
@pytest.mark.parametrize('value', [0, 1])
def test_second(value): events.append(['second', value])
@pytest.fixture(scope='module', params=[0, 1])
def indirect(request):
    events.append(['setup', request.param])
    yield request.param
    events.append(['finish', request.param])
def test_indirect(indirect): events.append(['indirect', indirect])
def test_last(): events.append('last')
""",
    )
    _compare(project)


def test_setmulti_preserves_python_indices_iterators_and_mark_validation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest
from _pytest.scope import Scope
events = []
indices = [-(1 << 140), 1 << 180]
class Names:
    def __iter__(self):
        events.append('names iterator')
        return iter(['extra'])
class Values:
    def __iter__(self):
        events.append('values iterator')
        return iter([object()])
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    for number, item in enumerate(items):
        previous = item.callspec
        added = previous.setmulti(argnames=Names(), valset=Values(),
            id='added-id', marks=[pytest.mark.note], scope=Scope.Module,
            param_index=indices[number], nodeid=item.nodeid)
        assert 'extra' not in previous.params
        assert added.indices['extra'] is indices[number]
        item.callspec = added
        assert added.getparam('extra') is added.params['extra']
        try: added.getparam('missing')
        except ValueError as error: events.append(['missing', list(error.args)])
        try:
            added.setmulti(argnames=['invalid'], valset=[None], id='invalid',
                marks=[object()], scope=Scope.Function, param_index=0, nodeid=item.nodeid)
        except TypeError: events.append('invalid mark')
        else: raise AssertionError('invalid mark accepted')
def pytest_sessionfinish():
    Path('observed.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_indices.py",
        """import pytest
from conftest import events, indices
@pytest.fixture(scope='module')
def extra(request):
    assert request.param_index is indices[request.param_index > 0]
    events.append(['index', request.param_index])
    return request.param
@pytest.mark.parametrize('value', [0, 1])
def test_value(value, extra, request):
    assert extra is request.node.callspec.params['extra']
    events.append(['test', value])
""",
    )
    _compare(project)


def test_direct_override_closure_is_a_leaf_and_setup_follows_declared_scopes(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
events = []
def pytest_collection_modifyitems(items):
    events.append(['closures', [[item.nodeid, item.fixturenames] for item in items]])
def pytest_sessionfinish():
    Path('observed.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_closure.py",
        """import pytest
from conftest import events
@pytest.fixture
def hidden(): raise AssertionError('shadowed dependency ran')
@pytest.fixture
def direct(hidden): raise AssertionError('shadowed fixture ran')
@pytest.fixture(scope='module')
def outer(request):
    events.append(['outer', request.fixturenames])
    return 'outer'
@pytest.fixture
def inner(request):
    events.append(['inner', request.fixturenames])
    return 'inner'
@pytest.mark.parametrize('direct', [42])
def test_mixed(direct, inner, outer, request):
    events.append(['test', request.fixturenames])
    assert direct == 42 and inner == 'inner' and outer == 'outer'
@pytest.mark.parametrize('direct', [7])
def test_leaf(direct): assert direct == 7
""",
    )
    _compare(project)


def test_builtin_fixture_closure_uses_real_scopes_and_dependencies(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
events = []
def pytest_collection_modifyitems(items):
    events.append(['closures', [[item.nodeid, item.fixturenames] for item in items]])
def pytest_sessionfinish():
    Path('observed.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_builtin.py",
        """import pytest
from conftest import events
@pytest.fixture
def narrow(request):
    assert request.node.funcargs['pytestconfig'] is request.config
    events.append(['narrow', request.fixturenames])
def test_order(narrow, pytestconfig): events.append('test')
def test_legacy(tmpdir, request):
    assert tmpdir.isdir()
    events.append(['legacy', request.fixturenames])
""",
    )
    _compare(project)


def test_resource_grouping_deduplicates_repeated_existing_items(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest
events = []
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    items.append(items[0])
def pytest_collection_finish(session):
    events.append(['collected', [item.nodeid for item in session.items]])
def pytest_sessionfinish():
    Path('observed.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_duplicate.py",
        """from conftest import events
def test_first(): events.append('first')
def test_second(): events.append('second')
""",
    )
    _compare(project)


def test_cartesian_direct_indices_follow_the_complete_generated_family(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
events = []
def pytest_collection_modifyitems(items):
    for item in items:
        cs = item.callspec
        events.append([item.nodeid, cs.indices, list(cs.params), list(cs._idlist),
            [mark.name for mark in cs.marks]])
def pytest_sessionfinish():
    Path('observed.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_cartesian.py",
        """import pytest
@pytest.fixture(params=[pytest.param(0, id='resource-zero', marks=pytest.mark.note),
                        pytest.param(1, id='resource-one', marks=pytest.mark.note)])
def resource(request): return request.param
@pytest.fixture
def indirect(request): return request.param
@pytest.mark.parametrize('left,right', [(0, 1), (2, 3)], ids=['pair-one', 'pair-two'])
@pytest.mark.parametrize('indirect', [4, 5], indirect=True, ids=['indirect-one', 'indirect-two'])
def test_values(left, right, indirect, resource):
    assert right == left + 1 and indirect in (4, 5) and resource in (0, 1)
""",
    )
    _compare(project)


def test_builtin_tmp_path_consumes_the_actual_parameterized_factory(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest
events = []
@pytest.fixture(scope='session', params=['one', 'two'])
def tmp_path_factory(request, tmp_path_factory):
    label = request.param
    events.append(['factory setup', label])
    class Factory:
        _retention_policy = 'all'
        def mktemp(self, basename, numbered=True):
            events.append(['mktemp', label, basename, numbered])
            return tmp_path_factory.mktemp(basename, numbered=numbered)
    yield Factory()
    events.append(['factory finish', label])
def pytest_sessionfinish():
    Path('observed.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_factory.py",
        """from conftest import events
def test_value(tmp_path, request):
    assert tmp_path.is_dir()
    events.append(['test', request.node.callspec.params['tmp_path_factory']])
""",
    )
    _compare(project)


def test_callspec_mapping_keys_share_parameter_name_identity(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

def pytest_collection_modifyitems(items):
    observed = []
    for item in items:
        spec = item.callspec
        indices = {name: name for name in spec.indices}
        scopes = {name: name for name in spec._arg2scope}
        observed.append([[name, name is indices[name], name is scopes[name]]
                         for name in spec.params])
    Path('observed.json').write_text(json.dumps(observed))
""",
    )
    project.write(
        "test_identity.py",
        """import pytest

@pytest.fixture(params=[3])
def fixture_parameter_with_a_long_name(request):
    return request.param

@pytest.fixture
def indirect_parameter_with_a_long_name(request):
    return request.param

@pytest.mark.parametrize(
    'direct_parameter_with_a_long_name, indirect_parameter_with_a_long_name',
    [(1, 2)], indirect=['indirect_parameter_with_a_long_name'])
def test_values(direct_parameter_with_a_long_name, indirect_parameter_with_a_long_name,
                fixture_parameter_with_a_long_name):
    pass
""",
    )
    expected = _reference(project)
    assert isinstance(expected, list)
    assert len(expected) == 1
    assert all(row[1:] == [True, True] for row in expected[0])
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((project.root / "observed.json").read_text()) == expected


def test_callspec_idlist_types_and_scope_members_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import dataclasses
import json
from pathlib import Path
from _pytest.python import CallSpec2
from _pytest.scope import Scope

def pytest_collection_modifyitems(items):
    spec = items[0].callspec
    initial = CallSpec2()
    explicit = CallSpec2(_idlist=('before',))
    after = explicit.setmulti(argnames=('name',), valset=(1,), id='after', marks=(),
                              scope=Scope.Function, param_index=0, nodeid=items[0].nodeid)
    try:
        spec.params = {}
    except BaseException as error:
        frozen = type(error).__name__
    else:
        frozen = 'accepted'
    record = [type(initial._idlist).__name__, type(spec._idlist).__name__,
              type(explicit._idlist).__name__, type(after._idlist).__name__,
              list(explicit._idlist), list(after._idlist), frozen,
              spec._arg2scope['direct_value'] is Scope.Module,
              spec._arg2scope['session_value'] is Scope.Session,
              dataclasses.is_dataclass(spec)]
    Path('observed.json').write_text(json.dumps(record))
""",
    )
    project.write(
        "test_scope.py",
        """import pytest

@pytest.fixture(scope='session', params=[1])
def session_value(request):
    return request.param

@pytest.mark.parametrize('direct_value', [2], scope='module')
def test_scope(direct_value, session_value):
    pass
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((project.root / "observed.json").read_text())
    assert expected == [
        "tuple",
        "list",
        "tuple",
        "list",
        ["before"],
        ["before", "after"],
        "FrozenInstanceError",
        True,
        True,
        True,
    ]
    (project.root / "observed.json").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((project.root / "observed.json").read_text()) == expected
