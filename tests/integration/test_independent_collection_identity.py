"""Collection identity checks preserve hook mutations and fixture ownership."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_ENVIRONMENT = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}


def test_collection_reordering_and_repeated_slots_keep_fixture_owners(tmp_path: Path) -> None:
    observations = []
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import json
from pathlib import Path
import pytest

events = []

@pytest.fixture(scope='module', params=['left', 'right'])
def resource(request):
    events.append(['setup', request.node.nodeid, request.param])
    yield request.param
    events.append(['teardown', request.node.nodeid, request.param])

def pytest_collection_modifyitems(items):
    items.reverse()
    items.pop()
    items.append(items[0])
    events.append(['selected', [item.nodeid for item in items]])
    assert items[0] is items[-1]

def pytest_sessionfinish():
    Path('events.json').write_text(json.dumps(events))
""",
        )
        source = """import pytest
from conftest import events

@pytest.mark.parametrize('number', [0, 1, 2])
def test_case(resource, number, request):
    events.append(['call', request.node.nodeid, resource, number])
"""
        project.write("test_a.py", source)
        project.write("test_b.py", source)
        result = (
            project.run_python("-m", "pytest", "-q", env=_ENVIRONMENT)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=_ENVIRONMENT)
        )
        result.expect(0)
        assert "12 passed" in result.stdout, result.stdout + result.stderr
        observations.append(json.loads((project.root / "events.json").read_text()))
    assert observations[1] == observations[0]


@pytest.mark.parametrize("callback", ["item", "monitor", "collector"])
def test_collection_rejects_equal_length_session_item_mutation(
    tmp_path: Path, callback: str
) -> None:
    project = VfyProject(tmp_path)
    hooks = {
        "item": """def pytest_itemcollected(item):
    if len(item.session.items) == 2:
        item.session.items.reverse()
""",
        "monitor": """def pytest_configure(config):
    def before(name, methods, kwargs):
        if name == 'pytest_itemcollected':
            item = kwargs['item']
            if len(item.session.items) == 2:
                item.session.items.reverse()
    def after(outcome, name, methods, kwargs):
        pass
    config.pluginmanager.add_hookcall_monitoring(before, after)
""",
        "collector": """session = None
def pytest_sessionstart(session):
    globals()['session'] = session
def pytest_collectreport(report):
    if report.nodeid == 'test_b.py':
        session.items.reverse()
""",
    }
    project.write("conftest.py", hooks[callback])
    project.write("test_a.py", "def test_one(): pass\ndef test_two(): pass\n")
    project.write(
        "test_b.py",
        "from pathlib import Path\ndef test_three(): Path('executed').touch()\n",
    )
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=_ENVIRONMENT)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "mutating Session.items during native collection is unsupported" in (
        result.stdout + result.stderr
    )
    assert not (project.root / "executed").exists()


def test_function_and_class_paths_share_the_live_parent_object(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

class ModulePath(type(Path())):
    pass

class ClassPath(ModulePath):
    pass

class Probe(pytest.Item):
    def runtest(self):
        pass

observed = []
def pytest_pycollect_makeitem(collector, name, obj):
    if name == 'test_plain':
        collector.path = ModulePath(collector.path)
    elif name == 'test_method':
        collector.path = ClassPath(collector.path)

def pytest_generate_tests(metafunc):
    node = metafunc.definition
    observed.append(['definition', node.name, node.path is node.parent.path,
                     type(node.path).__name__])

def pytest_itemcollected(item):
    observed.append(['item', item.name, item.path is item.parent.path,
                     type(item.path).__name__])

def pytest_collection_finish(session):
    parent = session.items[0].parent
    path = ModulePath(parent.path)
    probe = Probe.from_parent(parent, name='probe', path=path)
    observed.append(['explicit', probe.path is path])
    Path('paths.json').write_text(json.dumps(observed))
""",
    )
    project.write(
        "test_paths.py",
        """import pytest

@pytest.mark.parametrize('value', [0, 1])
def test_plain(value):
    pass

class TestOwner:
    @pytest.mark.parametrize('value', [0, 1])
    def test_method(self, value):
        pass
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "paths.json").read_text())
    assert expected == [
        ["definition", "test_plain", True, "ModulePath"],
        ["item", "test_plain[0]", True, "ModulePath"],
        ["item", "test_plain[1]", True, "ModulePath"],
        ["definition", "test_method", True, "ClassPath"],
        ["item", "test_method[0]", True, "ClassPath"],
        ["item", "test_method[1]", True, "ClassPath"],
        ["explicit", True],
    ]
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((tmp_path / "paths.json").read_text()) == expected


def test_parameterized_items_retain_original_marks_and_separate_marker_lists(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nmarkers = tag(label): marker owner\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

def pytest_collection_modifyitems(items):
    observed = []
    for item in items:
        original = next(mark for mark in item.function.pytestmark if mark.name == 'tag')
        original = getattr(original, 'mark', original)
        own = next(item.iter_markers('tag'))
        observed.append([item.name, own is original,
                         [mark.args[0] for mark in item.iter_markers('tag')]])
    first, second = items[:2]
    first.add_marker(pytest.mark.tag('local'))
    observed.append(['local', [mark.args[0] for mark in first.iter_markers('tag')],
                     [mark.args[0] for mark in second.iter_markers('tag')]])
    first.get_closest_marker('tag').kwargs['changed'] = True
    observed.append(['shared', second.get_closest_marker('tag').kwargs.get('changed')])
    Path('marks.json').write_text(json.dumps(observed))
""",
    )
    project.write(
        "test_marks.py",
        """import pytest

pytestmark = pytest.mark.tag('module')

@pytest.mark.tag('function')
@pytest.mark.parametrize('value', [0, 1])
def test_plain(value):
    pass

@pytest.mark.tag('class')
class TestOwner:
    @pytest.mark.tag('method')
    @pytest.mark.parametrize('value', [pytest.param(0, marks=pytest.mark.tag('parameter')), 1])
    def test_method(self, value):
        pass
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "marks.json").read_text())
    assert expected == [
        ["test_plain[0]", True, ["function", "module"]],
        ["test_plain[1]", True, ["function", "module"]],
        ["test_method[0]", True, ["method", "parameter", "class", "module"]],
        ["test_method[1]", True, ["method", "class", "module"]],
        ["local", ["function", "local", "module"], ["function", "module"]],
        ["shared", True],
    ]
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((tmp_path / "marks.json").read_text()) == expected


def test_decorator_marks_keep_identity_kwargs_and_inherited_class_order(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini", "[pytest]\nmarkers =\n    tag(owner, value): mark owner\n    second\n"
    )
    project.write(
        "conftest.py",
        """import json
import sys
from pathlib import Path
import pytest

def pytest_generate_tests(metafunc):
    if 'value' in metafunc.fixturenames:
        metafunc.parametrize('value', [pytest.param(0, marks=metafunc.module.parameter_mark)],
                             ids=['generated'])

def pytest_collection_modifyitems(items):
    module = sys.modules[items[0].function.__module__]
    rows = module.before_collection
    rows['items'] = [
        [item.nodeid, [[mark.kwargs['owner'], mark.kwargs['value'],
                       mark is module.markers[mark.kwargs['owner']].mark]
                      for mark in item.iter_markers('tag')]]
        for item in items
    ]
    Path('marks.json').write_text(json.dumps(rows))
""",
    )
    project.write(
        "test_marks.py",
        """import pytest

function_mark = pytest.mark.tag(owner='function', value=1)
base_mark = pytest.mark.tag(owner='base', value=1)
class_mark = pytest.mark.tag(owner='class', value=1)
parameter_mark = pytest.mark.tag(owner='parameter', value=1)
markers = dict(function=function_mark, base=base_mark, **{'class': class_mark},
               parameter=parameter_mark)

@function_mark
def test_plain(value): pass
original = test_plain.pytestmark[0]
pytest.mark.second(test_plain)

@base_mark
class Base: pass

@class_mark
class TestOwner(Base):
    def test_method(self): pass

function_mark.kwargs['value'] = 2
base_mark.kwargs['value'] = 3
class_mark.kwargs['value'] = 4
parameter_mark.kwargs['value'] = 5
before_collection = {
    'function': [test_plain.pytestmark[0] is function_mark.mark,
                 test_plain.pytestmark[0] is original],
    'class': [Base.pytestmark[0] is base_mark.mark,
              TestOwner.__dict__['pytestmark'][0] is class_mark.mark,
              len(TestOwner.__dict__['pytestmark'])],
}
""",
    )
    oracle = project.run_pytest("-q", load_xdist=False)
    oracle.expect(0)
    expected = json.loads((tmp_path / "marks.json").read_text())
    assert expected == {
        "function": [True, True],
        "class": [True, True, 1],
        "items": [
            [
                "test_marks.py::test_plain[generated]",
                [["function", 2, True], ["parameter", 5, True]],
            ],
            ["test_marks.py::TestOwner::test_method", [["base", 3, True], ["class", 4, True]]],
        ],
    }
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    candidate.expect(0)
    assert json.loads((tmp_path / "marks.json").read_text()) == expected


def test_parameterized_items_share_the_definition_fixture_names(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

names = {}
observed = []
def pytest_generate_tests(metafunc):
    names[metafunc.function] = metafunc.fixturenames
    metafunc.fixturenames.reverse()

def pytest_itemcollected(item):
    if item.name == 'test_first[0]':
        item.fixturenames.append('extra')
    observed.append([item.name, item.fixturenames is names[item.function],
                     list(item.fixturenames)])

@pytest.fixture
def anchor():
    return 'anchor'

@pytest.fixture
def extra(request):
    with Path('calls.jsonl').open('a') as stream:
        stream.write(json.dumps(['extra', request.node.name]) + '\\n')

def pytest_collection_finish(session):
    Path('names.json').write_text(json.dumps(observed))
""",
    )
    project.write(
        "test_names.py",
        """import json
from pathlib import Path
import pytest

def observe(request):
    with Path('calls.jsonl').open('a') as stream:
        stream.write(json.dumps(['call', request.node.name, request.fixturenames]) + '\\n')

@pytest.mark.parametrize('value', [0, 1])
def test_first(anchor, value, request):
    observe(request)

@pytest.mark.parametrize('value', [0, 1])
def test_other(anchor, value, request):
    observe(request)
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "names.json").read_text())
    expected_calls = (tmp_path / "calls.jsonl").read_text()
    assert len(expected) == 4
    assert all(row[1] is True for row in expected)
    assert all("extra" in row[2] for row in expected[:2])
    assert all("extra" not in row[2] for row in expected[2:])
    assert [json.loads(line)[:2] for line in expected_calls.splitlines()] == [
        ["extra", "test_first[0]"],
        ["call", "test_first[0]"],
        ["extra", "test_first[1]"],
        ["call", "test_first[1]"],
        ["call", "test_other[0]"],
        ["call", "test_other[1]"],
    ]
    (tmp_path / "calls.jsonl").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((tmp_path / "names.json").read_text()) == expected
    assert (tmp_path / "calls.jsonl").read_text() == expected_calls


def test_marker_iterator_reads_live_nodes_only_when_advanced(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nmarkers = probe(value): marker iteration\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

def marker(value):
    return pytest.mark.probe(value).mark

def pytest_collection_modifyitems(items):
    item = items[0]
    original_parent = item.parent
    replacement = items[1].parent
    original_own = item.own_markers
    replacement_own = replacement.own_markers
    calls = []
    def overridden(name=None):
        calls.append(name)
        return iter([marker('overridden')])
    replacement.iter_markers = overridden
    try:
        item.own_markers = [marker('old-own')]
        iterator = item.iter_markers('probe')
        item.own_markers = [marker('live-own')]
        first = next(iterator).args[0]
        item.parent = replacement
        replacement.own_markers = [marker('live-parent')]
        second = next(iterator).args[0]
        remaining = [mark.args[0] for mark in iterator]
        Path('markers.json').write_text(json.dumps([first, second, remaining, calls]))
    finally:
        item.parent = original_parent
        item.own_markers = original_own
        replacement.own_markers = replacement_own
        del replacement.iter_markers
""",
    )
    project.write("test_a.py", "def test_first(): pass\n")
    project.write("test_b.py", "def test_other(): pass\n")
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "markers.json").read_text())
    assert expected == ["live-own", "live-parent", [], []]
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((tmp_path / "markers.json").read_text()) == expected


def test_removed_function_mark_payload_is_released_by_collection_finish(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nmarkers = lifetime(value): payload lifetime\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import weakref

payload = None
definitions = []

class Payload:
    def __init__(self):
        global payload
        payload = weakref.ref(self)

def pytest_generate_tests(metafunc):
    definitions.append(metafunc.definition)

def pytest_collection_modifyitems(items):
    # Both real pytest and the independent collector can retain the definition.
    # Remove every public owner of the mark before observing payload lifetime.
    for node in [*definitions, *items]:
        node.own_markers.clear()
        node.keywords['lifetime'] = None
        node.keywords['pytestmark'] = None
        node.function.pytestmark = []
    definitions.clear()

def pytest_collection_finish(session):
    Path('lifetime.json').write_text(json.dumps(payload() is None))
""",
    )
    project.write(
        "test_lifetime.py",
        """import pytest
from conftest import Payload

@pytest.mark.lifetime(Payload())
def test_case():
    pass
""",
    )
    stock = project.run_pytest("-q", load_xdist=False, env=_ENVIRONMENT)
    stock.expect(0)
    expected = json.loads((project.root / "lifetime.json").read_text())
    assert expected is True
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=_ENVIRONMENT)
    independent.expect(0)
    assert json.loads((project.root / "lifetime.json").read_text()) == expected


def test_itemcollected_monitor_and_live_hook_registration_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

events = []
class Dynamic:
    def pytest_itemcollected(self, item):
        events.append(['dynamic', item.name])

def pytest_configure(config):
    def before(name, methods, kwargs):
        if name == 'pytest_itemcollected':
            events.append(['before', kwargs['item'].name])
    def after(outcome, name, methods, kwargs):
        if name == 'pytest_itemcollected':
            events.append(['after', kwargs['item'].name])
    config.pluginmanager.add_hookcall_monitoring(before, after)

def pytest_itemcollected(item):
    events.append(['initial', item.name])
    if item.name == 'test_first':
        item.config.pluginmanager.register(Dynamic(), 'dynamic-collection-hook')

def pytest_sessionfinish():
    Path('item-hooks.json').write_text(json.dumps(events))
""",
    )
    project.write("test_items.py", "def test_first(): pass\ndef test_second(): pass\n")
    stock = project.run_pytest("-q", load_xdist=False, env=_ENVIRONMENT)
    stock.expect(0)
    expected = json.loads((project.root / "item-hooks.json").read_text())
    assert expected == [
        ["before", "test_first"],
        ["initial", "test_first"],
        ["after", "test_first"],
        ["before", "test_second"],
        ["dynamic", "test_second"],
        ["initial", "test_second"],
        ["after", "test_second"],
    ]
    (project.root / "item-hooks.json").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=_ENVIRONMENT)
    independent.expect(0)
    assert json.loads((project.root / "item-hooks.json").read_text()) == expected


def test_function_parameters_still_read_live_package_paths(tmp_path: Path) -> None:
    observations = []
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import json
from pathlib import Path
import pytest

parents = []

class TrackedPath(type(Path())):
    @property
    def parent(self):
        parents.append(str(self))
        return super().parent

@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    for item in items:
        item.path = TrackedPath(item.path)

class Record:
    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items):
        observed = {'parents': parents[:], 'names': [item.name for item in items]}
        for item in items:
            item.path = Path(item.path)
        Path('paths.json').write_text(json.dumps(observed))

def pytest_configure(config):
    config.pluginmanager.register(Record(), 'tracked-path-observer')
""",
        )
        project.write(
            "test_scopes.py",
            "import pytest\n@pytest.mark.parametrize('value', [0, 1, 2])\n"
            "def test_value(value): pass\n",
        )
        result = (
            project.run_python("-m", "pytest", "-q", env=_ENVIRONMENT)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=_ENVIRONMENT)
        )
        result.expect(0)
        observed = json.loads((project.root / "paths.json").read_text())
        observations.append([len(observed["parents"]), observed["names"]])
    assert observations[0] == [3, ["test_value[0]", "test_value[1]", "test_value[2]"]]
    assert observations[1] == observations[0]


def test_custom_collection_duplicates_and_hook_mutations_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

events = []
class CustomItem(pytest.Item):
    def runtest(self):
        events.append(['custom', self.nodeid])
class CustomFile(pytest.File):
    def collect(self):
        item = CustomItem.from_parent(self, name='duplicate')
        return [item, item]
def pytest_collect_file(file_path, parent):
    if file_path.suffix == '.case':
        return CustomFile.from_parent(parent, path=file_path)
def pytest_collection_modifyitems(items):
    items.reverse()
    items.pop()
    items.append(items[0])
    events.append(['selected', [item.nodeid for item in items]])
def pytest_sessionfinish():
    Path('events.json').write_text(json.dumps(events))
""",
    )
    project.write("test_regular.py", "def test_a(): pass\ndef test_b(): pass\n")
    project.write("zz.case", "")
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "events.json").read_text())
    assert expected[0][1] == [
        "zz.case::duplicate",
        "test_regular.py::test_b",
        "zz.case::duplicate",
    ]
    assert len(expected) == 3
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((tmp_path / "events.json").read_text()) == expected


def test_unowned_item_in_collection_hook_is_rejected(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest
class Foreign(pytest.Item):
    def runtest(self):
        pass
def pytest_collection_modifyitems(items):
    items.append(Foreign.from_parent(items[0].parent, name='foreign'))
""",
    )
    project.write("test_regular.py", "def test_a(): pass\n")
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "item is not owned by this collection" in result.stdout + result.stderr
