"""Terminal publication runs inside pytest's live session-finish boundaries."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_ORDER_CONFTEST = """import pytest
import warnings

class Outer:
    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_terminal_summary(self):
        print('HOOK_TERMINAL_OUTER_ENTER')
        try:
            yield
        finally:
            print('HOOK_TERMINAL_OUTER_EXIT')

def pytest_configure(config):
    config.pluginmanager.register(Outer(), 'order_outer')

def pytest_sessionfinish(session):
    print('HOOK_SESSION_BODY')
    warnings.warn('session hook warning')

def pytest_terminal_summary():
    print('HOOK_TERMINAL_BODY')
    warnings.warn('terminal hook warning')

def pytest_unconfigure():
    print('HOOK_UNCONFIGURE')
"""

_ORDER_CASES = """import warnings

def test_pass():
    print('PASSED_CAPTURE_PAYLOAD')
    warnings.warn('test warning')

def test_fail():
    raise ValueError('failure payload')
"""

_OBSERVER = """
import json
from pathlib import Path

def pytest_runtest_logreport(report):
    with Path('outcomes.jsonl').open('a') as output:
        output.write(json.dumps([report.nodeid, report.when, report.outcome]) + '\\n')

def pytest_warning_recorded(warning_message, when, nodeid):
    with Path('warnings.jsonl').open('a') as output:
        output.write(json.dumps([str(warning_message.message), when, nodeid]) + '\\n')
"""

_ENV = {"NO_COLOR": "1", "CI": None, "BUILD_NUMBER": None}


def _ordered_sections(output: str) -> list[str]:
    pattern = re.compile(
        r"HOOK_[A-Z_]+(?::True:True)?|"
        r"(?<= )FAILURES(?= =)|(?<= )PASSES(?= =)|"
        r"warnings summary(?: \(final\))?|short test summary info|"
        r"PASSED_CAPTURE_PAYLOAD|"
        r"^\d+ (?:failed|passed|skipped|error|tests? collected)[^\n]*",
        re.MULTILINE,
    )
    return [
        "TOTALS" if match.group(0)[0].isdigit() else match.group(0)
        for match in pattern.finditer(output)
    ]


def _observations(path: Path) -> dict[str, list[object]]:
    groups: dict[str, list[object]] = {}
    for line in path.read_text().splitlines():
        nodeid, *record = json.loads(line)
        groups.setdefault(nodeid, []).append(record)
    return groups


@pytest.mark.parametrize("workers", [1, 2])
def test_terminal_sections_hooks_and_warning_passes_follow_stock(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    conftest = _ORDER_CONFTEST + _OBSERVER
    if workers == 2:
        # xdist can stop receiving worker warnings after workerfinished, before
        # worker terminal hooks complete. Identify the controller independently.
        conftest = "import os\nfrom pathlib import Path\n" + conftest.replace(
            "    print('HOOK_SESSION_BODY')",
            "    Path('finish-pid').write_text(str(os.getpid()))\n    print('HOOK_SESSION_BODY')",
        ).replace(
            "warnings.warn('terminal hook warning')",
            "warnings.warn(f'terminal hook warning (pid={os.getpid()})')",
        )
    project.write("conftest.py", conftest)
    project.write("test_order.py", _ORDER_CASES)
    oracle = project.run_python("-m", "pytest", "-q", "-rA", env=_ENV)
    expected = _observations(tmp_path / "outcomes.jsonl")
    expected_warnings = _observations(tmp_path / "warnings.jsonl")
    (tmp_path / "outcomes.jsonl").unlink()
    (tmp_path / "warnings.jsonl").unlink()
    terminal_oracle = oracle
    oracle_warning = "terminal hook warning"
    if workers == 2:
        # Parallel session hooks execute in workers and the controller. Their
        # terminal interval has its own oracle; ordinary phase delivery keeps
        # the serial pytest contract above.
        terminal_oracle = project.run_python(
            "-m", "pytest", "-p", "xdist.plugin", "-q", "-rA", "-n", "2", "--dist=load", env=_ENV
        )
        # Every worker session-finish body precedes workerfinished; the
        # controller body therefore writes last, before its terminal hooks.
        oracle_warning += f" (pid={(tmp_path / 'finish-pid').read_text()})"
        (tmp_path / "outcomes.jsonl").unlink()
        (tmp_path / "warnings.jsonl").unlink()
    candidate = project.run_vfy(
        "test", "--engine=native", "--no-cov", f"--workers={workers}", "-q", "-rA", env=_ENV
    )
    candidate_warning = "terminal hook warning"
    if workers == 2:
        candidate_warning += f" (pid={(tmp_path / 'finish-pid').read_text()})"
    diagnostic = (
        oracle.stdout
        + oracle.stderr
        + terminal_oracle.stdout
        + terminal_oracle.stderr
        + candidate.stdout
        + candidate.stderr
    )
    assert oracle.returncode == terminal_oracle.returncode == candidate.returncode == 1, diagnostic
    assert _observations(tmp_path / "outcomes.jsonl") == expected, diagnostic
    if workers == 1:
        assert _observations(tmp_path / "warnings.jsonl") == expected_warnings, diagnostic
        assert _ordered_sections(candidate.stdout) == _ordered_sections(oracle.stdout), diagnostic
    else:
        # Worker hook output remains visible. Compare the controller's complete
        # terminal interval, which follows the final session-body marker.
        def controller(output: str) -> str:
            return output.rsplit("HOOK_SESSION_BODY", 1)[1]

        assert _ordered_sections(controller(candidate.stdout)) == _ordered_sections(
            controller(terminal_oracle.stdout)
        ), diagnostic
        for output, message in (
            (candidate.stdout, candidate_warning),
            (terminal_oracle.stdout, oracle_warning),
        ):
            before, final = output.rsplit("warnings summary (final)", 1)
            assert message not in before, diagnostic
            assert final.count(message) == 1, diagnostic
        # Preserve every vfy-observed terminal warning, including workers whose
        # corresponding late xdist event may not reach the controller.
        observed = {
            message: rows
            for message, rows in _observations(tmp_path / "warnings.jsonl").items()
            if message.startswith("terminal hook warning (pid=")
        }
        assert candidate_warning in observed, diagnostic
        assert len(observed) >= 2, diagnostic
        for message, rows in observed.items():
            assert len(rows) == candidate.stdout.count(message) == 1, diagnostic
    assert candidate.stdout.count("PASSED_CAPTURE_PAYLOAD") == 1, diagnostic
    assert candidate.stdout.count(candidate_warning) == terminal_oracle.stdout.count(
        oracle_warning
    ), diagnostic


def test_executed_empty_session_still_publishes_its_terminal_summary(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    oracle = project.run_python("-m", "pytest", "-q", env=_ENV)
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=_ENV)
    diagnostic = oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    assert oracle.returncode == candidate.returncode == 5, diagnostic
    assert len(re.findall(r"^no tests ran\b", oracle.stdout, re.MULTILINE)) == 1, diagnostic
    assert len(re.findall(r"^no tests ran\b", candidate.stdout, re.MULTILINE)) == 1, diagnostic


@pytest.mark.parametrize("option", ["--collect-only", "-pno:terminal"])
def test_terminal_absence_and_collection_totals_follow_live_hooks(
    tmp_path: Path, option: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _ORDER_CONFTEST)
    project.write("test_value.py", "def test_value():\n    pass\n")
    options = ("-q", option) if option == "--collect-only" else (option,)
    oracle = project.run_python("-m", "pytest", *options, env=_ENV)
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", *options, env=_ENV)
    diagnostic = oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    assert oracle.returncode == candidate.returncode == 0, diagnostic
    assert _ordered_sections(candidate.stdout) == _ordered_sections(oracle.stdout), diagnostic
    if option == "-pno:terminal":
        assert "tests collected" not in candidate.stdout, diagnostic
        assert "passed" not in candidate.stdout, diagnostic


@pytest.mark.parametrize("disabled", [False, True])
def test_warning_suppression_and_unicode_line_boundaries_preserve_records(
    tmp_path: Path, disabled: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _ORDER_CONFTEST + _OBSERVER)
    project.write(
        "test_order.py", _ORDER_CASES.replace("test warning", "first\\u0085second\\u2028third")
    )
    options = ("--disable-warnings",) if disabled else ()
    oracle = project.run_python("-m", "pytest", "-q", "-rA", *options, env=_ENV)
    expected = _observations(tmp_path / "warnings.jsonl")
    (tmp_path / "warnings.jsonl").unlink()
    candidate = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-q", "-rA", *options, env=_ENV
    )
    diagnostic = oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    assert oracle.returncode == candidate.returncode == 1, diagnostic
    assert _observations(tmp_path / "warnings.jsonl") == expected, diagnostic
    assert _ordered_sections(candidate.stdout) == _ordered_sections(oracle.stdout), diagnostic
    if disabled:
        assert "UserWarning:" not in candidate.stdout, diagnostic
    else:
        for line in ("UserWarning: first", "  second", "  third"):
            assert line in oracle.stdout, diagnostic
            assert line in candidate.stdout, diagnostic


def test_early_configuration_failure_matches_pytest_and_allows_a_fresh_run(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_value():\n    pass\n")
    project.write(
        "conftest.py",
        """from pathlib import Path
import os

def pytest_addoption(parser):
    if os.environ.get('EARLY_FAILURE') == '1':
        raise RuntimeError('early acquisition payload')

def pytest_unconfigure():
    print('HOOK_UNCONFIGURE')
""",
    )
    pattern = re.compile(r"HOOK_UNCONFIGURE|early acquisition payload")
    observed = []
    for engine in ("stock", "native"):
        failed = project.run_engine(engine, "-q", "-rA", env={**_ENV, "EARLY_FAILURE": "1"})
        fresh = project.run_engine(engine, "-q", "-rA", env={**_ENV, "EARLY_FAILURE": "0"})
        assert failed.returncode != 0, failed.stdout + failed.stderr
        assert "early acquisition payload" in failed.stdout + failed.stderr
        fresh.expect(0)
        observed.append(
            (
                failed.returncode,
                pattern.findall(failed.stdout),
                fresh.returncode,
                pattern.findall(fresh.stdout),
            )
        )
    assert observed[0] == observed[1]


_CLEANUP_CONFTEST = """import warnings
from pathlib import Path

def pytest_unconfigure():
    print('HOOK_UNCONFIGURE')
    warnings.warn('unconfigure warning payload')

def pytest_warning_recorded(warning_message):
    Path('recorded-warning').touch()
"""


@pytest.mark.parametrize("filter_action", ["default", "ignore", "error"])
def test_unconfigure_warning_filters_publish_without_recording(
    tmp_path: Path, filter_action: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_value():\n    pass\n")
    project.write("conftest.py", _CLEANUP_CONFTEST)
    pattern = re.compile(r"HOOK_UNCONFIGURE")
    observed = []
    for engine in ("stock", "native"):
        result = project.run_engine(engine, "-q", "-W", filter_action, env=_ENV)
        assert not (tmp_path / "recorded-warning").exists(), result.stdout + result.stderr
        observed.append(
            (
                result.returncode,
                pattern.findall(result.stdout),
                "UserWarning: unconfigure warning payload" in result.stderr,
                "unconfigure warning payload" in result.stderr,
            )
        )
    assert observed[0] == observed[1]
    if filter_action == "error":
        assert observed[0][3]


@pytest.mark.parametrize("workers", [1, 2])
def test_final_machine_result_includes_edits_after_terminal_summary(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_value():\n    pass\n")
    project.write(
        "conftest.py",
        """import json
import os
from pathlib import Path
import pytest

# A worker's parent process has already loaded this file.
Path(f'process-{os.getpid()}').touch()

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session):
    yield
    if Path(f'process-{os.getppid()}').exists():
        return
    terminal = session.config.pluginmanager.get_plugin('terminalreporter')
    passed = [] if terminal is None else terminal.stats.get('passed', [])
    if not passed:
        return
    report = passed[0]
    report.outcome = 'failed'
    report.longrepr = 'late result payload'
    Path('late.json').write_text(json.dumps([report.nodeid, report.outcome, report.longreprtext]))

def pytest_terminal_summary():
    print('JSON_IN_SUMMARY:' + str(Path('native.json').exists()))

def pytest_unconfigure():
    print('JSON_IN_UNCONFIGURE:' + str(Path('native.json').exists()))
""",
    )
    oracle = project.run_python("-m", "pytest", "-q", "-rA", env=_ENV)
    expected = json.loads((tmp_path / "late.json").read_text())
    (tmp_path / "late.json").unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-q",
        "-rA",
        f"--workers={workers}",
        "--native-result-json=native.json",
        env=_ENV,
    )
    diagnostic = oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    assert oracle.returncode == candidate.returncode == 0, diagnostic
    assert json.loads((tmp_path / "late.json").read_text()) == expected, diagnostic
    result = json.loads((tmp_path / "native.json").read_text())
    call = next(phase for phase in result["cases"][0]["phases"] if phase["phase"] == "call")
    assert [result["cases"][0]["nodeid"], call["outcome"], call["detail"]] == expected
    if workers == 2:
        assert call["report_summary"]["message"] == "late result payload", diagnostic
    assert "JSON_IN_SUMMARY:False" in candidate.stdout, diagnostic
    assert "JSON_IN_UNCONFIGURE:False" in candidate.stdout, diagnostic
