"""Session logging resources retain pytest identity and per-test record boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("workers", ["1", "4"])
@pytest.mark.parametrize("boundary", ["collection", "sessionstart", "sessionfinish"])
def test_lifecycle_basic_config_preserves_logging_defaults(
    tmp_path: Path, workers: str, boundary: str
) -> None:
    source = """import logging, pytest
had_handlers = bool(logging.getLogger().handlers)
logging.basicConfig(level=logging.NOTSET)
@pytest.mark.parametrize('index', range(4))
def test_logging(index, caplog):
    logging.debug('hidden debug message')
    assert (had_handlers, logging.getLogger().level, caplog.messages) == (True, logging.WARNING, [])
"""
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_logging.py", source)
        if boundary != "collection":
            project.write(
                "conftest.py",
                "import logging\n"
                f"def pytest_{boundary}(session):\n"
                "    root = logging.getLogger()\n"
                "    had_handlers = bool(root.handlers)\n"
                "    logging.basicConfig(level=logging.NOTSET)\n"
                "    assert (had_handlers, root.level) == (True, logging.WARNING)\n",
            )
        result = project.run_engine(
            engine, "-q", "--capture=no", independent=(f"--workers={workers}",)
        )
        result.expect(0)


CONFTEXT = """import json, logging, pytest
from pathlib import Path
events = []
reports = []
@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item):
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        return (yield)
    finally:
        events.append(['root restored', root.handlers == handlers, root.level == level])
def pytest_runtest_logreport(report):
    reports.append([report.nodeid, report.when, report.outcome,
                    [list(section) for section in report.sections]])
def pytest_sessionfinish():
    Path('observed.json').write_text(json.dumps([events, reports]))
"""


def _compare(tmp_path: Path, source: str, *, errors: bool = False) -> None:
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", CONFTEXT)
        project.write("test_logging.py", source)
        arguments = ["-q", "--capture=sys", "--log-level=WARNING", "--log-format=%(message)s"]
        if engine == "pytest":
            result = project.run_pytest(*arguments, load_xdist=False)
        else:
            result = project.run_vfy(
                "test", "--engine=native", "--workers=1", "--no-cov", *arguments
            )
        assert result.returncode == (1 if errors else 0), result.stdout + result.stderr
        observations[engine] = json.loads((project.root / "observed.json").read_text())
    assert observations["native"] == observations["pytest"]
    events, _ = observations["pytest"]
    assert all(event[1:] == [True, True] for event in events)


def test_logging_handlers_survive_items_with_fresh_records_and_mutable_formatters(
    tmp_path: Path,
) -> None:
    _compare(
        tmp_path,
        """import logging, pytest
from conftest import events
saved = {}
class Reject(logging.Filter):
    def filter(self, record):
        return record.getMessage() != 'blocked'
@pytest.fixture(autouse=True)
def phases(request, caplog):
    logging.warning('setup:' + request.node.name)
    yield
    logging.warning('teardown:' + request.node.name)
def test_first(caplog, pytestconfig):
    handler = caplog.handler
    report = next(h for h in logging.getLogger().handlers
                  if h is not handler and hasattr(h, 'records'))
    assert handler.formatter is report.formatter
    report.formatter.session_marker = 'retained formatter mutation'
    custom = logging.Formatter('CUSTOM:%(message)s')
    handler.setFormatter(custom)
    handler.addFilter(Reject())
    logging.warning('first')
    saved.update(fixture=caplog, handler=handler, report=report,
                 custom=custom, filters=handler.filters, records=caplog.records,
                 stream=handler.stream)
    pytestconfig.option.log_level = 'CRITICAL'
    pytestconfig.option.log_format = 'CHANGED:%(message)s'
def test_second(caplog):
    assert caplog.handler is saved['handler']
    assert caplog.handler.formatter is saved['custom']
    assert caplog.handler.filters is saved['filters']
    assert saved['report'].formatter.session_marker == 'retained formatter mutation'
    assert [r.getMessage() for r in saved['records']] == ['first']
    assert saved['stream'].getvalue() == 'CUSTOM:first\\n'
    assert caplog.records == []
    assert [r.getMessage() for r in caplog.get_records('setup')] == ['setup:test_second']
    logging.warning('blocked')
    logging.warning('second')
    assert caplog.messages == ['second']
    assert caplog.text == 'CUSTOM:second\\n'
    for access in (lambda: saved['fixture'].handler,
                   lambda: saved['fixture'].records,
                   lambda: saved['fixture'].messages,
                   lambda: saved['fixture'].text,
                   lambda: saved['fixture'].get_records('setup')):
        with pytest.raises(KeyError):
            access()
""",
    )


@pytest.mark.parametrize("failure", ["setup", "teardown"])
def test_logging_session_handlers_recover_after_phase_fixture_errors(
    tmp_path: Path, failure: str
) -> None:
    _compare(
        tmp_path,
        f"""import logging, pytest
saved = {{}}
@pytest.fixture(autouse=True)
def logs(request, caplog):
    if 'handler' not in saved:
        saved['handler'] = caplog.handler
    assert caplog.handler is saved['handler']
    logging.warning('setup:' + request.node.name)
    if request.node.name == 'test_first' and {failure!r} == 'setup':
        raise RuntimeError('setup witness')
    yield
    logging.warning('teardown:' + request.node.name)
    if request.node.name == 'test_first' and {failure!r} == 'teardown':
        raise RuntimeError('teardown witness')
def test_first(caplog):
    logging.warning('first')
def test_second(caplog):
    assert caplog.handler is saved['handler']
    assert [r.getMessage() for r in caplog.get_records('setup')] == ['setup:test_second']
    assert caplog.records == []
    logging.warning('second')
    assert caplog.messages == ['second']
""",
        errors=True,
    )


def test_logging_session_handler_cycles_are_collectable_after_main(tmp_path: Path) -> None:
    observed = {}
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_logging.py",
            """import logging, weakref
references = []
def test_cycle(pytestconfig, caplog):
    handler = caplog.handler
    report = next(h for h in logging.getLogger().handlers
                  if h is not handler and hasattr(h, 'records'))
    assert handler.formatter is report.formatter
    handler.formatter.session_config = pytestconfig
    references.extend(weakref.ref(value) for value in (handler, report, handler.formatter))
""",
        )
        project.write(
            "conftest.py",
            """import atexit, gc, json, logging, sys
from pathlib import Path
root = logging.getLogger()
handlers, level = root.handlers[:], root.level

def observe():
    references = sys.modules['test_logging'].references
    gc.collect()
    Path('observed.json').write_text(json.dumps([
        root.handlers == handlers, root.level == level,
        [reference() is None for reference in references]]))

atexit.register(observe)
""",
        )
        result = project.run_engine(
            engine, "-q", "--capture=sys", "--log-level=WARNING", stock=("-p", "no:xdist")
        )
        result.expect(0)
        observed[engine] = json.loads((project.root / "observed.json").read_text())
    assert observed["stock"][:2] == [True, True]
    assert observed["native"] == [True, True, [True] * 3]


def test_empty_phase_record_identity_and_clear_reentry_match_pytest(tmp_path: Path) -> None:
    _compare(
        tmp_path,
        """import logging, pytest
saved = {}
@pytest.fixture(autouse=True)
def phases(caplog):
    yield
    assert caplog.get_records('setup') is saved['setup']
    assert caplog.get_records('call') is saved['call']
    assert caplog.get_records('teardown') is caplog.records
    assert caplog.records == []
def test_records(caplog):
    setup = caplog.get_records('setup')
    assert setup == [] and caplog.get_records('setup') is setup
    future = caplog.get_records('teardown')
    future.append('unstarted phase')
    assert caplog.get_records('teardown') == []
    assert caplog.get_records('teardown') is not future
    records = caplog.records
    assert records is caplog.get_records('call') is caplog.handler.records
    caplog.clear()
    assert caplog.records is records
    class Release:
        def __del__(self):
            logging.warning('clear reentry')
    records.append(Release())
    del records
    caplog.clear()
    assert caplog.messages == ['clear reentry']
    saved.update(setup=setup, call=caplog.records)
""",
    )
