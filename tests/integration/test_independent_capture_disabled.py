"""Disabling the capture plugin preserves real IO and user fixture overrides."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("workers", [1, 2])
def test_disabled_capture_preserves_process_io_and_declared_capture_fixture(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

@pytest.fixture
def capsys():
    return 'caller override'

def pytest_runtest_logreport(report):
    with Path('phases-' + str(__import__('os').getpid()) + '.jsonl').open('a') as stream:
        stream.write(json.dumps([report.nodeid, report.when, report.outcome,
                                report.capstdout, report.capstderr]) + '\\n')
""",
    )
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            """import os, sys

def test_io(pytestconfig, capsys):
    assert pytestconfig.pluginmanager.getplugin('capture') is None
    assert pytestconfig.pluginmanager.getplugin('capturemanager') is None
    assert capsys == 'caller override'
    assert sys.stdin.fileno() == 0
    print('DISABLED:stdout', flush=True)
    print('DISABLED:stderr', file=sys.stderr, flush=True)
    os.write(1, b'DISABLED:descriptor-out\\n')
    os.write(2, b'DISABLED:descriptor-err\\n')
""",
        )
    stock = project.run_engine("stock", "-q", "-p", "no:capture")
    stock.expect(0)
    expected = _phases(tmp_path)
    for path in tmp_path.glob("phases-*.jsonl"):
        path.rename(path.with_suffix(".stock"))
    candidate = project.run_vfy(
        "test", "--engine=native", "--no-cov", f"--workers={workers}", "-p", "no:capture"
    )
    candidate.expect(0)
    assert _phases(tmp_path) == expected
    for stream in ("stdout", "stderr"):
        assert sorted(re.findall(r"DISABLED:[^\r\n]*", getattr(candidate, stream))) == sorted(
            re.findall(r"DISABLED:[^\r\n]*", getattr(stock, stream))
        )


@pytest.mark.parametrize("fixture", ["capsys", "capfd"])
@pytest.mark.parametrize("disabled", [False, True])
def test_only_plugin_blocking_removes_default_capture_fixtures(
    tmp_path: Path, fixture: str, disabled: bool
) -> None:
    _capture_fixture_availability(tmp_path, fixture, mode="blocked" if disabled else "uncaptured")


def _capture_fixture_availability(tmp_path: Path, fixture: str, *, mode: str) -> None:
    disabled = mode != "uncaptured"
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

def pytest_collection_modifyitems(items):
    Path('fixture-names.json').write_text(json.dumps([item.fixturenames for item in items]))

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    error = call.excinfo
    with Path('raw-errors.jsonl').open('a') as stream:
        stream.write(json.dumps({
            'when': report.when,
            'exception': type(error.value).__name__ if error else None,
            'message': str(error.value) if error else None,
            'longrepr_type': type(report.longrepr).__name__,
            'longrepr': str(report.longrepr),
        }) + '\\n')
    return report

def pytest_runtest_logreport(report):
    with Path('phases.jsonl').open('a') as stream:
        stream.write(json.dumps([report.when, report.outcome,
                                "fixture '" in str(report.longrepr)
                                and "not found" in str(report.longrepr)]) + '\\n')
""",
    )
    if mode == "unblocked":
        conftest = tmp_path / "conftest.py"
        project.write(
            "conftest.py",
            conftest.read_text()
            + """
def pytest_configure(config):
    assert config.pluginmanager.unblock('capture')
    assert config.pluginmanager.getplugin('capture') is None
    assert config.pluginmanager.getplugin('capturemanager') is None
""",
        )
    project.write(
        "test_capture.py",
        f"""def test_body({fixture}):
    print('fixture witness')
    assert {fixture}.readouterr().out == 'fixture witness\\n'
""",
    )
    arguments = ["-p", "no:capture"] if disabled else ["-s"]
    stock = project.run_engine("stock", "-q", *arguments)
    assert stock.returncode == int(disabled), stock.stdout + stock.stderr
    phases = tmp_path / "phases.jsonl"
    expected = [json.loads(line) for line in phases.read_text().splitlines()]
    names = json.loads(tmp_path.joinpath("fixture-names.json").read_text())
    project.write("stock-fixture-names.json", json.dumps(names))
    raw = tmp_path / "raw-errors.jsonl"
    stock_errors = [json.loads(line) for line in raw.read_text().splitlines()]
    if disabled:
        assert stock_errors[0]["exception"] == "FixtureLookupError"
    else:
        assert all(record["exception"] is None for record in stock_errors)
    raw.rename(tmp_path / "stock-raw-errors.jsonl")
    assert expected == (
        [["setup", "failed", True], ["teardown", "passed", False]]
        if disabled
        else [["setup", "passed", False], ["call", "passed", False], ["teardown", "passed", False]]
    )
    phases.rename(tmp_path / "stock-phases.jsonl")
    candidate = project.run_engine("native", *arguments)
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert json.loads(tmp_path.joinpath("fixture-names.json").read_text()) == names
    assert [json.loads(line) for line in phases.read_text().splitlines()] == expected


@pytest.mark.parametrize("fixture", ["capsys", "capfd"])
def test_unblocking_capture_does_not_restore_missing_default_fixtures(
    tmp_path: Path, fixture: str
) -> None:
    _capture_fixture_availability(tmp_path, fixture, mode="unblocked")


def test_later_plugin_blocking_preserves_initialized_capture_and_cached_fixture(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

@pytest.fixture(autouse=True)
def phases():
    yield
    print('LATE:teardown', flush=True)

def pytest_runtest_logreport(report):
    with Path('phases.jsonl').open('a') as stream:
        stream.write(json.dumps([report.when, report.outcome,
                                report.capstdout, report.capstderr]) + '\\n')
""",
    )
    project.write(
        "test_capture.py",
        """def test_body(request, pytestconfig):
    first = request.getfixturevalue('capsys')
    manager = pytestconfig.pluginmanager
    manager.set_blocked('capture')
    assert manager.is_blocked('capture')
    assert manager.getplugin('capturemanager') is not None
    assert request.getfixturevalue('capsys') is first
    print('LATE:fixture', flush=True)
    assert first.readouterr().out == 'LATE:fixture\\n'
""",
    )
    stock = project.run_engine("stock", "-q")
    stock.expect(0)
    phases = tmp_path / "phases.jsonl"
    expected = [json.loads(line) for line in phases.read_text().splitlines()]
    phases.rename(tmp_path / "stock-phases.jsonl")
    candidate = project.run_engine("native")
    candidate.expect(0)
    assert [json.loads(line) for line in phases.read_text().splitlines()] == expected
    assert "LATE:teardown" not in candidate.stdout


def _phases(root: Path) -> list[list[object]]:
    return sorted(
        json.loads(line)
        for path in root.glob("phases-*.jsonl")
        for line in path.read_text().splitlines()
    )
