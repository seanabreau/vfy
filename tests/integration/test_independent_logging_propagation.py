"""Native logging handlers preserve the caller's standard-library propagation."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_logger_creation_and_propagation_change_during_call_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import logging
existing = logging.getLogger('capture-existing')
existing.propagate = False
""",
    )
    project.write(
        "test_logging.py",
        """import json, logging
from pathlib import Path

def test_logging(caplog):
    existing = logging.getLogger('capture-existing')
    created = logging.getLogger('capture-created-during-call')
    created.propagate = False
    with caplog.at_level(logging.INFO):
        existing.info('existing')
        created.info('created')
        existing.propagate = True
        existing.info('changed')
        existing.propagate = False
    Path('messages.json').write_text(json.dumps(caplog.messages))
""",
    )
    env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    stock = project.run_python("-m", "pytest", "-q", env=env)
    stock.expect(0)
    expected = json.loads((tmp_path / "messages.json").read_text())
    assert expected == ["existing", "changed", "changed"]
    (tmp_path / "messages.json").unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=env)
    candidate.expect(0)
    assert json.loads((tmp_path / "messages.json").read_text()) == expected


@pytest.mark.parametrize("propagate", [False, True])
@pytest.mark.parametrize("creation", ["conftest", "setup"])
def test_live_and_fixture_logging_preserve_existing_logger_propagation(
    tmp_path: Path, propagate: bool, creation: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"""import json, logging, sys
from pathlib import Path
import pytest

logger = logging.getLogger('application.retained') if {creation!r} == 'conftest' else None
if logger is not None:
    logger.propagate = {propagate!r}
    logger.setLevel(logging.INFO)

@pytest.fixture
def retained():
    global logger
    if logger is None:
        logger = logging.getLogger('application.retained')
        logger.propagate = {propagate!r}
        logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    logger.addHandler(handler)
    logger.info('BOUND:setup')
    yield logger
    logger.info('BOUND:teardown')
    logger.removeHandler(handler)

def pytest_runtest_logreport(report):
    with Path('phases.jsonl').open('a') as stream:
        stream.write(json.dumps([report.nodeid, report.when, report.outcome,
                                report.capstdout, report.capstderr]) + '\\n')
""",
    )
    project.write(
        "test_logging.py",
        """import json, logging
from pathlib import Path

def test_body(retained, caplog):
    retained.info('BOUND:call')
    logging.getLogger('application.root').warning('ROOT:call')
    Path('messages.json').write_text(json.dumps(caplog.messages))
    assert caplog.messages == ['BOUND:call', 'ROOT:call']
""",
    )
    arguments = ["--log-cli-level=INFO", "--log-cli-format=%(message)s"]
    stock = project.run_python("-m", "pytest", "-q", *arguments)
    stock.expect(0)
    expected_messages = json.loads(tmp_path.joinpath("messages.json").read_text())
    phases = tmp_path / "phases.jsonl"
    expected_phases = [json.loads(line) for line in phases.read_text().splitlines()]
    phases.rename(tmp_path / "stock-phases.jsonl")
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    candidate.expect(0)
    assert json.loads(tmp_path.joinpath("messages.json").read_text()) == expected_messages
    assert [json.loads(line) for line in phases.read_text().splitlines()] == expected_phases
    for marker in ("BOUND:", "ROOT:"):
        assert sorted(re.findall(re.escape(marker) + r"[^\r\n]*", candidate.stdout)) == sorted(
            re.findall(re.escape(marker) + r"[^\r\n]*", stock.stdout)
        )


def test_live_handler_identity_and_level_survive_test_configuration_changes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, logging, weakref
from pathlib import Path
import pytest

live = None
events = []

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtestloop(session):
    root = logging.getLogger()
    before = root.handlers[:]
    level = root.level
    try:
        yield
    finally:
        events.append(['restored', root.handlers == before, root.level == level])
        Path('handler-events.json').write_text(json.dumps(events))

def pytest_runtest_setup(item):
    global live
    handlers = [handler for handler in logging.getLogger().handlers
                if handler.level == logging.INFO]
    assert len(handlers) == 1
    if live is None:
        live = weakref.ref(handlers[0])
    events.append(['handler', handlers[0] is live(), handlers[0].level])
""",
    )
    project.write(
        "test_logging.py",
        """import logging, threading

def emit(message):
    thread = threading.Thread(target=logging.getLogger('live.stateful').info, args=(message,))
    thread.start()
    thread.join()

def test_first(pytestconfig):
    emit('IDENTITY:first')
    pytestconfig.option.log_cli_level = 'CRITICAL'

def test_second():
    emit('IDENTITY:second')
""",
    )
    arguments = ["--log-cli-level=INFO", "--log-cli-format=%(message)s"]
    stock = project.run_python("-m", "pytest", "-q", *arguments)
    stock.expect(0)
    expected = json.loads(tmp_path.joinpath("handler-events.json").read_text())
    assert expected == [["handler", True, 20], ["handler", True, 20], ["restored", True, True]]
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    candidate.expect(0)
    assert json.loads(tmp_path.joinpath("handler-events.json").read_text()) == expected
    assert sorted(re.findall(r"IDENTITY:[^\r\n]*", candidate.stdout)) == [
        "IDENTITY:first",
        "IDENTITY:second",
    ]


_RESTORE = """
import atexit, json, logging
from pathlib import Path

_root = logging.getLogger()
_handlers, _level = _root.handlers[:], _root.level

def _record_restoration():
    Path('restored.json').write_text(json.dumps(
        [_root.handlers == _handlers, _root.level == _level]))

atexit.register(_record_restoration)
"""


@pytest.mark.parametrize("failure", ["interrupt", "outer-wrapper"])
def test_live_loop_exception_removes_handlers_and_allows_rerun(
    tmp_path: Path, failure: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        _RESTORE
        + """import os
import pytest

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtestloop(session):
    result = yield
    if os.environ.get('LOG_FAILURE') == 'outer-wrapper':
        raise RuntimeError('outer logging wrapper witness')
    return result
""",
    )
    project.write(
        "test_logging.py",
        """import logging, os

def test_body():
    logging.warning('RECOVERY:live')
    if os.environ.get('LOG_FAILURE') == 'interrupt':
        raise KeyboardInterrupt()
""",
    )
    arguments = ["-q", "--log-cli-level=INFO"]
    observed = {}
    for engine in ("stock", "native"):
        runs = []
        for mode in (failure, None):
            result = project.run_engine(engine, *arguments, env={"LOG_FAILURE": mode})
            restored = json.loads((tmp_path / "restored.json").read_text())
            runs.append((result.returncode, restored))
        observed[engine] = runs
    assert observed["stock"][0][0] != 0
    assert observed["stock"][1] == (0, [True, True])
    assert observed["native"] == observed["stock"]


def test_logger_created_during_live_iteration_restores_handlers_and_allows_rerun(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_body.py", "def test_body(): pass\n")
    project.write(
        "conftest.py",
        _RESTORE
        + """import os

logger = logging.getLogger('iteration-mutator')
logger.propagate = False
if os.environ.get('MUTATE') == '1':
    original = logger.addHandler
    seen = []
    def trigger(handler):
        original(handler)
        if not seen:
            seen.append(True)
            logging.getLogger('created-during-iteration')
    logger.addHandler = trigger
""",
    )
    env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    first = project.run_engine("native", "-q", env={**env, "MUTATE": "1"})
    first.expect(3)
    assert "dictionary changed size during iteration" in first.stdout + first.stderr
    assert json.loads((tmp_path / "restored.json").read_text()) == [True, True]
    second = project.run_engine("native", "-q", env={**env, "MUTATE": "0"})
    second.expect(0)
    assert json.loads((tmp_path / "restored.json").read_text()) == [True, True]


def test_logger_lifetime_during_level_change_matches_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import logging
import pytest

@pytest.fixture
def logger_lifetime(monkeypatch):
    events = []
    root = logging.getLogger()
    name = 'logger-lifetime-at-level-change'
    # Keep at least one entry for the second handler's traversal after removal.
    logging.getLogger('logger-lifetime-anchor')
    class Witness(logging.Logger):
        def __del__(self):
            events.append('logger finalized')
    root.manager.loggerDict[name] = Witness(name)
    original_add = root.addHandler
    original_level = root.setLevel
    count = 0
    armed = False
    def add_handler(handler):
        nonlocal count, armed
        if hasattr(handler, 'records'):
            count += 1
            if count <= 2:
                events.append('handler ' + str(count))
            if count == 1:
                # Arm only on call-phase entry, after setup cleanup is complete.
                armed = True
                logger = root.manager.loggerDict.pop(name)
                root.manager.loggerDict[name] = logger
        original_add(handler)
    def set_level(level):
        if armed and name in root.manager.loggerDict:
            events.append('setLevel start')
            root.manager.loggerDict.pop(name)
            events.append('setLevel end')
        return original_level(level)
    monkeypatch.setattr(root, 'addHandler', add_handler)
    monkeypatch.setattr(root, 'setLevel', set_level)
    return events
""",
    )
    project.write(
        "test_body.py",
        """import json
from pathlib import Path

def test_body(logger_lifetime):
    Path('logging-lifetime.json').write_text(json.dumps(logger_lifetime))
""",
    )
    env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    stock = project.run_python("-m", "pytest", "-q", "--log-level=INFO", env=env)
    stock.expect(0)
    expected = json.loads((tmp_path / "logging-lifetime.json").read_text())
    assert expected == [
        "handler 1",
        "setLevel start",
        "setLevel end",
        "logger finalized",
        "handler 2",
    ]
    (tmp_path / "logging-lifetime.json").unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--workers=1",
        "--no-cov",
        "-q",
        "--log-level=INFO",
        env=env,
    )
    candidate.expect(0)
    assert json.loads((tmp_path / "logging-lifetime.json").read_text()) == expected


@pytest.mark.parametrize("body_fails", [False, True])
def test_logging_cleanup_preserves_nested_exception_context(
    tmp_path: Path, body_fails: bool
) -> None:
    conftest = """import json, logging, sys
from pathlib import Path

body_error = ValueError('body witness')
report_error = RuntimeError('report cleanup witness')
caplog_error = RuntimeError('caplog cleanup witness')
events = []

def arm(caplog):
    root = logging.getLogger()
    original = root.removeHandler
    caplog_handler = caplog.handler
    report_handler = next(handler for handler in root.handlers
                          if hasattr(handler, 'records') and handler is not caplog_handler)
    def remove(handler):
        if handler is report_handler:
            active = sys.exception()
            events.append(['report', active is None, active is body_error])
            original(handler)
            raise report_error
        if handler is caplog_handler:
            active = sys.exception()
            events.append(['caplog', active is report_error])
            root.removeHandler = original
            original(handler)
            raise caplog_error
        return original(handler)
    root.removeHandler = remove

def pytest_runtest_makereport(item, call):
    if call.when == 'call' and call.excinfo is not None:
        error = call.excinfo.value
        events.append(['result', error is caplog_error,
                       error.__context__ is report_error,
                       report_error.__context__ is body_error,
                       report_error.__context__ is None])

def pytest_unconfigure():
    Path('exception-context.json').write_text(json.dumps(events))
"""
    source = """from conftest import arm, body_error

def test_body(caplog):
    arm(caplog)
    if BODY_FAILS:
        raise body_error
""".replace("BODY_FAILS", repr(body_fails))
    observations = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", conftest)
        project.write("test_body.py", source)
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = (
            project.run_python("-m", "pytest", "-q", env=env)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=env)
        )
        evidence = project.root / "exception-context.json"
        assert evidence.is_file(), result.stdout + result.stderr
        assert result.returncode != 0, result.stdout + result.stderr
        observations.append(json.loads(evidence.read_text()))
    assert observations[0] == [
        ["report", not body_fails, body_fails],
        ["caplog", True],
        ["result", True, True, body_fails, not body_fails],
    ]
    assert observations[1] == observations[0]
