"""Differential proof for native pytest's live and file logging lifecycle."""

from __future__ import annotations

import json
import re
import sys
from html import unescape
from pathlib import Path

import pytest

from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="native logging differential requires Unix",
)


def test_logging_handler_levels_follow_nested_scope_entry(tmp_path: Path) -> None:
    source = """import logging
import pytest

@pytest.fixture
def observe_entry(monkeypatch):
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    original_set = logging.Handler.setLevel
    original_add = root.addHandler
    events = []
    handlers = []
    def ordinal(handler):
        if handler not in handlers:
            handlers.append(handler)
        return handlers.index(handler)
    def set_level(handler, level):
        if hasattr(handler, 'records'):
            if not events:
                root.setLevel(logging.WARNING)
            events.append(('set', ordinal(handler), level, root.level))
        return original_set(handler, level)
    def add_handler(handler):
        if hasattr(handler, 'records'):
            events.append(('add', ordinal(handler), handler.level, root.level))
        return original_add(handler)
    monkeypatch.setattr(logging.Handler, 'setLevel', set_level)
    monkeypatch.setattr(root, 'addHandler', add_handler)
    return events

def test_entry(observe_entry):
    assert observe_entry == [
        ('set', 0, logging.INFO, logging.WARNING),
        ('add', 0, logging.INFO, logging.WARNING),
        ('set', 1, logging.INFO, logging.INFO),
        ('add', 1, logging.INFO, logging.INFO),
    ]
"""
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_logging.py", source)
        result = (
            project.run_pytest("-q", "-o", "log_level=INFO", load_xdist=False)
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=1",
                "--no-cov",
                "-q",
                "-o",
                "log_level=INFO",
            )
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "1 passed" in output, output


def _source() -> str:
    return """
import logging

import pytest

log = logging.getLogger("vfy.native.logging")

@pytest.fixture(autouse=True)
def phase_logs(request):
    log.info("setup:%s", request.node.name)
    yield
    log.info("teardown:%s", request.node.name)

def test_first(caplog):
    caplog.set_level(logging.INFO, logger="vfy.native.logging")
    log.info("call:first")
    setup_records = caplog.get_records("setup")
    assert [record.getMessage() for record in setup_records] == ["setup:test_first"]
    assert [record.getMessage() for record in caplog.records] == ["call:first"]

def test_second(caplog):
    caplog.set_level(logging.INFO, logger="vfy.native.logging")
    log.info("call:second")
    setup_records = caplog.get_records("setup")
    assert [record.getMessage() for record in setup_records] == ["setup:test_second"]
    assert [record.getMessage() for record in caplog.records] == ["call:second"]
"""


def _run(project: VfyProject, engine: str, capture: str) -> CommandResult:
    return project.run_vfy(
        "test",
        f"--engine={engine}",
        "--workers=1",
        "--no-cov",
        "-q",
        f"--capture={capture}",
        "--log-cli-level=INFO",
        "--log-file=pytest.log",
        "--log-file-level=INFO",
        "-o",
        "log_cli=true",
        "-o",
        "log_cli_format=LIVE %(message)s",
        "-o",
        "log_file_format=FILE %(levelname)s %(message)s",
    )


def _terminal_log_evidence(stdout: str) -> tuple[list[str], list[str]]:
    sections: list[str] = []
    for line in stdout.splitlines():
        match = re.search(r"\blive log (setup|call|teardown)\b", line)
        if match is not None:
            section = match.group(1)
            assert isinstance(section, str)
            sections.append(section)
    records = [line.strip() for line in stdout.splitlines() if line.strip().startswith("LIVE ")]
    return sections, records


@pytest.mark.parametrize("capture", ["no", "fd"])
def test_native_live_and_file_logging_match_stock_vfy(tmp_path: Path, capture: str) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("pytest-native", "stock")}
    for project in projects.values():
        project.write("test_logging.py", _source())

    native = _run(projects["pytest-native"], "pytest-native", capture)
    stock = _run(projects["stock"], "pytest", capture)
    assert native.returncode == stock.returncode == 0, (
        native.stdout + native.stderr + stock.stdout + stock.stderr
    )

    expected_sections = [
        "setup",
        "call",
        "teardown",
        "setup",
        "call",
        "teardown",
    ]
    expected_records = [
        "LIVE setup:test_first",
        "LIVE call:first",
        "LIVE teardown:test_first",
        "LIVE setup:test_second",
        "LIVE call:second",
        "LIVE teardown:test_second",
    ]
    expected_file = (
        "FILE INFO setup:test_first\n"
        "FILE INFO call:first\n"
        "FILE INFO teardown:test_first\n"
        "FILE INFO setup:test_second\n"
        "FILE INFO call:second\n"
        "FILE INFO teardown:test_second\n"
    )
    assert _terminal_log_evidence(stock.stdout) == (expected_sections, expected_records)
    assert _terminal_log_evidence(native.stdout) == (expected_sections, expected_records)
    assert (projects["stock"].root / "pytest.log").read_bytes() == expected_file.encode()
    assert (projects["pytest-native"].root / "pytest.log").read_bytes() == expected_file.encode()


def test_native_success_logs_have_one_owner_and_preserve_user_sections(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    source = (
        "import json, logging\nfrom pathlib import Path\n"
        "def test_00(request):\n"
        "    request.node.add_report_section('call', 'custom', 'user section sentinel')\n"
        "    logging.warning('retention sentinel')\n"
    )
    for index in range(1, 3):
        source += f"def test_{index:02d}():\n    logging.warning('retention sentinel')\n"
    source += (
        "def test_zz_sections(request):\n"
        "    sections = [section for item in request.session.items "
        "for section in item._report_sections]\n"
        "    Path('sections.json').write_text(json.dumps(sections))\n"
    )
    project.write("test_logs.py", source)
    result = project.run_vfy("test", "--engine=native", "--workers=1", "--no-cov", "-q")
    result.expect(0)
    assert json.loads((project.root / "sections.json").read_text()) == [
        ["call", "custom", "user section sentinel"]
    ]


def test_filtered_log_append_preserves_an_identical_existing_user_section(tmp_path: Path) -> None:
    observations = []
    for engine in ("stock", "default"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nlog_format=%(message)s\n")
        project.write(
            "conftest.py",
            """import json
from pathlib import Path

user_section = ('call', 'log', 'same-log-sentinel')
filtered = 0

def pytest_collection_modifyitems(items):
    item = items[0]
    item._report_sections.append(user_section)
    original = item.add_report_section
    def add_report_section(when, key, content):
        global filtered
        if when == 'call' and key == 'log':
            assert content == 'same-log-sentinel', repr(content)
            filtered += 1
            return
        original(when, key, content)
    item.add_report_section = add_report_section

def pytest_sessionfinish(session):
    sections = session.items[0]._report_sections
    Path('sections.json').write_text(json.dumps({
        'sections': sections,
        'same_identity': any(section is user_section for section in sections),
        'filtered': filtered,
    }))
""",
        )
        project.write(
            "test_log.py",
            "import logging\ndef test_emit():\n    logging.warning('same-log-sentinel')\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observations.append(json.loads((project.root / "sections.json").read_text()))
    assert observations[0] == {
        "sections": [["call", "log", "same-log-sentinel"]],
        "same_identity": True,
        "filtered": 1,
    }
    assert observations[1] == observations[0]


@pytest.mark.parametrize("kind", ["passed", "failed", "subtest"])
def test_native_log_ownership_preserves_terminal_junit_and_html(tmp_path: Path, kind: str) -> None:
    if kind == "subtest" and int(pytest.version_tuple[0]) < 9:
        pytest.skip("pytest 9 owns subtest reports")
    source = (
        "import logging, pytest\n"
        "def emit(phase):\n"
        "    logging.warning('%s log sentinel', phase)\n"
        "@pytest.fixture(autouse=True)\n"
        "def phase_logs():\n"
        "    emit('setup')\n"
        "    yield\n"
        "    emit('teardown')\n"
    )
    if kind == "subtest":
        source += (
            "from unittest import TestCase\n"
            "class TestLogs(TestCase):\n"
            "    def test_logs(self):\n"
            "        emit('call')\n"
            "        with self.subTest(value=1):\n"
            "            self.fail('failure sentinel')\n"
        )
    else:
        source += "def test_logs():\n    emit('call')\n"
        if kind == "failed":
            source += "    assert False, 'failure sentinel'\n"
    evidence = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nlog_format = %(message)s\n")
        project.write("test_logs.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "-rP",
            "--junitxml=results.xml",
            "-o",
            "junit_logging=log",
            "--html=results.html",
        )
        assert result.returncode == int(kind != "passed"), result.stdout + result.stderr
        transcripts = (
            result.stdout,
            (project.root / "results.xml").read_text(),
        )
        html = unescape((project.root / "results.html").read_text())
        evidence[engine] = (
            [
                [text.count(f"{phase} log sentinel") for phase in ("setup", "call", "teardown")]
                for text in transcripts
            ],
            # Stock HTML repeats cumulative sections; native HTML keeps each
            # phase's logs once. Both must retain every captured log record.
            set(re.findall(r"(setup|call|teardown) log sentinel", html)),
        )
    assert all(evidence["pytest"][0][1]), evidence
    assert evidence["pytest"][1] == {"setup", "call", "teardown"}
    assert evidence["pytest-native"] == evidence["pytest"]


def test_caplog_phase_record_lists_keep_pytest_identity(tmp_path: Path) -> None:
    source = """import logging
import pytest

log = logging.getLogger('record-identity')

@pytest.fixture
def phases(caplog):
    caplog.set_level(logging.INFO, logger='record-identity')
    log.info('setup')
    setup = caplog.records
    assert caplog.get_records('setup') is setup
    missing = caplog.get_records('teardown')
    assert missing == [] and missing is not caplog.get_records('teardown')
    missing.append('not a record')
    assert caplog.get_records('teardown') == []
    yield setup
    assert caplog.get_records('setup') is setup
    assert [record.getMessage() for record in setup] == ['setup']
    assert caplog.get_records('call') is saved_call[0]
    assert [record.getMessage() for record in saved_call[0]] == ['after clear']
    assert caplog.records is caplog.get_records('teardown')
    log.info('teardown')
    assert [record.getMessage() for record in caplog.records] == ['teardown']

saved_call = []

def test_records(caplog, phases):
    call = caplog.records
    saved_call.append(call)
    assert call is caplog.get_records('call') and call is not phases
    log.info('before clear')
    assert [record.getMessage() for record in call] == ['before clear']
    caplog.clear()
    assert call is caplog.records and call == []
    log.info('after clear')
"""
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_logging.py", source)
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--workers=1", "--no-cov", "-q")
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "1 passed" in output, output


def test_nonpropagating_logger_created_during_handler_attachment_is_captured(
    tmp_path: Path,
) -> None:
    source = """import logging
import pytest

@pytest.fixture
def create_logger_on_attach(monkeypatch):
    root = logging.getLogger()
    original = root.addHandler
    created = []
    def add_handler(handler):
        if not created and hasattr(handler, 'records'):
            logger = logging.getLogger('created-during-attachment')
            logger.propagate = False
            logger.setLevel(logging.INFO)
            created.append(logger)
        original(handler)
    monkeypatch.setattr(root, 'addHandler', add_handler)
    return created

def test_late_logger(caplog, create_logger_on_attach):
    assert len(create_logger_on_attach) == 1
    create_logger_on_attach[0].info('attached this phase')
    assert 'attached this phase' in caplog.messages
"""
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_logging.py", source)
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--workers=1", "--no-cov", "-q")
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "1 passed" in output, output


def test_logger_created_during_second_capture_handler_matches_pytest(tmp_path: Path) -> None:
    source = """import logging
import pytest

@pytest.fixture
def create_logger_on_second_attach(monkeypatch):
    root = logging.getLogger()
    original = root.addHandler
    created = []
    seen = []
    def add_handler(handler):
        if hasattr(handler, 'records'):
            seen.append(handler)
            if len(seen) == 2:
                logger = logging.getLogger('created-on-second-attachment')
                logger.propagate = False
                logger.setLevel(logging.INFO)
                created.append(logger)
        original(handler)
    monkeypatch.setattr(root, 'addHandler', add_handler)
    return created

def test_late_logger(caplog, create_logger_on_second_attach):
    assert len(create_logger_on_second_attach) == 1
    create_logger_on_second_attach[0].info('attached this phase')
    assert caplog.messages == []
"""
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_logging.py", source)
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--workers=1", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
