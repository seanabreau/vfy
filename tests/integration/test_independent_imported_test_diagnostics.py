"""Early-imported test helpers retain rewritten assertions and traceback paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_HELPER = """from pathlib import Path

with Path(__file__).with_name('imports.jsonl').open('a') as output:
    output.write(__name__ + '\\n')

def fail(value):
    assert value == 2, 'helper message'

def test_later_collection():
    pass
"""

_CASES = """from pathlib import Path
import pytest

with Path(__file__).with_name('imports.jsonl').open('a') as output:
    output.write('test_00_imports\\n')

import HELPER as automatic
import plain_helper
import registered_helper
import test_optout

helpers = {
    'automatic': automatic,
    'plain': plain_helper,
    'registered': registered_helper,
    'optout': test_optout,
}

@pytest.mark.parametrize('kind', ['automatic', 'plain', 'registered', 'optout'])
def test_failure(kind):
    helpers[kind].fail(1)
"""

_OBSERVER = """import json
from pathlib import Path
import pytest

ROOT = Path(__file__).parent
pytest.register_assert_rewrite('registered_helper')

@pytest.fixture(autouse=True)
def directory(monkeypatch):
    if CHANGE_CWD:
        monkeypatch.chdir(CHANGED_DIRECTORY)

def pytest_collectreport(report):
    if report.nodeid == 'HELPER.py':
        with (ROOT / 'imports.jsonl').open('a') as output:
            output.write('collected:HELPER\\n')

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if call.when == 'call' and call.excinfo is not None:
        graph = report.longrepr
        value = {
            'nodeid': report.nodeid,
            'outcome': report.outcome,
            'exception': [type(call.excinfo.value).__name__, str(call.excinfo.value)],
            'text': report.longreprtext,
            'crash': [graph.reprcrash.path, graph.reprcrash.lineno, graph.reprcrash.message],
            'entries': [[entry.style, entry.reprfileloc.path, entry.reprfileloc.lineno,
                         entry.reprfileloc.message, entry.lines]
                        for entry in graph.reprtraceback.reprentries],
        }
        with (ROOT / 'reports.jsonl').open('a') as output:
            output.write(json.dumps(value) + '\\n')
    return report
"""


@pytest.mark.parametrize("change_cwd", [False, True])
@pytest.mark.parametrize("helper", ["test_zz_helper", "zz_case_helper"])
def test_early_test_helper_import_preserves_assertions_and_chdir_paths(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, helper: str, change_cwd: bool
) -> None:
    project = VfyProject(tmp_path)
    changed_directory = tmp_path_factory.mktemp("changed-cwd")
    naming = "python_files = test_*.py zz_case*.py\n" if helper == "zz_case_helper" else ""
    project.write("pytest.ini", "[pytest]\n" + naming)
    project.write(f"{helper}.py", _HELPER)
    project.write("plain_helper.py", _HELPER)
    project.write("registered_helper.py", _HELPER)
    project.write("test_optout.py", '"""PYTEST_DONT_REWRITE"""\n' + _HELPER)
    project.write("test_00_imports.py", _CASES.replace("HELPER", helper))
    project.write(
        "conftest.py",
        _OBSERVER.replace("HELPER", helper)
        .replace("CHANGE_CWD", repr(change_cwd))
        .replace("CHANGED_DIRECTORY", repr(str(changed_directory))),
    )
    oracle = project.run_python("-m", "pytest", "-q")
    expected = (tmp_path / "reports.jsonl").read_text()
    expected_imports = (tmp_path / "imports.jsonl").read_text()
    (tmp_path / "reports.jsonl").unlink()
    (tmp_path / "imports.jsonl").unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    diagnostic = oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    assert oracle.returncode == candidate.returncode == 1, diagnostic
    assert (tmp_path / "reports.jsonl").read_text() == expected, diagnostic
    assert (tmp_path / "imports.jsonl").read_text() == expected_imports, diagnostic
    assert expected_imports.splitlines() == [
        "test_00_imports",
        helper,
        "plain_helper",
        "registered_helper",
        "test_optout",
        f"collected:{helper}",
    ], diagnostic
    reports = [json.loads(line) for line in expected.splitlines()]
    for report in reports:
        rewritten = report["nodeid"].endswith(("[automatic]", "[registered]"))
        assert report["exception"] == [
            "AssertionError",
            "helper message\nassert 1 == 2" if rewritten else "helper message",
        ], diagnostic
        assert Path(report["crash"][0]).is_absolute(), diagnostic
        assert all(Path(entry[1]).is_absolute() == change_cwd for entry in report["entries"])
        helper_lines = report["entries"][-1][-1]
        assert any("^^" in line for line in helper_lines) != rewritten, diagnostic
