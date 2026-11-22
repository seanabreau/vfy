"""Native terminal consumption preserves stock hook timing and live report order."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _project(root: Path, mode: str, priority: str = "normal") -> VfyProject:
    project = VfyProject(root)
    flags = "" if priority == "normal" else f"{priority}=True"
    prefix = f"{flags}, " if flags else ""
    project.write(
        "conftest.py",
        f"""import json, pytest
from pathlib import Path
mode = {mode!r}
calls, caught = [], []
reentered = False
@pytest.fixture(autouse=True)
def teardown_failure(request):
    yield
    if mode == 'teardown_error' and request.node.name == 'test_first':
        raise RuntimeError('terminal bucket witness')

@pytest.hookimpl({flags})
def pytest_report_teststatus(report, config):
    global reentered
    calls.append([report.nodeid, report.when])
    if report.when != 'call':
        return None
    if mode == 'custom':
        return 'custom', 'Q', ('CUSTOM', {{'yellow': True}})
    if mode == 'reentry' and not reentered:
        reentered = True
        config.hook.pytest_runtest_logreport(report=report)
    if mode == 'bad_arity':
        return 'custom', 'Q'
    if mode == 'bad_word':
        return 'custom', 'Q', ('CUSTOM',)
    return None

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_logreport(report):
    try:
        return (yield)
    except (TypeError, ValueError) as error:
        if mode not in ('bad_arity', 'bad_word'):
            raise
        caught.append([report.when, type(error).__name__])
        return None

@pytest.hookimpl({prefix}specname='pytest_runtest_logreport')
def pytest_runtest_logreport_mutate(report):
    if mode == 'mutation' and report.when == 'call':
        report.outcome = 'failed'

@pytest.hookimpl(wrapper=True, specname='pytest_report_teststatus')
def pytest_report_teststatus_wrapper(report):
    result = yield
    if mode == 'wrapper_mutation' and report.when == 'call':
        report.outcome = 'skipped'
        report.longrepr = (report.location[0], 1, 'Skipped: status wrapper witness')
    return result

def pytest_terminal_summary(terminalreporter):
    stats = terminalreporter.stats
    def row(report):
        return [report.nodeid, report.when, report.outcome]
    buckets = [[category, [row(report) for report in reports]]
               for category, reports in stats.items()]
    ordered = [row(report) for reports in stats.values() for report in reports]
    Path('status.json').write_text(json.dumps({{
        'buckets': buckets, 'ordered': ordered, 'calls': calls, 'caught': caught,
    }}))
""",
    )
    project.write("test_value.py", "def test_first():\n    pass\n\ndef test_second():\n    pass\n")
    return project


@pytest.mark.parametrize("priority", ["tryfirst", "normal", "trylast"])
@pytest.mark.parametrize("mode", ["custom", "mutation"])
def test_terminal_status_respects_provider_and_consumer_registration_priority(
    tmp_path: Path, mode: str, priority: str
) -> None:
    _compare(_project(tmp_path, mode, priority), mode)


@pytest.mark.parametrize(
    "mode", ["reentry", "bad_arity", "bad_word", "wrapper_mutation", "teardown_error"]
)
def test_terminal_status_preserves_callback_mutation_and_error_boundaries(
    tmp_path: Path, mode: str
) -> None:
    _compare(_project(tmp_path, mode), mode)


def _compare(project: VfyProject, mode: str) -> None:
    stock = project.run_engine("stock", "-q")
    assert stock.returncode == (1 if mode == "teardown_error" else 0), stock.stdout + stock.stderr
    expected = json.loads((project.root / "status.json").read_text())
    project.write("stock-status.json", json.dumps(expected))
    project.write("stock.stdout", stock.stdout)
    project.write("stock.stderr", stock.stderr)
    (project.root / "status.json").unlink()
    candidate = project.run_engine("native")
    project.write("candidate.stdout", candidate.stdout)
    project.write("candidate.stderr", candidate.stderr)
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert json.loads((project.root / "status.json").read_text()) == expected


def test_terminal_status_keeps_live_reporter_and_writer_methods(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nconsole_output_style = classic\n")
    project.write("test_value.py", "def test_first(): pass\ndef test_second(): pass\n")
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path
events = []
active = None

def pytest_sessionstart(session):
    terminal = session.config.pluginmanager.get_plugin('terminalreporter')
    add = terminal._add_stats
    write = terminal._tw.write
    flush = terminal.flush
    verbosity = session.config.get_verbosity
    def observed_add(category, items):
        if active is not None: events.append(['add', active, category])
        return add(category, items)
    def observed_write(text, **markup):
        if active is not None: events.append(['write', active, text, markup])
        return write(text, **markup)
    def observed_flush():
        if active is not None: events.append(['flush', active])
        return flush()
    def observed_verbosity(kind=None):
        if active is not None: events.append(['verbosity', active, kind])
        return verbosity(kind)
    terminal._add_stats = observed_add
    terminal._tw.write = observed_write
    terminal.flush = observed_flush
    session.config.get_verbosity = observed_verbosity

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_logreport(report):
    global active
    active = report.when
    try:
        return (yield)
    finally:
        active = None

def pytest_sessionfinish():
    Path('status.json').write_text(json.dumps(events))
""",
    )
    _compare(project, "ordinary")


def test_terminal_color_observes_report_mutation_during_stats_update(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nconsole_output_style = classic\n")
    project.write(
        "test_value.py", "def test_value():\n    import conftest\n    conftest.called = True\n"
    )
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

events = []
changed = None
called = False

def pytest_sessionstart(session):
    terminal = session.config.pluginmanager.get_plugin('terminalreporter')
    set_color = terminal._set_main_color
    write = terminal._tw.write
    def update_color():
        global changed
        set_color()
        if called and changed is None:
            passed = terminal.stats['passed']
            changed = passed[-1]
            changed.outcome = 'failed'
    def observe_write(text, **markup):
        if text == '.' and changed is not None:
            events.append([text, markup, changed.outcome])
            changed.outcome = 'passed'
        return write(text, **markup)
    terminal._set_main_color = update_color
    terminal._tw.write = observe_write

def pytest_sessionfinish():
    Path('status.json').write_text(json.dumps(events))
""",
    )
    stock = project.run_engine("stock", "-q")
    stock.expect(0)
    expected = json.loads((tmp_path / "status.json").read_text())
    assert expected == [[".", {"red": True}, "failed"]]
    (tmp_path / "status.json").unlink()

    candidate = project.run_engine("native", "-q")
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert json.loads((tmp_path / "status.json").read_text()) == expected


def test_terminal_status_observes_monitor_and_late_provider_registration(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_first(): pass\ndef test_second(): pass\n")
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path
events = []
manager = None
undo = None

class LateProvider:
    @pytest.hookimpl(tryfirst=True)
    def pytest_report_teststatus(self, report):
        events.append(['late', report.nodeid, report.when])
        if report.when == 'call': return 'custom', 'Q', 'CUSTOM'

def pytest_sessionstart(session):
    global manager, undo
    manager = session.config.pluginmanager
    def before(name, implementations, kwargs):
        if name == 'pytest_report_teststatus':
            events.append(['before', kwargs['report'].nodeid, kwargs['report'].when])
    def after(outcome, name, implementations, kwargs):
        if name == 'pytest_report_teststatus':
            events.append(['after', kwargs['report'].nodeid, kwargs['report'].when])
    undo = manager.add_hookcall_monitoring(before, after)

def pytest_runtest_logfinish(nodeid):
    if nodeid.endswith('test_first'):
        undo()
        manager.register(LateProvider(), 'late-provider')

def pytest_sessionfinish():
    Path('status.json').write_text(json.dumps(events))
""",
    )
    _compare(project, "ordinary")


@pytest.mark.parametrize("replacement", ["class", "instance"])
def test_terminal_status_observes_replaced_add_stats_implementation(
    tmp_path: Path, replacement: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_value(): pass\n")
    project.write(
        "conftest.py",
        f"""import json, types
from pathlib import Path

events = []
original = None
original_bound = None

def replacement(self, category, items):
    events.append([category, len(items)])
    return original_bound(category, items)

def pytest_sessionstart(session):
    global original, original_bound
    terminal = session.config.pluginmanager.get_plugin('terminalreporter')
    cls = type(terminal)
    original = cls._add_stats
    original_bound = terminal._add_stats
    if {replacement!r} == 'class':
        cls._add_stats = replacement
    else:
        terminal._add_stats = types.MethodType(replacement, terminal)

def pytest_sessionfinish():
    Path('status.json').write_text(json.dumps(events))
""",
    )
    _compare(project, "ordinary")
    events = json.loads((project.root / "status.json").read_text())
    assert any(category == "passed" and count == 1 for category, count in events)


def test_terminal_status_keeps_live_stats_mapping_callbacks(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_value(): pass\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

events = []
names = {'setdefault': [], 'extend': []}

class Bucket(list):
    def __getattribute__(self, name):
        if name == 'extend':
            names['extend'].append(name)
        return super().__getattribute__(name)
    def extend(self, items):
        events.append(['extend', len(items)])
        return super().extend(items)

class Stats(dict):
    def __getattribute__(self, name):
        if name == 'setdefault':
            names['setdefault'].append(name)
        return super().__getattribute__(name)
    def __contains__(self, category):
        events.append(['contains', category])
        return super().__contains__(category)
    def setdefault(self, category, default=None):
        events.append(['setdefault', category])
        return super().setdefault(category, Bucket(default))

def pytest_sessionstart(session):
    terminal = session.config.pluginmanager.get_plugin('terminalreporter')
    terminal.stats = Stats((key, Bucket(value)) for key, value in terminal.stats.items())

def pytest_sessionfinish():
    Path('status.json').write_text(json.dumps({
        'events': events,
        'names': {key: [len(values), all(value is values[0] for value in values)]
                  for key, values in names.items()},
    }))
""",
    )
    _compare(project, "ordinary")
    observed = json.loads((project.root / "status.json").read_text())
    events = observed["events"]
    assert observed["names"] == {"setdefault": [3, True], "extend": [3, True]}
    assert ["contains", "passed"] in events
    assert ["setdefault", "passed"] in events
    assert ["extend", 1] in events
