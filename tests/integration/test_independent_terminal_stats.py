"""Terminal statistics retain live reports through their status consumers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_OBSERVER = """import json, pytest
from pathlib import Path
events, seen = [], []
session_ref = None
initial = None

def counts(stats): return [[key,len(value)] for key,value in stats.items()]
def row(report):
    return [report.when,report.outcome,getattr(getattr(report,'context',None),'msg',None)]

def pytest_sessionstart(session):
    global session_ref, initial
    session_ref=session
    terminal=session.config.pluginmanager.get_plugin('terminalreporter')
    initial=terminal.stats
    original=terminal._add_stats
    def add(category,items):
        assert items[0] is seen[-1]
        events.append(['add before',category,counts(terminal.stats)])
        result=original(category,items)
        events.append(['add after',category,counts(terminal.stats)])
        return result
    terminal._add_stats=add

@pytest.hookimpl(wrapper=True,tryfirst=True)
def pytest_runtest_logreport(report):
    seen.append(report)
    terminal=session_ref.config.pluginmanager.get_plugin('terminalreporter')
    events.append(['report before',row(report),counts(terminal.stats)])
    result=yield
    events.append(['report after',row(report),counts(terminal.stats)])
    return result

@pytest.hookimpl(tryfirst=True)
def pytest_report_teststatus(report):
    if MODE=='empty' and report.when=='call': return '', '', ''
    if MODE=='tuple' and report.when=='call':
        class Word(tuple):
            def __new__(cls): return super().__new__(cls,('',None))
            def __iter__(self):
                events.append(['word iter',row(report)])
                yield 'ACTUAL'
                yield {}
        return 'typed', '', Word()

@pytest.hookimpl(wrapper=True,specname='pytest_report_teststatus')
def pytest_report_teststatus_observe(report,config):
    terminal=config.pluginmanager.get_plugin('terminalreporter')
    events.append(['status before',row(report),counts(terminal.stats)])
    result=yield
    if MODE=='wrapped': result=('observed '+result[0],result[1],result[2])
    events.append(['status after',row(report),result[0],counts(terminal.stats)])
    return result

@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session):
    terminal=session.config.pluginmanager.get_plugin('terminalreporter')
    assert terminal.stats is initial and type(initial) is dict
    assert all(any(report is original for original in seen)
               for bucket in initial.values() for report in bucket)
    assert sum(map(len,initial.values()))==len(seen)
    Path('observed.json').write_text(json.dumps({
        'events':events,
        'buckets':[[key,[row(r) for r in bucket]] for key,bucket in initial.items()],
        'verbosity':terminal.verbosity,'progress':terminal.reported_progress,
        'failed':session.testsfailed,'color':terminal._main_color,'known':terminal._known_types,
    }))
"""

_BODY = """import pytest
def test_children(subtests):
    with subtests.test('passed'): pass
    with subtests.test('skipped'): pytest.skip('child')
    with subtests.test('failed'): assert False, 'actual child failure'
    from pathlib import Path
    Path('continued').touch()
"""


def _run(project: VfyProject, engine: str, *arguments: str, status: int = 0) -> object:
    expected = None
    if engine == "native":
        expected = _run(project, "stock", *arguments, status=status)
        project.write("stock-observed.json", json.dumps(expected))
        (project.root / "observed.json").unlink()
    result = project.run_engine(engine, *arguments)
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    project.write(f"{engine}.status", str(result.returncode))
    assert result.returncode == status, result.stdout + result.stderr
    observed = json.loads((project.root / "observed.json").read_text())
    if engine == "native":
        assert observed == expected
    return observed


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("mode", ["ordinary", "empty", "wrapped", "tuple"])
@pytest.mark.parametrize("verbosity", ["-qq", "-q", "-v"])
def test_terminal_live_stats_consume_subreports_once_before_quiet_output(
    tmp_path: Path, engine: str, mode: str, verbosity: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", f"MODE={mode!r}\n" + _OBSERVER)
    project.write("test_children.py", _BODY)
    result = _run(project, engine, verbosity, status=1)
    assert (project.root / "continued").exists()
    assert isinstance(result, dict)
    events = result["events"]
    assert len([event for event in events if event[0] == "add before"]) == 6
    assert len([event for event in events if event[0] == "status before"]) == 6


@requires_native_pytest
def test_status_word_survives_writer_error_until_its_hook_wrapper_returns(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_value.py", "def test_value():\n    pass\n")
    project.write(
        "conftest.py",
        """import gc, json, pytest
from pathlib import Path
events = []

class WriteError(Exception): pass

class Word(str):
    def __del__(self): events.append('released')

class Status(tuple):
    def __new__(cls): return super().__new__(cls, ('', None))
    def __iter__(self):
        yield Word('CUSTOM')
        yield {}

def pytest_sessionstart(session):
    writer = session.config.pluginmanager.get_plugin('terminalreporter')._tw
    original = writer.write
    def write(text, **markup):
        if text == 'Q':
            events.append('write')
            raise WriteError('actual status writer failure')
        return original(text, **markup)
    writer.write = write

@pytest.hookimpl(tryfirst=True)
def pytest_report_teststatus(report):
    if report.when == 'call': return 'custom', 'Q', Status()

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_logreport(report):
    try:
        return (yield)
    except WriteError:
        events.append('caught')
        assert events == ['write', 'caught']

def pytest_sessionfinish():
    gc.collect()
    Path('observed.json').write_text(json.dumps({'events': events}))
""",
    )
    observed = _run(project, "independent", "-q")
    assert observed == {"events": ["write", "caught", "released"]}


@requires_native_pytest
def test_status_tuple_subclass_is_released_before_stats_and_output(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_value(): pass\n")
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path
events = []

class Status(tuple):
    def __new__(cls): return super().__new__(cls, ('custom', 'Q', 'CUSTOM'))
    def __iter__(self):
        events.append('iterate')
        return super().__iter__()
    def __del__(self): events.append('result released')

def pytest_sessionstart(session):
    terminal = session.config.pluginmanager.get_plugin('terminalreporter')
    add = terminal._add_stats
    write = terminal._tw.write
    def observed_add(category, items):
        if category == 'custom': events.append('stats')
        return add(category, items)
    def observed_write(text, **markup):
        if text == 'Q': events.append('write')
        return write(text, **markup)
    terminal._add_stats = observed_add
    terminal._tw.write = observed_write

@pytest.hookimpl(tryfirst=True)
def pytest_report_teststatus(report):
    if report.when == 'call': return Status()

def pytest_sessionfinish():
    Path('observed.json').write_text(json.dumps({'events': events}))
""",
    )
    observed = _run(project, "independent", "-q")
    assert observed == {"events": ["iterate", "result released", "stats", "write"]}


@requires_native_pytest
def test_status_hook_error_retains_user_traceback_and_report_until_release(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_value(): pass\n")
    project.write(
        "conftest.py",
        """import gc, json, pytest
from pathlib import Path
events, retained = [], []

class StatusError(Exception): pass
class Resource:
    def __del__(self): events.append('released')

@pytest.hookimpl(tryfirst=True)
def pytest_report_teststatus(report):
    if report.when == 'call':
        resource = Resource()
        raise StatusError('status hook witness')

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_logreport(report):
    try:
        return (yield)
    except StatusError as error:
        retained.append(error)
        trace = error.__traceback__
        user_frames = []
        original_report = False
        while trace is not None:
            frame = trace.tb_frame
            original_report |= any(value is report for value in frame.f_locals.values())
            if (frame.f_code.co_filename == __file__
                    and frame.f_code.co_name == 'pytest_report_teststatus'):
                user_frames.append([frame.f_code.co_name, trace.tb_lineno])
            trace = trace.tb_next
        events.append(['caught', str(error), original_report, user_frames])

def pytest_sessionfinish():
    assert len(events) == 1
    retained.clear()
    gc.collect()
    Path('observed.json').write_text(json.dumps({'events': events}))
""",
    )
    observed = _run(project, "independent", "-q")
    assert isinstance(observed, dict)
    assert observed["events"][0][:3] == ["caught", "status hook witness", True]
    assert len(observed["events"][0][3]) == 1
    assert observed["events"][1:] == ["released"]
