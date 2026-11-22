"""Node hook proxies retain pytest's live configuration and conftest scope."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CUSTOM_COLLECTOR = """import pytest

class RelayItem(pytest.Item):
    def runtest(self): pass
    def reportinfo(self): return self.path, 0, self.name

class RelayFile(pytest.File):
    def collect(self):
        yield RelayItem.from_parent(self, name=self.path.stem)

def pytest_collect_file(file_path, parent):
    if file_path.suffix == '.relay':
        return RelayFile.from_parent(parent, path=file_path)
"""


@pytest.mark.parametrize("initial_file", [True, False])
def test_hook_scope_tracks_current_file_and_directory_paths(
    tmp_path: Path, initial_file: bool
) -> None:
    observations = {}
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_body.py", "def test_body(): pass\n")
        project.write(
            "conftest.py",
            f"INITIAL_FILE = {initial_file!r}\n"
            """import json, pytest
from pathlib import Path

class Spec:
    @pytest.hookspec
    def pytest_path_probe(): pass

def pytest_addhooks(pluginmanager): pluginmanager.add_hookspecs(Spec)
def pytest_path_probe(): return 'root'

def pytest_sessionstart(session):
    root = session.config.rootpath
    file_control = root / 'file_control'
    file_control.write_text('')
    directory_control = root / 'directory_control'
    directory_control.mkdir()
    target = root / 'changing_target'
    target.write_text('') if INITIAL_FILE else target.mkdir()
    def observe(path):
        return sorted(session.gethookproxy(path).pytest_path_probe())
    rows = {
        'file': observe(file_control),
        'directory': observe(directory_control),
        'initial': observe(target),
    }
    if INITIAL_FILE:
        target.unlink()
        target.mkdir()
    else:
        target.rmdir()
        target.write_text('')
    rows['changed'] = observe(target)
    Path('relays.json').write_text(json.dumps(rows))
""",
        )
        observations[route] = json.loads(_run(project, route, ("test_body.py",)))
    stock, candidate = observations["stock"], observations["independent"]
    assert stock["file"] == candidate["file"] == ["root"]
    assert stock["directory"] == candidate["directory"] == []
    assert (
        stock["initial"] == candidate["initial"] == stock["file" if initial_file else "directory"]
    )
    assert candidate["changed"] == stock["directory" if initial_file else "file"]


def _run(project: VfyProject, route: str, arguments: tuple[str, ...] = ()) -> str:
    result = project.run_engine(route, "-q", *arguments, independent=("--no-replay", "--workers=1"))
    project.write("runner.stdout", result.stdout)
    project.write("runner.stderr", result.stderr)
    result.expect(0)
    return (project.root / "relays.json").read_text()


@pytest.mark.parametrize("item_kind", ["function", "custom"])
def test_common_hook_relay_shares_mutations_and_tracks_live_configuration(
    tmp_path: Path, item_kind: str
) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        if item_kind == "function":
            project.write("test_body.py", "def test_first(): pass\ndef test_second(): pass\n")
        else:
            project.write("first.relay", "")
            project.write("second.relay", "")
        project.write(
            "conftest.py",
            """import json, pytest
from pathlib import Path

class Proxy:
    def __init__(self, delegate): self.delegate = delegate
    def __getattr__(self, name): return getattr(self.delegate, name)

def pytest_collection_modifyitems(config, items):
    first, second = items
    relay = first.ihook
    observed = {
        'function_items': [isinstance(item, pytest.Function) for item in items],
        'global': [item.ihook is config.hook for item in items],
        'parent_global': [item.parent.ihook is config.hook for item in items],
        'shared': relay is second.ihook,
    }
    marker = object()
    relay.relay_marker = marker
    observed['marker_global'] = getattr(config.hook, 'relay_marker', None) is marker
    observed['marker_other'] = getattr(second.ihook, 'relay_marker', None) is marker
    original_header = config.hook.pytest_report_header
    def replacement(**kwargs): return 'replacement callback'
    try:
        config.hook.pytest_report_header = replacement
        observed['callback_replacement'] = second.ihook.pytest_report_header is replacement
    finally:
        config.hook.pytest_report_header = original_header
    original = config.hook
    proxy = Proxy(original)
    try:
        config.hook = proxy
        observed['configuration_replacement'] = [item.ihook is proxy for item in items]
        observed['parent_configuration_replacement'] = [
            item.parent.ihook is proxy for item in items]
    finally:
        config.hook = original
    kind = type(first)
    missing = object()
    previous = kind.__dict__.get('ihook', missing)
    try:
        kind.ihook = property(lambda self: proxy)
        observed['custom_descriptor'] = [item.ihook is proxy for item in items]
    finally:
        if previous is missing: delattr(kind, 'ihook')
        else: kind.ihook = previous
    Path('relays.json').write_text(json.dumps(observed))
"""
            + (_CUSTOM_COLLECTOR if item_kind == "custom" else ""),
        )
        observations.append(json.loads(_run(project, route)))
    assert observations[0] == {
        "function_items": [item_kind == "function"] * 2,
        "global": [True, True],
        "parent_global": [True, True],
        "shared": True,
        "marker_global": True,
        "marker_other": True,
        "callback_replacement": True,
        "configuration_replacement": [True, True],
        "parent_configuration_replacement": [True, True],
        "custom_descriptor": [True, True],
    }
    assert observations[1] == observations[0]


@pytest.mark.parametrize("override", ["default", "descriptor", "getattribute", "session"])
def test_failed_conftest_scope_preserves_actual_custom_hook_access(
    tmp_path: Path, override: str
) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "scope_observer.py",
            """import json
from pathlib import Path
events = []
def pytest_collectreport(report):
    events.append(['global', report.nodeid, report.outcome])
def pytest_sessionfinish():
    Path('failure-scopes.json').write_text(json.dumps(sorted(
        row for row in events if row[1] == 'broken' and row[2] == 'failed')))
""",
        )
        project.write(
            "conftest.py",
            f"""import pytest
from scope_observer import events
OVERRIDE = {override!r}

class Proxy:
    def __init__(self, delegate): self.delegate = delegate
    def __getattr__(self, name): return getattr(self.delegate, name)
    def pytest_collectreport(self, report):
        events.append(['proxy', report.nodeid, report.outcome])
        return self.delegate.pytest_collectreport(report=report)

def pytest_collectreport(report):
    events.append(['conftest', report.nodeid, report.outcome])

def pytest_configure(config):
    proxy = Proxy(config.hook)
    if OVERRIDE == 'default': return
    kind = pytest.Session if OVERRIDE == 'session' else pytest.Dir
    name = 'gethookproxy' if OVERRIDE == 'session' else (
        '__getattribute__' if OVERRIDE == 'getattribute' else 'ihook')
    missing = object()
    previous = kind.__dict__.get(name, missing)
    if OVERRIDE == 'session':
        def gethookproxy(self, fspath): return proxy
        replacement = gethookproxy
    elif OVERRIDE == 'getattribute':
        original = kind.__getattribute__
        def getattribute(self, name):
            return proxy if name == 'ihook' else original(self, name)
        replacement = getattribute
    else:
        replacement = property(lambda self: proxy)
    setattr(kind, name, replacement)
    def restore():
        if previous is missing: delattr(kind, name)
        else: setattr(kind, name, previous)
    config.add_cleanup(restore)
""",
        )
        project.write("broken/conftest.py", "raise ImportError('nested conftest sentinel')\n")
        result = (
            project.run_pytest("-q", "-p", "scope_observer", load_xdist=False)
            if route == "stock"
            else project.run_vfy(
                "test", "--engine=native", "--all", "--no-cov", "-q", "-p", "scope_observer"
            )
        )
        project.write("runner.stdout", result.stdout)
        project.write("runner.stderr", result.stderr)
        result.expect(2)
        assert "nested conftest sentinel" in result.stdout + result.stderr
        observations.append(json.loads((project.root / "failure-scopes.json").read_text()))
    expected = [["global", "broken", "failed"]]
    if override != "default":
        expected = [["conftest", "broken", "failed"], *expected, ["proxy", "broken", "failed"]]
    assert observations[0] == expected
    assert observations[1] == observations[0]


def test_same_path_collectors_share_published_conftest_scope(tmp_path: Path) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """import json, pytest
from pathlib import Path
class Spec:
    @pytest.hookspec
    def pytest_relay_probe(self): pass
def pytest_addhooks(pluginmanager): pluginmanager.add_hookspecs(Spec)
def pytest_relay_probe(): return 'root'
def pytest_collection_modifyitems(items):
    Path('relays.json').write_text(json.dumps({
        'function_items': [isinstance(item, pytest.Function) for item in items],
        'same_parent_path': items[0].parent.path == items[1].parent.path,
        'scopes': [[item.path.parent.name, sorted(item.ihook.pytest_relay_probe()),
                    sorted(item.parent.ihook.pytest_relay_probe())] for item in items],
    }))
"""
            + _CUSTOM_COLLECTOR.replace("'.relay'", "'.py'"),
        )
        for side in ("left", "right"):
            project.write(f"{side}/conftest.py", f"def pytest_relay_probe(): return {side!r}\n")
            project.write(f"{side}/test_{side}.py", "")
        arguments = (
            "--keep-duplicates",
            "left/test_left.py",
            "left/test_left.py",
            "right/test_right.py",
        )
        observations.append(json.loads(_run(project, route, arguments)))
    assert observations[0] == {
        "same_parent_path": True,
        "function_items": [False, False, False],
        "scopes": [
            ["left", ["left", "root"], ["left", "root"]],
            ["left", ["left", "root"], ["left", "root"]],
            ["right", ["right", "root"], ["right", "root"]],
        ],
    }
    assert observations[1] == observations[0]


def test_initial_conftest_scope_publishes_after_live_ancestor_imports(tmp_path: Path) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("tests/test_body.py", "def test_body(): pass\n")
        project.write(
            "conftest.py",
            """import json, pytest
from pathlib import Path
child = Path(__file__).parent / 'tests' / 'conftest.py'
child.write_text("def pytest_relay_probe(): return 'tests'\\n")
class Spec:
    @pytest.hookspec
    def pytest_relay_probe(self): pass
def pytest_addhooks(pluginmanager): pluginmanager.add_hookspecs(Spec)
def pytest_relay_probe(): return 'root'
def pytest_sessionstart(session):
    root = session.config.rootpath
    Path('relays.json').write_text(json.dumps({
        'selected': sorted(session.gethookproxy(root / 'tests').pytest_relay_probe()),
        'unselected_parent': sorted(session.gethookproxy(root).pytest_relay_probe()),
    }))
""",
        )
        observations.append(json.loads(_run(project, route, ("tests",))))
    assert observations[0] == {"selected": ["root", "tests"], "unselected_parent": []}
    assert observations[1] == observations[0]


@pytest.mark.parametrize("item_kind", ["function", "custom"])
def test_hook_relay_reads_later_loaded_conftest_exclusions(tmp_path: Path, item_kind: str) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("relay_events.py", "phase_calls = []\n")
        project.write(
            "conftest.py",
            """import json, pytest
from pathlib import Path
from relay_events import phase_calls

class Spec:
    @pytest.hookspec
    def pytest_relay_probe(self): pass

def pytest_addhooks(pluginmanager): pluginmanager.add_hookspecs(Spec)
def pytest_relay_probe(): return 'root'
initial_scope = None
def pytest_sessionstart(session):
    global initial_scope
    initial_scope = sorted(session.gethookproxy(session.config.rootpath).pytest_relay_probe())
def pytest_runtest_setup(item): phase_calls.append(['setup', 'root', item.path.parent.name])
def pytest_runtest_call(item): phase_calls.append(['call', 'root', item.path.parent.name])
def pytest_runtest_teardown(item): phase_calls.append(['teardown', 'root', item.path.parent.name])

def pytest_collection_modifyitems(session, config, items):
    unloaded = config.rootpath / 'unloaded'
    unloaded.mkdir()
    Path('unloaded.json').write_text(json.dumps({
        'directory': sorted(session.gethookproxy(unloaded).pytest_relay_probe()),
        'file': sorted(session.gethookproxy(unloaded / 'probe.py').pytest_relay_probe()),
        'missing_in_published': sorted(session.gethookproxy(
            config.rootpath / 'missing_probe.py').pytest_relay_probe()),
        'existing_in_published': sorted(session.gethookproxy(
            config.rootpath / 'conftest.py').pytest_relay_probe()),
    }))
    first, second = items
    path = first.path
    try:
        first.path = second.path
        moved = sorted(first.ihook.pytest_relay_probe())
    finally:
        first.path = path
    parent_path = first.parent.path
    try:
        first.parent.path = second.parent.path
        parent_moved = sorted(first.parent.ihook.pytest_relay_probe())
    finally:
        first.parent.path = parent_path
    Path('relays.json').write_text(json.dumps([
        [item.path.parent.name, item.ihook is config.hook,
         item.ihook is item.ihook, sorted(item.ihook.pytest_relay_probe())]
        for item in items
    ]))
    Path('moved.json').write_text(json.dumps(moved))
    Path('parents.json').write_text(json.dumps({
        'moved': parent_moved,
        'relays': [[item.parent.path.parent.name, item.parent.ihook is config.hook,
                    item.parent.ihook is item.parent.ihook,
                    sorted(item.parent.ihook.pytest_relay_probe())] for item in items],
    }))

def pytest_sessionfinish(session):
    path = Path('relays.json')
    path.write_text(json.dumps({'relays': json.loads(path.read_text()),
                                'phase_calls': sorted(phase_calls),
                                'moved': json.loads(Path('moved.json').read_text()),
                                'parents': json.loads(Path('parents.json').read_text()),
                                'initial': initial_scope,
                                'unloaded': json.loads(Path('unloaded.json').read_text()),
                                'function_items': [isinstance(item, pytest.Function)
                                                   for item in session.items]}))
"""
            + (_CUSTOM_COLLECTOR if item_kind == "custom" else ""),
        )
        for side in ("left", "right"):
            project.write(
                f"{side}/conftest.py",
                "from relay_events import phase_calls\n"
                f"def pytest_relay_probe(): return {side!r}\n"
                "def pytest_runtest_setup(item):\n"
                f"    phase_calls.append(['setup', {side!r}, item.path.parent.name])\n"
                "def pytest_runtest_call(item):\n"
                f"    phase_calls.append(['call', {side!r}, item.path.parent.name])\n"
                "def pytest_runtest_teardown(item):\n"
                f"    phase_calls.append(['teardown', {side!r}, item.path.parent.name])\n",
            )
            if item_kind == "function":
                project.write(f"{side}/test_{side}.py", "def test_body(): pass\n")
            else:
                project.write(f"{side}/probe.relay", "")
        observations.append(json.loads(_run(project, route)))
    assert observations[0] == {
        "function_items": [item_kind == "function"] * 2,
        "relays": [
            ["left", False, False, ["left", "root"]],
            ["right", False, False, ["right", "root"]],
        ],
        "phase_calls": [
            [phase, owner, side]
            for phase in ("call", "setup", "teardown")
            for owner, side in (
                ("left", "left"),
                ("right", "right"),
                ("root", "left"),
                ("root", "right"),
            )
        ],
        "moved": ["right", "root"],
        "initial": ["root"],
        "unloaded": {
            "directory": [],
            "file": [],
            "missing_in_published": [],
            "existing_in_published": ["root"],
        },
        "parents": {
            "moved": ["right", "root"],
            "relays": [
                ["left", False, False, ["left", "root"]],
                ["right", False, False, ["right", "root"]],
            ],
        },
    }
    assert observations[1] == observations[0]


@pytest.mark.parametrize("mode", ["success", "error"])
def test_item_scope_preserves_live_path_callbacks_and_errors(tmp_path: Path, mode: str) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("mode.py", f"MODE = {mode!r}\n")
    project.write("test_value.case", "")
    project.write(
        "conftest.py",
        """import json
import os
from pathlib import Path
import pytest
from mode import MODE

events = []
active = False
original_fspath = os.fspath

def unexpected_fspath(path):
    raise AssertionError("fspath resolved after item.path")

class SessionProxy:
    def __init__(self, delegate): self.delegate = delegate
    def __getattr__(self, name):
        return getattr(self.delegate, name)
    def __del__(self): events.append('release-session')

class Normalized(str):
    def __del__(self): events.append('release-normalized')

class PathLike:
    def __init__(self, path, manager): self.path, self.manager = path, manager
    def __fspath__(self):
        os.fspath = original_fspath
        events.append('fspath')
        marker = object()
        self.manager.register(marker, 'scope-reentry')
        events.append(['reentry', self.manager.get_plugin('scope-reentry') is marker])
        self.manager.unregister(marker)
        if MODE == 'error': raise ValueError('actual fspath observer error')
        return Normalized(str(self.path))
    def __del__(self): events.append('release-pathlike')

class CaseFile(pytest.File):
    def collect(self): yield CaseItem.from_parent(self, name='actual')

class CaseItem(pytest.Item):
    def __getattribute__(self, name):
        global active
        state = object.__getattribute__(self, '__dict__')
        if name == 'session' and state.pop('_observe_scope', False):
            active = True
            events.append('session')
            return SessionProxy(object.__getattribute__(self, name))
        if name == 'path' and active:
            active = False
            events.append('path')
            os.fspath = unexpected_fspath
            return PathLike(object.__getattribute__(self, name), self.config.pluginmanager)
        return object.__getattribute__(self, name)
    def runtest(self): pass

def pytest_collect_file(file_path, parent):
    if file_path.suffix == '.case': return CaseFile.from_parent(parent, path=file_path)

def pytest_runtest_setup(item): item._observe_scope = True

def pytest_sessionfinish(session):
    os.fspath = original_fspath
    Path('observed.json').write_text(json.dumps(events))
""",
    )
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    assert result.returncode == (0 if mode == "success" else 3), result.stdout + result.stderr
    if mode == "error":
        assert "actual fspath observer error" in result.stdout + result.stderr
    observed = json.loads((tmp_path / "observed.json").read_text())
    assert observed == [
        "session",
        "path",
        "fspath",
        ["reentry", True],
        *(
            ["release-pathlike", "release-normalized", "release-session"]
            if mode == "success"
            else ["release-session", "release-pathlike"]
        ),
    ]


@pytest.mark.parametrize("late_registration", [False, True])
def test_no_conftest_hooks_track_late_registration_unregistration_and_blocking(
    tmp_path: Path, late_registration: bool
) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("left/test_first.py", "def test_first(): pass\n")
        project.write("right/test_second.py", "def test_second(): pass\n")
        project.write(
            "scope_driver.py",
            f"LATE_REGISTRATION = {late_registration!r}\n"
            + """import json
from pathlib import Path
import pytest

events = []
late = None

class Late:
    def pytest_runtest_setup(self, item):
        events.append(['late-setup', item.nodeid])
    def pytest_runtest_call(self, item):
        events.append(['late-call', item.nodeid])
    def pytest_runtest_teardown(self, item):
        events.append(['late-teardown', item.nodeid])
    def pytest_runtest_logreport(self, report):
        events.append(['late-report', report.nodeid, report.when])

def pytest_sessionstart(session):
    global late
    late = Late()
    # No conftest is loaded at collection. A later registration located at the
    # project root applies to both item paths, whether treated as a normal
    # plugin or as a conftest by the owning implementation.
    late.__file__ = str(session.config.rootpath / 'conftest.py')

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    manager = item.config.pluginmanager
    events.append(['driver-setup', item.nodeid])
    if item.name == 'test_first':
        assert manager.get_plugin('late-scope') is None
        if LATE_REGISTRATION:
            assert manager.register(late, 'late-scope') == 'late-scope'
            events.append(['registered'])
        else:
            manager.set_blocked('late-scope')
            assert manager.register(late, 'late-scope') is None
    else:
        assert manager.is_blocked('late-scope')
        assert manager.get_plugin('late-scope') is None

@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item):
    manager = item.config.pluginmanager
    events.append(['driver-teardown', item.nodeid])
    if item.name == 'test_first':
        if LATE_REGISTRATION:
            assert manager.unregister(name='late-scope') is late
            events.append(['unregistered'])
        manager.set_blocked('late-scope')
        assert manager.is_blocked('late-scope')
        assert manager.register(late, 'late-scope') is None
        events.append(['blocked'])

def pytest_runtest_logreport(report):
    events.append(['driver-report', report.nodeid, report.when])

def pytest_sessionfinish(session):
    assert session.testscollected == 2
    assert not (session.config.rootpath / 'conftest.py').exists()
    Path('scope-events.json').write_text(json.dumps(events))
""",
        )
        arguments = ("-q", "-p", "scope_driver", "left", "right")
        result = (
            project.run_python("-m", "pytest", *arguments)
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "--workers=1", *arguments)
        )
        result.expect(0)
        observations.append(json.loads((project.root / "scope-events.json").read_text()))
    first = "left/test_first.py::test_first"
    assert [row for row in observations[0] if row[0].startswith("late-")] == (
        [
            ["late-report", first, "setup"],
            ["late-call", first],
            ["late-report", first, "call"],
            ["late-teardown", first],
        ]
        if late_registration
        else []
    )
    assert [row for row in observations[0] if row[0] == "blocked"] == [["blocked"]]
    assert len([row for row in observations[0] if row[0] == "driver-report"]) == 6
    assert observations[1] == observations[0]


def test_declared_function_source_and_arbitrary_path_after_file_replacement(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_body.py", "def test_body(): pass\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

observed = []
class Spec:
    @pytest.hookspec
    def pytest_path_probe(): pass

def pytest_addhooks(pluginmanager): pluginmanager.add_hookspecs(Spec)
def pytest_path_probe(): return 'root'

def pytest_runtest_setup(item):
    path = item.path
    path.rename(path.with_suffix('.saved'))
    path.mkdir()
    observed.append(['arbitrary', sorted(item.session.gethookproxy(path).pytest_path_probe())])

def pytest_runtest_makereport(item, call):
    observed.append(['report', call.when])

def pytest_sessionfinish(session):
    path = session.items[0].path
    path.rmdir()
    path.with_suffix('.saved').rename(path)
    Path('relays.json').write_text(json.dumps(observed))
""",
    )
    observed = json.loads(_run(project, "independent", ("test_body.py",)))
    assert observed == [
        ["arbitrary", []],
        ["report", "setup"],
        ["report", "call"],
        ["report", "teardown"],
    ]


@pytest.mark.parametrize("item_kind", ["function", "custom"])
def test_live_item_path_reassignment_changes_executed_conftest_scope(
    tmp_path: Path, item_kind: str
) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "left/test_body.py" if item_kind == "function" else "left/body.relay",
            "def test_body(): pass\n" if item_kind == "function" else "",
        )
        project.write(
            "right/conftest.py",
            "def pytest_runtest_call(item): item.config._path_events.append('right')\n",
        )
        project.write("right/test_other.py", "def test_other(): pass\n")
        project.write(
            "conftest.py",
            """import json
from pathlib import Path

def pytest_configure(config): config._path_events = []
def pytest_runtest_call(item): item.config._path_events.append('root')
def pytest_collection_modifyitems(items):
    first, second = items
    first.path = second.path.parent
    del items[1:]
def pytest_sessionfinish(session):
    Path('relays.json').write_text(json.dumps(session.config._path_events))
"""
            + (_CUSTOM_COLLECTOR if item_kind == "custom" else ""),
        )
        observations.append(json.loads(_run(project, route)))
    assert observations[0] == observations[1] == ["right", "root"]
