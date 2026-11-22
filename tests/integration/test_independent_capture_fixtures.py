"""Builtin stream fixtures retain ownership across native phase reporting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("kind", ["capsys", "capfd"])
@pytest.mark.parametrize("stage", ["pass", "setup", "call"])
@pytest.mark.parametrize("workers", [1, 2])
def test_capture_fixture_reads_reports_disabled_output_and_cleanup_match_pytest(
    tmp_path: Path, kind: str, stage: str, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"""import json, os, sys, pytest
from pathlib import Path
kind, stage = {kind!r}, {stage!r}
reports, reads, cleanups = [], [], []

def emit(text):
    print(text)
    print(text + ' error', file=sys.stderr)
    if kind == 'capfd':
        os.write(1, ('fd:' + text + '\\n').encode())
        os.write(2, ('fd:' + text + ' error\\n').encode())

@pytest.fixture
def stream(request):
    return request.getfixturevalue(kind)

@pytest.fixture(autouse=True)
def phases(stream, request):
    request.addfinalizer(lambda: cleanups.append(request.node.nodeid))
    emit('setup output')
    if stage == 'setup':
        raise RuntimeError('capture setup witness')
    yield
    emit('teardown output')
    captured = stream.readouterr()
    reads.append([request.node.nodeid, 'teardown', captured.out, captured.err])

def pytest_runtest_logreport(report):
    reports.append([report.nodeid, report.when, report.outcome,
                    report.sections, report.capstdout, report.capstderr])
    print('HOOK:' + report.when)

def pytest_unconfigure():
    if reports:
        Path('capture-' + str(os.getpid()) + '.json').write_text(json.dumps([
            reports, reads, cleanups,
        ]))
""",
    )
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            """from conftest import emit, reads, stage

def test_body(stream, request):
    captured = stream.readouterr()
    assert 'setup output\\n' in captured.out
    assert 'setup output error\\n' in captured.err
    reads.append([request.node.nodeid, 'setup', captured.out, captured.err])
    with stream.disabled():
        emit('disabled output')
    emit('call output')
    captured = stream.readouterr()
    assert 'call output\\n' in captured.out
    assert 'call output error\\n' in captured.err
    reads.append([request.node.nodeid, 'call', captured.out, captured.err])
    emit('unread call output')
    if stage == 'call':
        raise RuntimeError('capture call witness')
""",
        )
    oracle = project.run_python("-m", "pytest", "-q")
    assert oracle.returncode == (0 if stage == "pass" else 1), oracle.stdout + oracle.stderr
    expected = _capture_records(tmp_path)
    assert len(expected[0]) == (4 if stage == "setup" else 6)
    for path in tmp_path.glob("capture-*.json"):
        path.unlink()

    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "--native-result-json=results.json",
    )
    assert candidate.returncode == oracle.returncode, candidate.stdout + candidate.stderr
    assert _capture_records(tmp_path) == expected
    assert len(expected[2]) == 2
    for report in expected[0]:
        assert "HOOK:" not in report[4] + report[5]
        assert "disabled output" not in report[4] + report[5]
    if stage != "setup":
        assert "disabled output" in candidate.stdout
        assert "disabled output error" in candidate.stderr
    if workers == 2:
        raw = json.loads((tmp_path / "results.json").read_text())
        pids = {int(path.stem.removeprefix("capture-")) for path in tmp_path.glob("capture-*.json")}
        assert pids == set(raw["execution_topology"]["worker_pids"])
        assert len(pids) == 2


class CaptureReport(NamedTuple):
    nodeid: str
    when: str
    outcome: str
    sections: list[list[str]]
    stdout: str
    stderr: str


def _capture_records(root: Path) -> tuple[list[CaptureReport], list[list[str]], list[str]]:
    reports: list[CaptureReport] = []
    reads: list[list[str]] = []
    cleanups: list[str] = []
    for path in root.glob("capture-*.json"):
        local_reports, local_reads, local_cleanups = json.loads(path.read_text())
        reports.extend(CaptureReport(*report) for report in local_reports)
        reads.extend(local_reads)
        cleanups.extend(local_cleanups)
    return sorted(reports), sorted(reads), sorted(cleanups)
