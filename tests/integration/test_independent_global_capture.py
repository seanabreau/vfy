"""Global capture preserves real descriptors, retained streams, and fixture nesting."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import NamedTuple, TypedDict

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.skipif(sys.platform != "linux", reason="checks descriptor inheritance via procfs")
def test_global_fd_capture_files_are_not_inherited(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_fds.py",
        '''import json, os, subprocess, sys
from pathlib import Path

def test_descriptors():
    stat = os.fstat(1)
    child = r"""import json, os, sys
target = (int(sys.argv[1]), int(sys.argv[2]))
leaked = []
for name in os.listdir('/proc/self/fd'):
    fd = int(name)
    if fd < 3:
        continue
    try:
        stat = os.fstat(fd)
    except OSError:
        continue
    if (stat.st_dev, stat.st_ino) == target:
        leaked.append(fd)
print(json.dumps(leaked))
"""
    result = subprocess.run([sys.executable, '-c', child, str(stat.st_dev), str(stat.st_ino)],
                            stdout=subprocess.PIPE, text=True, close_fds=False, check=True)
    Path('leaks.json').write_text(result.stdout)
''',
    )
    stock = project.run_python("-m", "pytest", "-q", "--capture=fd")
    stock.expect(0)
    expected = json.loads((tmp_path / "leaks.json").read_text())
    (tmp_path / "leaks.json").unlink()
    actual = project.run_vfy("test", "--engine=native", "--no-cov", "-q", "--capture=fd")
    actual.expect(0)
    assert json.loads((tmp_path / "leaks.json").read_text()) == expected == []


@pytest.mark.parametrize("mode", ["default", "fd", "sys", "tee-sys", "no", "-s"])
@pytest.mark.parametrize("workers", [1, 2])
def test_global_capture_modes_retained_handlers_subprocesses_and_fixtures_match_pytest(
    tmp_path: Path, mode: str, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, logging, os, sys
from pathlib import Path
import pytest

def emit(label):
    print('PY:' + label, flush=True)
    print('ERR:' + label, file=sys.stderr, flush=True)
    os.write(1, ('FD:' + label + '\\n').encode())
    os.write(2, ('FDERR:' + label + '\\n').encode())

@pytest.fixture(autouse=True)
def phases():
    emit('setup')
    yield
    emit('teardown')

@pytest.fixture
def stale():
    handler = logging.StreamHandler(sys.stderr)
    logger = logging.getLogger('retained.system.stream')
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.info('RETAINED:setup')
    yield logger
    logger.info('RETAINED:teardown')
    logger.removeHandler(handler)

def pytest_runtest_logreport(report):
    with Path('reports-' + str(os.getpid()) + '.jsonl').open('a') as output:
        output.write(json.dumps([report.nodeid, report.when, report.outcome,
                                report.capstdout, report.capstderr, report.sections]) + '\\n')
    print('HOOK:' + report.when, flush=True)
""",
    )
    project.write(
        "test_capture.py",
        """import json, logging, os, select, subprocess, sys, threading
from pathlib import Path
import pytest
from conftest import emit

def test_direct(stale):
    emit('call')
    stale.info('RETAINED:call')
    sys.stdout.buffer.write(b'BINARY:stdout\\n')
    sys.stderr.buffer.write(b'BINARY:stderr\\n')
    with open(2, 'w', closefd=False) as stream:
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger('retained.fd.stream')
        logger.addHandler(handler)
        logger.propagate = False
        logger.setLevel(logging.INFO)
        try:
            logger.info('RETAINED:fd')
        finally:
            logger.removeHandler(handler)
    subprocess.run([sys.executable, '-c',
                    "import os; os.write(1, b'CHILD:stdout\\\\n'); "
                    "os.write(2, b'CHILD:stderr\\\\n')"],
                   check=True)

@pytest.mark.parametrize('kind', ['capsys', 'capfd'])
def test_fixture_disabled(request, kind):
    capture = request.getfixturevalue(kind)
    emit('fixture-before')
    sys.stdout.buffer.write(b'FIXTURE:binary\\n')
    first = capture.readouterr()
    with capture.disabled():
        emit('disabled-outer-before')
        with capture.disabled():
            emit('disabled-inner')
        emit('disabled-outer-after')
    emit('fixture-after')
    thread = threading.Thread(target=logging.getLogger('live.thread').warning,
                              args=('LIVE:thread',))
    thread.start()
    thread.join()
    emit('fixture-after-live-log')
    second = capture.readouterr()
    Path('reads-' + kind + '-' + str(os.getpid()) + '.json').write_text(
        json.dumps([first.out, first.err, second.out, second.err]))
    emit('fixture-unread')

def test_stdin():
    state = {}
    try:
        state['fileno'] = sys.stdin.fileno()
    except Exception as error:
        state['fileno_error'] = [type(error).__name__, str(error)]
    ready = bool(select.select([0], [], [], 0)[0])
    try:
        state['read'] = sys.stdin.read(1) if ready else 'not-ready'
    except Exception as error:
        state['read_error'] = [type(error).__name__, str(error)]
    Path('stdin-' + str(os.getpid()) + '.json').write_text(json.dumps(state))
""",
    )
    project.write(
        "test_second.py",
        """from conftest import emit

def test_second():
    emit('second file')
""",
    )
    mode_args = [] if mode == "default" else [mode if mode == "-s" else f"--capture={mode}"]
    log_args = ["--log-cli-level=INFO", "--log-cli-format=%(message)s", "--log-format=%(message)s"]
    stock = project.run_python("-m", "pytest", "-q", *mode_args, *log_args)
    stock.expect(0)
    expected = _records(tmp_path)
    _clear_records(tmp_path)
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "--native-result-json=native.json",
        *mode_args,
        *log_args,
    )
    candidate.expect(0)
    assert _records(tmp_path) == expected
    for report in expected["reports"]:
        assert "HOOK:" not in report.stdout + report.stderr
        assert "LIVE:thread" not in report.stdout + report.stderr
        assert "disabled-outer-after" not in report.stdout + report.stderr
    assert "HOOK:" in candidate.stdout
    assert "LIVE:thread" in candidate.stdout
    for marker in ("PY:", "ERR:", "FD:", "FDERR:", "RETAINED:", "BINARY:", "CHILD:"):
        assert _marker_lines(candidate.stdout + candidate.stderr, marker) == _marker_lines(
            stock.stdout + stock.stderr, marker
        )
    if workers == 2:
        topology = json.loads(tmp_path.joinpath("native.json").read_text())["execution_topology"]
        assert topology["active_workers"] == 2
        report_pids = {
            int(path.stem.removeprefix("reports-")) for path in tmp_path.glob("reports-*")
        }
        assert report_pids == set(topology["worker_pids"])


class CaptureReport(NamedTuple):
    nodeid: str
    phase: str
    outcome: str
    stdout: str
    stderr: str
    sections: list[list[str]]


class CaptureRecords(TypedDict):
    reports: list[CaptureReport]
    reads: list[tuple[str, list[str]]]
    stdin: list[dict[str, object]]


def _records(root: Path) -> CaptureRecords:
    return {
        "reports": sorted(
            CaptureReport(*json.loads(line))
            for path in root.glob("reports-*.jsonl")
            for line in path.read_text().splitlines()
        ),
        "reads": sorted(
            (path.name.split("-")[1], json.loads(path.read_text()))
            for path in root.glob("reads-*.json")
        ),
        "stdin": [json.loads(path.read_text()) for path in root.glob("stdin-*.json")],
    }


def _clear_records(root: Path) -> None:
    for pattern in ("reports-*.jsonl", "reads-*.json", "stdin-*.json"):
        for path in root.glob(pattern):
            path.unlink()


def _marker_lines(text: str, marker: str) -> list[str]:
    return sorted(re.findall(re.escape(marker) + r"[^\r\n]*", text))
