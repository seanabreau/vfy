"""Actual capture declarations retain fixture requests and stock IO behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

CAPTURE_NAMES = ("capsys", "capfd", "capsysbinary", "capfdbinary", "capteesys")


def _write_tee_project(project: VfyProject) -> None:
    project.write(
        "conftest.py",
        """import json, logging, sys, pytest
from pathlib import Path
reads, reports, saved = [], [], []

def read(capture, label):
    value = capture.readouterr()
    assert isinstance(value.out, str) and isinstance(value.err, str)
    reads.append([label, value.out, value.err])
    return value

@pytest.fixture(autouse=True)
def closed_read(request):
    def after_cleanup():
        capture = saved.pop()
        value = read(capture, 'after cleanup')
        assert value.out.endswith('teardown pending\\n')
        assert read(capture, 'after cleanup consumed') == ('', '')
    request.node.addfinalizer(after_cleanup)

@pytest.fixture
def stream(capteesys, request):
    saved.append(capteesys)
    assert capteesys.request.fixturename == 'capteesys'
    assert capteesys.request.scope == 'function'
    assert request.getfixturevalue('capteesys') is capteesys
    print('setup read')
    read(capteesys, 'setup')
    print('setup pending')
    yield capteesys
    print('teardown read')
    read(capteesys, 'teardown')
    print('teardown pending')

def pytest_runtest_logreport(report):
    reports.append([report.nodeid, report.when, report.outcome,
                    report.sections, report.capstdout, report.capstderr])

def pytest_sessionfinish():
    Path('observations.json').write_text(json.dumps([reads, reports]))
""",
    )
    project.write(
        "test_tee.py",
        """import logging, os, sys
from conftest import read

def test_tee(stream, caplog):
    assert 'setup pending' in read(stream, 'call start').out
    print('live text')
    print('live error', file=sys.stderr)
    sys.stdout.buffer.write(b'raw buffer\\xe2\\x82\\xac')
    os.write(1, b'raw descriptor\\n')
    value = read(stream, 'call')
    assert value.out == 'live text\\nraw buffer€'
    assert value.err == 'live error\\n'
    assert read(stream, 'consumed') == ('', '')
    logging.warning('logging callback')
    assert caplog.messages == ['logging callback']
    with stream.disabled():
        print('disabled before')
        with stream.disabled():
            print('disabled nested', file=sys.stderr)
        print('disabled after')
    print('call pending')

def test_reuse(stream):
    assert 'setup pending' in read(stream, 'second call').out
""",
    )


@pytest.mark.parametrize("capture", ["fd", "sys", "no", "tee-sys"])
def test_tee_phases_raw_io_logging_and_cleanup_match_pytest(tmp_path: Path, capture: str) -> None:
    project = VfyProject(tmp_path)
    _write_tee_project(project)
    arguments = [f"--capture={capture}", "--log-cli-level=WARNING"]
    stock = project.run_pytest("-q", *arguments, load_xdist=False)
    stock.expect(0)
    observations = tmp_path / "observations.json"
    expected = json.loads(observations.read_text())
    observations.unlink()
    candidate = project.run_engine("native", *arguments)
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert json.loads(observations.read_text()) == expected
    for marker in ["live text", "disabled before", "disabled after"]:
        assert candidate.stdout.count(marker) == stock.stdout.count(marker)
    assert candidate.stderr.count("disabled nested") == stock.stderr.count("disabled nested")


def _write_reverse_conflict_project(project: VfyProject, first: str, second: str) -> None:
    project.write(
        "test_conflict.py",
        f"""import json, pytest
from pathlib import Path

def test_conflict(request):
    capture = request.getfixturevalue({first!r})
    print('before', end='')
    with pytest.raises(pytest.FixtureLookupError) as error:
        request.getfixturevalue({second!r})
    assert error.value.msg == 'cannot use {second} and {first} at the same time'
    assert request.getfixturevalue({first!r}) is capture
    print('after', end='')
    result = capture.readouterr()
    assert result.out in ('beforeafter', b'beforeafter') and not result.err
    Path('conflict.json').write_text(json.dumps([type(error.value).__name__, error.value.msg]))

def test_recovery(request):
    capture = request.getfixturevalue({second!r})
    print('recovered', end='')
    assert capture.readouterr().out in ('recovered', b'recovered')
""",
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [(first, second) for first in CAPTURE_NAMES for second in CAPTURE_NAMES if first != second],
)
def test_capture_conflicts_preserve_existing_resource_and_recover(
    tmp_path: Path, first: str, second: str
) -> None:
    project = VfyProject(tmp_path)
    _write_reverse_conflict_project(project, first, second)
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    observation = tmp_path / "conflict.json"
    expected = observation.read_text()
    observation.unlink()
    candidate = project.run_engine("native")
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert observation.read_text() == expected


def _write_callback_project(project: VfyProject, kind: str) -> None:
    project.write(
        "test_callbacks.py",
        f"""import json, sys
from pathlib import Path

def test_callback(request, monkeypatch):
    original = sys.stdout
    holder, reads = [], []
    class Buffer:
        def write(self, data):
            value = holder[0].readouterr()
            reads.append([value.out.hex(), value.err.hex()])
            return original.buffer.write(data)
        def flush(self):
            original.buffer.flush()
    class Stream:
        buffer = Buffer()
        def write(self, data):
            return original.write(data)
        def flush(self):
            original.flush()
    monkeypatch.setattr(sys, 'stdout', Stream())
    capture = request.getfixturevalue({kind!r})
    holder.append(capture)
    sys.stdout.buffer.write(b'pending\\x00\\xff')
    capture.close()
    value = capture.readouterr()
    assert value.out == b'pending\\x00\\xff'
    assert capture.readouterr() == (b'', b'')
    Path('callback.json').write_text(json.dumps([reads, value.out.hex()]))
""",
    )


def _write_history_project(project: VfyProject, kind: str, history: str) -> None:
    if history != "blocked":
        operations = {
            "unblocked": "assert manager.unblock('capture')",
            "unregistered": "assert manager.unregister(name='capture') is not None",
            "late_blocked": "manager.set_blocked('capture')",
        }
        project.write(
            "conftest.py",
            f"""def pytest_configure(config):
    manager = config.pluginmanager
    {operations[history]}
    assert manager.getplugin('capture') is None
""",
        )
    available = history in ("unregistered", "late_blocked")
    project.write(
        "test_history.py",
        f"""import json, pytest
from pathlib import Path

def test_history(request):
    if {available!r}:
        capture = request.getfixturevalue({kind!r})
        assert capture.request.fixturename == {kind!r}
        print('retained declaration', end='')
        assert capture.readouterr().out in ('retained declaration', b'retained declaration')
    else:
        with pytest.raises(pytest.FixtureLookupError):
            request.getfixturevalue({kind!r})
    Path('history.json').write_text(json.dumps([{kind!r}, {history!r}, {available!r}]))
""",
    )


def _write_wrapper_project(project: VfyProject, kind: str) -> None:
    project.write(
        "conftest.py",
        f"""import json, pytest
from pathlib import Path
reads, saved, reports = [], [], []

def record(capture, label):
    value = capture.readouterr()
    reads.append([label, repr(value.out), repr(value.err)])
    return value

@pytest.fixture(autouse=True)
def after_cleanup(request):
    def finalizer():
        capture = saved.pop()
        value = record(capture, 'after cleanup')
        assert value.out in ('teardown pending\\n', b'teardown pending\\n')
    request.node.addfinalizer(finalizer)

@pytest.fixture
def {kind}({kind}, request):
    capture = {kind}
    saved.append(capture)
    assert capture.request is not request
    assert capture.request.fixturename == {kind!r}
    stack = capture.request._get_fixturestack()
    assert [definition.argname for definition in stack][-2:] == [{kind!r}, {kind!r}]
    assert stack[-1].scope == 'function'
    print('setup pending')
    yield capture
    print('teardown pending')

def pytest_runtest_logreport(report):
    reports.append([report.when, report.outcome, report.sections])

def pytest_sessionfinish():
    Path('wrapper.json').write_text(json.dumps([reads, reports]))
""",
    )
    project.write(
        "test_wrapper.py",
        f"""from conftest import record

def test_wrapper({kind}):
    assert record({kind}, 'call setup').out in ('setup pending\\n', b'setup pending\\n')
    print('call read')
    assert record({kind}, 'call').out in ('call read\\n', b'call read\\n')
""",
    )


@pytest.mark.parametrize("kind", CAPTURE_NAMES)
def test_overridden_capture_returns_actual_provider_request_without_duplicate_phase_io(
    tmp_path: Path, kind: str
) -> None:
    project = VfyProject(tmp_path)
    _write_wrapper_project(project, kind)
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    observation = tmp_path / "wrapper.json"
    expected = observation.read_text()
    observation.unlink()
    candidate = project.run_engine("native")
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert observation.read_text() == expected


def _write_reservation_project(project: VfyProject) -> None:
    project.write(
        "test_reservation.py",
        """import json, sys, pytest
from pathlib import Path
PAIRS=[('capsys','capfd'),('capsysbinary','capfdbinary'),('capteesys','capsys')]
def record(value):
    with Path('observations.jsonl').open('a') as stream:
        stream.write(json.dumps(value)+'\\n')
def conflict(request,first,second):
    with pytest.raises(pytest.FixtureLookupError) as error:
        request.getfixturevalue(second)
    assert error.value.msg == f'cannot use {second} and {first} at the same time'
    return error.value.msg
@pytest.mark.parametrize('first,second',PAIRS)
def test_manual_close(request,first,second):
    capture=request.getfixturevalue(first)
    print('pending',end='')
    capture.close()
    assert request.getfixturevalue(first) is capture
    message=conflict(request,first,second)
    assert capture.readouterr().out in ('pending',b'pending')
    record({'stage':'manual-close','first':first,'message':message})
@pytest.mark.parametrize('first,second',PAIRS)
def test_disabled_reservation(request,first,second):
    capture=request.getfixturevalue(first)
    with capture.disabled():
        with capture.disabled():
            message=conflict(request,first,second)
    print('resumed',end='')
    assert capture.readouterr().out in ('resumed',b'resumed')
    record({'stage':'nested-disabled','first':first,'message':message})
@pytest.mark.parametrize('first,second',PAIRS)
def test_close_inside_disabled_stays_closed(request,first,second):
    capture=request.getfixturevalue(first)
    with capture.disabled():
        capture.close()
    print('after close',end='')
    value=capture.readouterr().out
    assert value in ('',b'')
    message=conflict(request,first,second)
    record({'stage':'close-inside-disabled','first':first,'message':message,'read':repr(value)})
@pytest.mark.parametrize('first,second',PAIRS)
def test_replay_or_tee_callback(request,monkeypatch,first,second):
    original=sys.stdout
    active=[False]
    reads=[]
    def observe():
        if active[0]:
            active[0]=False
            reads.append(conflict(request,first,second))
    class Buffer:
        def write(self,data):
            observe()
            return original.buffer.write(data)
        def flush(self):
            original.buffer.flush()
    class Stream:
        buffer=Buffer()
        def write(self,text):
            observe()
            return original.write(text)
        def flush(self):
            original.flush()
    monkeypatch.setattr(sys,'stdout',Stream())
    capture=request.getfixturevalue(first)
    if first=='capteesys':
        active[0]=True
        print('tee callback',end='')
    else:
        print('replay callback',end='')
        active[0]=True
        capture.close()
    assert reads == [f'cannot use {second} and {first} at the same time']
    record({'stage':'replay-or-tee-callback','first':first,'messages':reads})

@pytest.mark.parametrize('first,second',PAIRS[:2])
def test_replay_failure_preserves_reservation(request,monkeypatch,first,second):
    original=sys.stdout
    fail=[True]
    def observe():
        if fail[0]:
            fail[0]=False
            raise RuntimeError('replay failed')
    class Buffer:
        def write(self,data):
            observe()
            return original.buffer.write(data)
        def flush(self):
            original.buffer.flush()
    class Stream:
        buffer=Buffer()
        def write(self,text):
            observe()
            return original.write(text)
        def flush(self):
            original.flush()
    monkeypatch.setattr(sys,'stdout',Stream())
    capture=request.getfixturevalue(first)
    print('drained before failed replay',end='')
    with pytest.raises(RuntimeError,match='replay failed'):
        capture.close()
    assert request.getfixturevalue(first) is capture
    message=conflict(request,first,second)
    value=capture.readouterr().out
    assert value in ('',b'')
    record({'stage':'replay-failure','first':first,'message':message,'read':repr(value)})
""",
    )
