"""Independent logging and warning scopes preserve observable pytest behavior."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _runs(project: VfyProject, arguments: list[str]) -> Iterator[tuple[str, CommandResult]]:
    yield "pytest", project.run_pytest("-q", *arguments, load_xdist=False)
    yield (
        "vfy",
        project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            "--native-result-json=results.json",
            *arguments,
        ),
    )


def test_caplog_phase_records_clear_and_handler_identity(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nlog_format = %(levelname)s:%(message)s\n")
    project.write(
        "test_logs.py",
        """import json, logging, pytest
from pathlib import Path
logger = logging.getLogger('capture.phase')
@pytest.fixture(autouse=True)
def phases(caplog):
    logger.warning('setup')
    yield
    logger.warning('teardown')
    assert caplog.messages == ['teardown']
    assert [r.getMessage() for r in caplog.get_records('call')] == ['after clear']
    Path('phase.json').write_text(json.dumps([
        [r.getMessage() for r in caplog.get_records(p)]
        for p in ['setup', 'call', 'teardown', 'missing']]))
def test_logs(caplog):
    assert caplog.messages == []
    assert [r.getMessage() for r in caplog.get_records('setup')] == ['setup']
    records = caplog.records
    logger.warning('before %s', 'clear')
    assert records is caplog.records is caplog.handler.records
    assert records[0].msg == 'before %s' and records[0].args == ('clear',)
    assert caplog.record_tuples == [('capture.phase', logging.WARNING, 'before clear')]
    assert caplog.text == 'WARNING:before clear\\n'
    assert caplog.handler.stream.getvalue() == caplog.text
    stream = caplog.handler.stream
    caplog.clear()
    assert records is caplog.records and records == [] and caplog.text == ''
    assert caplog.handler.stream is not stream
    assert stream.getvalue() == 'WARNING:before clear\\n'
    logger.warning('after clear')
""",
    )
    for _, result in _runs(project, []):
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads((tmp_path / "phase.json").read_text()) == [
            ["setup"],
            ["after clear"],
            ["teardown"],
            [],
        ]
        (tmp_path / "phase.json").unlink()


def test_caplog_levels_filters_disabled_logging_and_restoration(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_logs.py",
        """import logging
logger = logging.getLogger('capture.level')
initial_level = logger.level
initial_disable = logger.manager.disable
def test_first(caplog):
    logging.disable(logging.CRITICAL)
    caplog.set_level('INFO', logger=logger.name)
    logger.info('enabled')
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        logger.debug('debug')
        with caplog.at_level(logging.ERROR, logger=logger.name):
            logger.warning('hidden')
        logger.debug('restored inner')
    logger.debug('hidden outer')
    class Filter(logging.Filter):
        def filter(self, record):
            return 'filtered' not in record.getMessage()
    with caplog.filtering(Filter()):
        logger.warning('filtered')
        logger.warning('visible')
    logger.warning('unfiltered')
    assert caplog.messages == ['enabled', 'debug', 'restored inner', 'visible', 'unfiltered']
def test_second(caplog):
    assert logger.level == initial_level
    # set_level restores the disabled value present at fixture acquisition.
    assert logger.manager.disable == logging.CRITICAL
    logging.disable(initial_disable)
    assert caplog.records == []
""",
    )
    for _, result in _runs(project, []):
        assert result.returncode == 0, result.stdout + result.stderr


def test_caplog_report_logs_survive_fixture_clear(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nlog_format = %(message)s\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
sections = []
def pytest_runtest_logreport(report):
    sections.extend([[name, text] for name, text in report.sections
                     if name.startswith('Captured log')])
def pytest_sessionfinish():
    Path('sections.json').write_text(json.dumps(sections))
""",
    )
    project.write(
        "test_logs.py",
        """import logging
def test_logs(caplog):
    logging.warning('retained in report')
    caplog.clear()
    assert caplog.records == []
    assert False, 'report sentinel'
""",
    )
    for engine, result in _runs(project, []):
        assert result.returncode == 1, result.stdout + result.stderr
        sections = json.loads((tmp_path / "sections.json").read_text())
        assert ["Captured log call", "retained in report"] in sections
        (tmp_path / "sections.json").unlink()
        if engine == "vfy":
            record = json.loads((tmp_path / "results.json").read_text())
            assert record["cases"][0]["phases"][1]["log"] == "retained in report"


@pytest.mark.parametrize(("raise_exceptions", "status"), [(True, 1), (False, 0)])
def test_invalid_log_format_uses_stdlib_error_policy(
    tmp_path: Path, raise_exceptions: bool, status: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_logs.py",
        f"""import logging
def test_logs(monkeypatch):
    monkeypatch.setattr(logging, 'raiseExceptions', {raise_exceptions!r})
    logging.warning('format %d', 'not numeric')
""",
    )
    for _, result in _runs(project, []):
        assert result.returncode == status, result.stdout + result.stderr


def test_warning_hook_observes_startup_collection_and_test_protocol(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "startup_wrapper.py",
        """import warnings
import pytest
@pytest.hookimpl(wrapper=True)
def pytest_load_initial_conftests():
    warnings.warn('outer startup warning before yield')
    yield
    warnings.warn('outer startup warning after yield')
""",
    )
    project.write(
        "conftest.py",
        """import json
import warnings
from pathlib import Path
warnings.warn('initial conftest warning')
seen = []
def pytest_warning_recorded(warning_message, when, nodeid, location):
    seen.append([str(warning_message.message), warning_message.category.__name__,
                 when, nodeid, location])
def pytest_sessionfinish():
    Path('warnings.json').write_text(json.dumps(seen))
""",
    )
    project.write(
        "test_warnings.py",
        """import warnings, pytest
warnings.warn('collection warning', DeprecationWarning)
@pytest.fixture(autouse=True)
def warning_fixture():
    warnings.warn('setup warning', PendingDeprecationWarning)
    warnings.filterwarnings('ignore', message='hidden protocol warning')
    yield
    warnings.warn('teardown warning')
def test_warns():
    warnings.warn('hidden protocol warning')
    warnings.warn('call warning')
""",
    )
    expected = [
        ["initial conftest warning", "UserWarning", "config", "", None],
        ["collection warning", "DeprecationWarning", "collect", "", None],
        [
            "setup warning",
            "PendingDeprecationWarning",
            "runtest",
            "test_warnings.py::test_warns",
            None,
        ],
        ["call warning", "UserWarning", "runtest", "test_warnings.py::test_warns", None],
        ["teardown warning", "UserWarning", "runtest", "test_warnings.py::test_warns", None],
    ]
    for engine, result in _runs(project, ["-p", "startup_wrapper"]):
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.count("UserWarning: initial conftest warning") == 1
        for phase in ["before", "after"]:
            warning = f"UserWarning: outer startup warning {phase} yield"
            assert warning not in result.stdout
            assert result.stderr.count(warning) == 1
        assert json.loads((tmp_path / "warnings.json").read_text()) == expected
        (tmp_path / "warnings.json").unlink()
        if engine == "vfy":
            record = json.loads((tmp_path / "results.json").read_text())
            assert len(record["warnings"]) == len(expected)
            assert [warning["nodeid"] for warning in record["warnings"]] == [
                "",
                "",
                *["test_warnings.py::test_warns"] * 3,
            ]


@pytest.mark.parametrize(
    ("settings", "arguments", "status"),
    [
        ("filterwarnings = error\n", [], 4),
        ("filterwarnings = ignore\n", ["-W", "error"], 4),
        ("", ["-p", "no:warnings", "-W", "error"], 0),
        ("", ["-p", "no:warnings", "-p", "warnings", "-W", "error"], 4),
    ],
)
def test_initial_conftest_warnings_respect_filters_and_plugin_disable(
    tmp_path: Path, settings: str, arguments: list[str], status: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n" + settings)
    project.write("conftest.py", "import warnings\nwarnings.warn('startup warning')\n")
    project.write("test_example.py", "def test_example():\n    assert True\n")
    for _, result in _runs(project, arguments):
        assert result.returncode == status, result.stdout + result.stderr
        if status == 4:
            assert "UserWarning: startup warning" in result.stderr
        else:
            assert "1 passed" in result.stdout


@pytest.mark.parametrize(
    ("filters", "arguments", "mark", "message", "status"),
    [
        (["error"], [], "", "danger", 1),
        (["error", "ignore:danger:UserWarning"], [], "", "danger", 0),
        (["error"], ["-W", "ignore:danger:UserWarning"], "", "danger", 0),
        (["ignore"], ["-W", "error"], "ignore:danger:UserWarning", "danger", 0),
        (["error"], ["-W", "ignore:a.b:UserWarning"], "", "axb", 1),
        (["error", "ignore:a.b:UserWarning"], [], "", "axb", 0),
        (["ignore"], ["-W", "error", "-W", "ignore"], "", "danger", 0),
    ],
)
def test_warning_filter_configuration_cli_and_marker_precedence(
    tmp_path: Path,
    filters: list[str],
    arguments: list[str],
    mark: str,
    message: str,
    status: int,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini", "[pytest]\nfilterwarnings =\n" + "".join(f"    {f}\n" for f in filters)
    )
    decorator = f"@pytest.mark.filterwarnings({mark!r})\n" if mark else ""
    project.write(
        "test_warning.py",
        f"import warnings, pytest\n{decorator}def test_warning():\n"
        f"    warnings.warn({message!r})\n",
    )
    for _, result in _runs(project, arguments):
        assert result.returncode == status, result.stdout + result.stderr


@pytest.mark.parametrize(
    "filter_text",
    ["invalid", "error::::-1", "ignore::ValueError", "ignore::MissingCategory", "a:b:c:d:e:f"],
)
def test_invalid_warning_filter_is_usage_error_before_test_import(
    tmp_path: Path, filter_text: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_warning.py", "raise AssertionError('test module was imported')\n")
    for _, result in _runs(project, ["-W", filter_text]):
        assert result.returncode == 4, result.stdout + result.stderr
        assert "warning configuration" in result.stderr
        assert "test module was imported" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("settings", "arguments", "visible"),
    [
        ("log_cli = true\nlog_cli_level = INFO\n", [], True),
        ("", ["--log-cli-level", "INFO"], True),
        ("log_cli_level = INFO\n", [], False),
    ],
)
def test_live_logging_level_enables_only_requested_terminal_output(
    tmp_path: Path, settings: str, arguments: list[str], visible: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nlog_format = %(message)s\n" + settings)
    project.write(
        "test_live.py",
        "import logging\ndef test_live():\n    logging.info('live logging sentinel')\n",
    )
    for _, result in _runs(project, arguments):
        assert result.returncode == 0, result.stdout + result.stderr
        assert ("live logging sentinel" in result.stdout) is visible


@pytest.mark.parametrize("indent", ["true", "y", "t", "4", "false"])
def test_log_format_multiline_indentation(tmp_path: Path, indent: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini",
        f"[pytest]\nlog_format = %(levelname)s %(message)s\nlog_auto_indent = {indent}\n",
    )
    project.write(
        "test_logs.py",
        """import json, logging
from pathlib import Path
def test_logs(caplog):
    logging.warning('first\\nsecond')
    logging.warning('third\\nfourth', extra={'auto_indent': 2})
    Path('text.json').write_text(json.dumps(caplog.text))
""",
    )
    prefix = {"true": "        ", "y": "        ", "t": "        ", "4": "    ", "false": ""}[
        indent
    ]
    for _, result in _runs(project, []):
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads((tmp_path / "text.json").read_text()) == (
            f"WARNING first\n{prefix}second\nWARNING third\n  fourth\n"
        )
        (tmp_path / "text.json").unlink()


def test_warning_sources_retain_original_objects_and_resource_diagnostics(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
seen = []
def pytest_warning_recorded(warning_message):
    seen.append([type(warning_message.source).__name__, str(warning_message.message)])
def pytest_sessionfinish():
    Path('warnings.json').write_text(json.dumps(seen))
""",
    )
    project.write(
        "test_warning.py",
        """import warnings
class Source:
    pass
def test_warning():
    warnings.warn('resource sentinel', ResourceWarning, source=Source())
""",
    )
    for engine, result in _runs(project, ["-W", "always"]):
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads((tmp_path / "warnings.json").read_text()) == [
            ["Source", "resource sentinel"]
        ]
        (tmp_path / "warnings.json").unlink()
        if engine == "vfy":
            record = json.loads((tmp_path / "results.json").read_text())
            assert "Enable tracemalloc" in record["warnings"][0]["message"]


def test_logging_capture_respects_builtin_wrapper_priority(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, logging, pytest
from pathlib import Path
logger = logging.getLogger('capture.hooks')
@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call():
    logger.warning('outer before')
    result = yield
    logger.warning('outer after')
    return result
@pytest.hookimpl(wrapper=True, trylast=True, specname='pytest_runtest_call')
def pytest_inner_call():
    logger.warning('inner before')
    result = yield
    logger.warning('inner after')
    return result
@pytest.fixture(autouse=True)
def snapshot(caplog):
    yield
    Path('captured.json').write_text(json.dumps(
        [r.getMessage() for r in caplog.get_records('call')]))
""",
    )
    project.write(
        "test_hooks.py",
        """import logging
def test_logs(caplog):
    assert caplog.messages == ['inner before']
    logging.getLogger('capture.hooks').warning('body')
""",
    )
    for engine, result in _runs(project, []):
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads((tmp_path / "captured.json").read_text()) == [
            "inner before",
            "body",
            "inner after",
        ]
        (tmp_path / "captured.json").unlink()
        if engine == "vfy":
            record = json.loads((tmp_path / "results.json").read_text())
            assert record["cases"][0]["phases"][1].get("stderr", "") == ""


@pytest.mark.parametrize("live", [False, True])
def test_caplog_captures_background_thread_records(tmp_path: Path, live: bool) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini",
        "[pytest]\nlog_format = %(message)s\n"
        + ("log_cli = true\nlog_cli_level = INFO\n" if live else ""),
    )
    project.write(
        "test_thread.py",
        """import logging, threading
def test_thread(caplog):
    caplog.set_level(logging.INFO)
    thread = threading.Thread(target=logging.getLogger('capture.thread').info,
                              args=('background thread sentinel',))
    thread.start()
    thread.join()
    assert caplog.messages == ['background thread sentinel']
    assert caplog.records[0].thread != threading.get_ident()
""",
    )
    for engine, result in _runs(project, []):
        assert result.returncode == 0, result.stdout + result.stderr
        assert ("background thread sentinel" in result.stdout) is live
        if engine == "vfy":
            record = json.loads((tmp_path / "results.json").read_text())
            assert "background thread sentinel" in record["cases"][0]["phases"][1]["log"]


def test_warning_hooks_can_emit_further_recorded_warnings(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, warnings
from pathlib import Path
seen = []
def pytest_warning_recorded(warning_message, when):
    text = str(warning_message.message)
    seen.append([text, when])
    if text == 'first':
        warnings.warn('second')
def pytest_sessionfinish():
    Path('warnings.json').write_text(json.dumps(seen))
""",
    )
    project.write(
        "test_warning.py", "import warnings\ndef test_warning(): warnings.warn('first')\n"
    )
    for engine, result in _runs(project, []):
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads((tmp_path / "warnings.json").read_text()) == [
            ["first", "runtest"],
            ["second", "runtest"],
        ]
        (tmp_path / "warnings.json").unlink()
        if engine == "vfy":
            record = json.loads((tmp_path / "results.json").read_text())
            assert len(record["warnings"]) == 2


@pytest.mark.parametrize("tryfirst", [True, False])
def test_warning_formatting_runs_at_builtin_hook_priority(tmp_path: Path, tryfirst: bool) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"""import pytest
from pathlib import Path
@pytest.hookimpl(tryfirst={tryfirst!r})
def pytest_warning_recorded(warning_message):
    warning_message.message = UserWarning('changed by hook')
    Path('observed.txt').write_text(str(warning_message.message))
""",
    )
    project.write("test_warning.py", "import warnings\ndef test_warning(): warnings.warn('body')\n")
    expected = "UserWarning: changed by hook" if tryfirst else "UserWarning: body"
    for engine, result in _runs(project, []):
        assert result.returncode == 0, result.stdout + result.stderr
        assert expected in result.stdout
        assert (tmp_path / "observed.txt").read_text() == "changed by hook"
        (tmp_path / "observed.txt").unlink()
        if engine == "vfy":
            record = json.loads((tmp_path / "results.json").read_text())
            assert expected in record["warnings"][0]["message"]
