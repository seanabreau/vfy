"""Public logging-scope differentials against the installed pytest oracle."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native logging requires Unix")


def _compare(
    tmp_path: Path,
    source: str,
    *,
    live_logs: bool = False,
    conftest: str | None = None,
) -> dict[str, object]:
    observed: dict[str, dict[str, object]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "pytest.ini",
            "[pytest]\nlog_level=INFO\nlog_format=%(message)s\n"
            + (
                "log_cli=true\nlog_cli_level=INFO\nlog_cli_format=%(message)s\n"
                if live_logs
                else ""
            ),
        )
        if conftest is not None:
            project.write("conftest.py", conftest)
        project.write("test_scopes.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--junitxml=result.xml",
            "-o",
            "junit_logging=all",
        )
        evidence = project.root / "evidence.json"
        assert evidence.exists(), result.stdout + result.stderr
        observed[engine] = {
            "status": result.returncode,
            "evidence": json.loads(evidence.read_text()),
            "cases": [
                (
                    case.get("name"),
                    [
                        (child.tag, child.get("message"))
                        for child in case
                        if child.tag in {"error", "failure", "skipped"}
                    ],
                    "".join(
                        child.text or ""
                        for child in case
                        if child.tag in {"system-out", "system-err"}
                    ),
                )
                for case in ET.parse(project.root / "result.xml").iter("testcase")
            ],
        }
    assert observed["pytest-native"] == observed["pytest"]
    return observed["pytest"]


COMMON = """import atexit, builtins, contextlib, json, logging, types
from pathlib import Path
import pytest
import _pytest.logging as pytest_logging
events = []
state = {}
def save_evidence():
    Path('evidence.json').write_text(json.dumps({'events': events, 'state': state}))
atexit.register(save_evidence)
@pytest.fixture(autouse=True)
def phase_logs(request):
    logging.info('setup:' + request.node.name)
    yield
    logging.info('teardown:' + request.node.name)
"""


@pytest.mark.parametrize("mutation", ["root", "handler", "propagation"])
def test_logging_scope_reads_live_roots_handlers_and_propagation(
    tmp_path: Path, mutation: str
) -> None:
    sources = {
        "root": """
old_root = logging.root
new_root = logging.RootLogger(logging.ERROR)
new_root.manager = old_root.manager
def test_first():
    logging.info('call:first')
    logging.root = new_root
def test_second(caplog):
    state['level_during_call'] = logging.root.level
    state['old_root_capture_handlers'] = [
        type(h).__name__ for h in old_root.handlers
        if isinstance(h, pytest_logging.LogCaptureHandler)
    ]
    logging.info('call:second')
    state['call_records'] = [r.getMessage() for r in caplog.records]
""",
        "handler": """
def test_first(request, caplog):
    plugin = request.config.pluginmanager.getplugin('logging-plugin')
    old_caplog = plugin.caplog_handler
    old_report = plugin.report_handler
    logging.info('call:original-handler')
    plugin.caplog_handler = pytest_logging.LogCaptureHandler()
    plugin.report_handler = pytest_logging.LogCaptureHandler()
    plugin.caplog_handler.setFormatter(old_caplog.formatter)
    plugin.report_handler.setFormatter(old_report.formatter)
    state['caplog_is_original'] = caplog.handler is old_caplog
    state['original_records'] = [r.getMessage() for r in caplog.records]
def test_second(caplog):
    logging.info('call:replacement-handler')
    state['replacement_records'] = [r.getMessage() for r in caplog.records]
""",
        "propagation": """
isolated = logging.getLogger('scope-isolated')
isolated.propagate = False
isolated.setLevel(logging.INFO)
def test_first(caplog):
    isolated.info('call:isolated')
    state['first_records'] = [r.getMessage() for r in caplog.records]
    isolated.propagate = True
def test_second(caplog):
    isolated.info('call:propagating')
    state['second_records'] = [r.getMessage() for r in caplog.records]
""",
    }
    observed = _compare(tmp_path, COMMON + sources[mutation])
    assert observed["status"] == 0


def test_unregistered_logging_plugin_captures_nothing_until_registered_again(
    tmp_path: Path,
) -> None:
    # Flask's logging tests unregister pytest's logging plug-in in a fixture.
    source = (
        COMMON
        + """
@pytest.fixture
def without_logging_plugin(pytestconfig):
    plugin = pytestconfig.pluginmanager.unregister(name='logging-plugin')
    yield
    pytestconfig.pluginmanager.register(plugin, 'logging-plugin')
def test_first(without_logging_plugin):
    state['first_handlers'] = [type(h).__name__ for h in logging.root.handlers]
    logging.info('call:first')
def test_second(caplog):
    state['second_handlers'] = sorted(type(h).__name__ for h in logging.root.handlers)
    logging.info('call:second')
    state['second_records'] = [r.getMessage() for r in caplog.records]
"""
    )
    observed = _compare(tmp_path, source)
    assert observed["status"] == 0
    evidence = observed["evidence"]
    assert isinstance(evidence, dict)
    state = evidence["state"]
    assert isinstance(state, dict)
    assert "LogCaptureHandler" not in state["first_handlers"]
    assert "LogCaptureHandler" in state["second_handlers"]
    assert state["second_records"] == ["call:second"]


def test_logging_plugin_registered_after_initialization_captures_later_tests(
    tmp_path: Path,
) -> None:
    # Collection can remove the plug-in before native execution initializes.
    conftest = """import builtins
def pytest_collection_modifyitems(config):
    builtins._logging_plugin = config.pluginmanager.unregister(name='logging-plugin')
"""
    source = (
        COMMON
        + """
@pytest.fixture
def restore_logging_plugin(pytestconfig):
    yield
    pytestconfig.pluginmanager.register(builtins._logging_plugin, 'logging-plugin')
def test_first(restore_logging_plugin):
    state['first_handlers'] = [type(h).__name__ for h in logging.root.handlers]
    logging.info('call:first')
def test_second(caplog):
    state['second_handlers'] = [type(h).__name__ for h in logging.root.handlers]
    logging.info('call:second')
    state['second_records'] = [r.getMessage() for r in caplog.records]
"""
    )
    observed = _compare(tmp_path, source, conftest=conftest)
    assert observed["status"] == 0
    evidence = observed["evidence"]
    assert isinstance(evidence, dict)
    state = evidence["state"]
    assert isinstance(state, dict)
    assert "LogCaptureHandler" not in state["first_handlers"]
    assert "LogCaptureHandler" in state["second_handlers"]
    assert state["second_records"] == ["call:second"]


MUTATIONS = {
    "instance": """
original = plugin._runtest_for
@contextlib.contextmanager
def replacement(item, when):
    if item.name == 'test_second': events.append('enter:' + when)
    with original(item, when):
        try: yield
        finally:
            if item.name == 'test_second': events.append('exit:' + when)
plugin._runtest_for = replacement
""",
    "class": """
original = type(plugin)._runtest_for
@contextlib.contextmanager
def replacement(self, item, when):
    if item.name == 'test_second': events.append('enter:' + when)
    with original(self, item, when):
        try: yield
        finally:
            if item.name == 'test_second': events.append('exit:' + when)
type(plugin)._runtest_for = replacement
""",
    "generator_code": """
generator = type(plugin)._runtest_for.__wrapped__
clone = types.FunctionType(generator.__code__, generator.__globals__)
builtins._vfy_scope_original = contextlib.contextmanager(clone)
builtins._vfy_scope_events = events
def replacement(self, item, when):
    storage = __import__('builtins')
    if item.name == 'test_second': storage._vfy_scope_events.append('enter:' + when)
    with storage._vfy_scope_original(self, item, when):
        try: yield
        finally:
            if item.name == 'test_second': storage._vfy_scope_events.append('exit:' + when)
generator.__code__ = replacement.__code__
""",
    "closure": """
wrapper = type(plugin)._runtest_for
original = contextlib.contextmanager(wrapper.__wrapped__)
def replacement(self, item, when):
    if item.name == 'test_second': events.append('enter:' + when)
    with original(self, item, when):
        try: yield
        finally:
            if item.name == 'test_second': events.append('exit:' + when)
wrapper.__closure__[0].cell_contents = replacement
""",
    "wrapper_code": """
builtins._vfy_scope_events = events
def factory(func):
    def replacement(*args, **kwargs):
        if args[1].name == 'test_second':
            __import__('builtins')._vfy_scope_events.append('wrapper:' + args[2])
        return __import__('contextlib')._GeneratorContextManager(func, args, kwargs)
    return replacement
wrapper = type(plugin)._runtest_for
wrapper.__code__ = factory(wrapper.__wrapped__).__code__
""",
    "catching_enter_code": """
original = pytest_logging.catching_logs.__enter__
builtins._vfy_scope_enter = types.FunctionType(original.__code__, original.__globals__)
builtins._vfy_scope_events = events
def replacement(self):
    storage = __import__('builtins')
    storage._vfy_scope_events.append('catching-enter')
    return storage._vfy_scope_enter(self)
original.__code__ = replacement.__code__
""",
    "context_enter_code": """
original = contextlib._GeneratorContextManager.__enter__
builtins._vfy_context_enter = types.FunctionType(original.__code__, original.__globals__)
builtins._vfy_scope_events = events
def replacement(self):
    storage = __import__('builtins')
    if getattr(getattr(self, 'func', None), '__name__', '') == '_runtest_for':
        storage._vfy_scope_events.append('context-enter')
    return storage._vfy_context_enter(self)
original.__code__ = replacement.__code__
""",
    "catching_factory": """
original = pytest_logging.catching_logs
class Replacement(original):
    def __enter__(self):
        events.append('scope-enter')
        return super().__enter__()
pytest_logging.catching_logs = Replacement
""",
    "context_constructor": """
original = contextlib._GeneratorContextManager
def replacement(func, args, kwargs):
    if getattr(func, '__name__', '') == '_runtest_for':
        events.append('context-construct:' + args[2])
    return original(func, args, kwargs)
contextlib._GeneratorContextManager = replacement
""",
}


@pytest.mark.parametrize("mutation", list(MUTATIONS))
def test_logging_scope_modifications_use_the_live_upstream_implementation(
    tmp_path: Path, mutation: str
) -> None:
    installation = "\n".join("    " + line for line in MUTATIONS[mutation].strip().splitlines())
    source = (
        COMMON
        + f"""
def test_first(request):
    plugin = request.config.pluginmanager.getplugin('logging-plugin')
{installation}
def test_second(caplog):
    logging.info('call:second')
    state['records'] = [r.getMessage() for r in caplog.records]
"""
    )
    observed = _compare(tmp_path, source)
    assert observed["status"] == 0
    evidence = observed["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["events"]


@pytest.mark.parametrize("mutation", [name for name in MUTATIONS if name != "instance"])
def test_logging_scope_modifications_before_native_initialization_use_upstream(
    tmp_path: Path, mutation: str
) -> None:
    installation = MUTATIONS[mutation].replace("type(plugin)", "pytest_logging.LoggingPlugin")
    source = (
        COMMON
        + installation
        + """
def test_first():
    logging.info('call:first')
def test_second(caplog):
    logging.info('call:second')
    state['records'] = [r.getMessage() for r in caplog.records]
"""
    )
    observed = _compare(tmp_path, source)
    assert observed["status"] == 0
    evidence = observed["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["events"]


@pytest.mark.parametrize("failure", ["second_enter", "report_exit", "caplog_exit"])
def test_logging_scope_errors_unwind_the_nested_handlers(tmp_path: Path, failure: str) -> None:
    source = (
        COMMON
        + f"""
class ObservedRoot(logging.RootLogger):
    def addHandler(self, handler):
        events.append('add:' + getattr(handler, 'scope_label', 'other'))
        if getattr(handler, 'scope_label', '') == 'report' and state.get('armed') == 'second_enter':
            state['armed'] = None
            raise RuntimeError('scope enter sentinel')
        super().addHandler(handler)
    def removeHandler(self, handler):
        events.append('remove:' + getattr(handler, 'scope_label', 'other'))
        super().removeHandler(handler)
        if getattr(handler, 'scope_label', '') == state.get('armed'):
            state['armed'] = None
            raise RuntimeError('scope exit sentinel')
def test_first(request):
    plugin = request.config.pluginmanager.getplugin('logging-plugin')
    plugin.caplog_handler.scope_label = 'caplog'
    plugin.report_handler.scope_label = 'report'
    replacement = ObservedRoot(logging.INFO)
    replacement.manager = logging.root.manager
    logging.root = replacement
    modes = {{'second_enter': 'second_enter', 'report_exit': 'report', 'caplog_exit': 'caplog'}}
    state['armed'] = modes[{failure!r}]
    logging.info('call:first')
def test_second():
    logging.info('call:second')
"""
    )
    observed = _compare(tmp_path, source)
    assert observed["status"] != 0


def test_live_logging_and_user_report_sections_are_preserved(tmp_path: Path) -> None:
    source = (
        COMMON
        + """
def test_sections(request, caplog):
    request.node.add_report_section('call', 'log', 'user section')
    logging.info('live call sentinel')
    state['records'] = [record.getMessage() for record in caplog.records]
"""
    )
    observed = _compare(tmp_path, source, live_logs=True)
    assert observed["status"] == 0


@pytest.mark.parametrize("when", ["actual", "other"])
@pytest.mark.parametrize("mutation", ["append", "replace", "insert"])
def test_user_log_sections_are_counted_once_in_phase_order(
    tmp_path: Path, when: str, mutation: str
) -> None:
    source = (
        COMMON
        + f"""
def user_log(item, phase):
    label = phase if {when!r} == 'actual' else {{
        'setup': 'teardown', 'call': 'setup', 'teardown': 'call',
    }}[phase]
    item.add_report_section(label, 'log', 'user:' + phase)
    item.add_report_section(label, 'log', 'user:duplicate')
@pytest.fixture(autouse=True)
def manual_sections(request, phase_logs):
    user_log(request.node, 'setup')
    yield
    user_log(request.node, 'teardown')
    state['user_sections'] = [
        (when, kind, text) for when, kind, text in request.node._report_sections
        if text.startswith('user:')
    ]
def test_sections(request, caplog):
    item = request.node
    if {mutation!r} == 'replace':
        item._report_sections = list(item._report_sections)
    user_log(item, 'call')
    if {mutation!r} == 'insert':
        item._report_sections.insert(
            len(item._report_sections) - 2, ('teardown', 'log', 'user:inserted')
        )
        item._report_sections.append(('call', 'log', 'user:removed'))
        item._report_sections.pop()
    item.add_report_section('teardown', 'log', 'user:duplicate')
    logging.info('call:original-handler')
    plugin = request.config.pluginmanager.getplugin('logging-plugin')
    old_report = plugin.report_handler
    plugin.report_handler = pytest_logging.LogCaptureHandler()
    plugin.report_handler.setFormatter(old_report.formatter)
    state['records'] = [record.getMessage() for record in caplog.records]
"""
    )
    observed = _compare(tmp_path, source)
    assert observed["status"] == 0


@pytest.mark.parametrize("same_text", [False, True])
def test_scope_exit_user_sections_are_retained(tmp_path: Path, same_text: bool) -> None:
    source = (
        COMMON
        + f"""
original = pytest_logging.LoggingPlugin._runtest_for
@contextlib.contextmanager
def replacement(self, item, when):
    with original(self, item, when):
        yield
    if when == 'call': item.add_report_section(when, 'log', 'user:exit')
pytest_logging.LoggingPlugin._runtest_for = replacement
@pytest.fixture(autouse=True)
def inspect_sections(request, phase_logs):
    yield
    state['user_sections'] = [
        section for section in request.node._report_sections
        if section[2].startswith('user:')
    ]
def test_sections():
    logging.info('user:exit' if {same_text!r} else 'call:builtin')
"""
    )
    observed = _compare(tmp_path, source)
    assert observed["status"] == 0


def test_scope_exit_user_section_does_not_keep_builtin_log_owner(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_scopes.py",
        """import contextlib, json, logging
from pathlib import Path
import _pytest.logging as pytest_logging
original = pytest_logging.LoggingPlugin._runtest_for
@contextlib.contextmanager
def replacement(self, item, when):
    with original(self, item, when):
        yield
    if when == 'call': item.add_report_section(when, 'log', 'user:exit')
pytest_logging.LoggingPlugin._runtest_for = replacement
def test_first():
    logging.warning('builtin retention sentinel')
def test_zz_sections(request):
    sections = [section for item in request.session.items for section in item._report_sections]
    Path('sections.json').write_text(json.dumps(sections))
""",
    )
    result = project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q")
    result.expect(0)
    assert json.loads((project.root / "sections.json").read_text()) == [
        ["call", "log", "user:exit"]
    ]


@pytest.mark.parametrize("interrupt", [False, True])
def test_logging_scopes_do_not_add_code_or_compile_audit_callbacks(
    tmp_path: Path, interrupt: bool
) -> None:
    source = (
        COMMON
        + f"""
import sys
def audit(event, args):
    scope_code = (
        event == 'object.__getattr__' and args[1] == '__code__'
        and getattr(args[0], '__name__', '') == '_runtest_for'
    )
    scope_compile = event == 'compile' and str(args[1]).endswith('contextlib.py')
    if scope_code or scope_compile:
        events.append(event)
        if {interrupt!r}: raise KeyboardInterrupt('logging audit sentinel')
sys.addaudithook(audit)
def test_audit(caplog):
    logging.info('audit call sentinel')
    state['records'] = [record.getMessage() for record in caplog.records]
"""
    )
    observed = _compare(tmp_path, source)
    assert observed["status"] == 0
    evidence = observed["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["events"] == []
