"""Empty parameter sets remain real collected outcomes under native policy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()

_COLLECTION_OBSERVER = """import json
from pathlib import Path

reports = []

def pytest_collectreport(report):
    crash = getattr(report.longrepr, 'reprcrash', None)
    reports.append({
        'nodeid': report.nodeid,
        'outcome': report.outcome,
        'when': report.when,
        'passed': report.passed,
        'failed': report.failed,
        'skipped': report.skipped,
        'longreprtext': report.longreprtext,
        'crash': [str(crash.path), crash.lineno, crash.message] if crash is not None else None,
        'stdout': report.capstdout,
        'stderr': report.capstderr,
        'log': report.caplog,
        'sections': list(report.sections),
        'result': [node.nodeid for node in report.result],
    })
    Path('collection-reports.json').write_text(json.dumps(reports))
"""


@pytest.mark.parametrize(
    ("policy", "status", "outcome"),
    [("skip", 0, "skipped"), ("xfail", 0, "xfailed"), ("fail_at_collect", 2, "errors")],
)
def test_empty_parameter_set_policy(tmp_path: Path, policy: str, status: int, outcome: str) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", f"[pytest]\nempty_parameter_set_mark = {policy}\n")
    project.write("conftest.py", _COLLECTION_OBSERVER)
    project.write(
        "test_empty.py",
        "import pytest\n@pytest.mark.parametrize('value', [])\n"
        "def test_empty(value):\n    raise AssertionError('must not execute')\n",
    )
    oracle = project.run_python("-m", "pytest", "-q")
    observed = project.root / "collection-reports.json"
    expected = json.loads(observed.read_text())
    observed.unlink()
    candidate = project.run_python("-m", "vfy.independent.entry", env={"PYTHONPATH": _SOURCE})
    assert oracle.returncode == candidate.returncode == status, (
        oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    )
    reports = json.loads(observed.read_text())
    assert reports == expected
    assert [(report["nodeid"], report["outcome"]) for report in reports] == [
        ("", "passed"),
        ("test_empty.py", "failed" if policy == "fail_at_collect" else "passed"),
        (".", "passed"),
    ]
    assert reports[1]["failed"] == (policy == "fail_at_collect")
    if policy == "fail_at_collect":
        assert reports[1]["longreprtext"] == "Empty parameter set in 'test_empty' at line 2"


def test_empty_parameter_error_location_uses_place_as(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nempty_parameter_set_mark = fail_at_collect\n")
    project.write("conftest.py", _COLLECTION_OBSERVER)
    project.write(
        "test_empty.py",
        "def reference(value): pass\n\n"
        "import pytest\n@pytest.mark.parametrize('value', [])\n"
        "def test_empty(value): raise AssertionError('must not execute')\n"
        "test_empty.place_as = reference\n",
    )
    oracle = project.run_python("-m", "pytest", "-q")
    observed = project.root / "collection-reports.json"
    expected_reports = json.loads(observed.read_text())
    observed.unlink()
    candidate = project.run_python("-m", "vfy.independent.entry", env={"PYTHONPATH": _SOURCE})
    assert oracle.returncode == candidate.returncode == 2, (
        oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    )
    assert json.loads(observed.read_text()) == expected_reports
    assert [(report["nodeid"], report["outcome"]) for report in expected_reports] == [
        ("", "passed"),
        ("test_empty.py", "failed"),
        (".", "passed"),
    ]
    assert expected_reports[1]["longreprtext"] == ("Empty parameter set in 'test_empty' at line 1")


def _empty_fixture_project(root: Path, *, policy: str = "", ids: str = "") -> VfyProject:
    project = VfyProject(root)
    project.write("pytest.ini", f"[pytest]\nempty_parameter_set_mark = {policy}\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

def pytest_collection_modifyitems(items):
    Path('nodes.json').write_text(json.dumps([item.nodeid for item in items]))
""",
    )
    project.write(
        "test_empty.py",
        """from pathlib import Path
import pytest

def identify(value):
    Path('ids-called').write_text('unexpected')
    return 'callable id'

@pytest.fixture(params=[], scope='module'IDS)
def resource(request):
    Path('fixture-ran').write_text('unexpected')
    return request.param

@pytest.fixture
def dependent(resource): raise AssertionError('empty dependency executed')

def test_first(resource): raise AssertionError('empty fixture test executed')
def test_second(dependent): raise AssertionError('empty fixture closure executed')
""".replace("IDS", ids),
    )
    return project


def _run_empty_fixture(project: VfyProject, *, outcome: str, status: int) -> None:
    oracle = project.run_python("-m", "pytest", "-q")
    assert oracle.returncode == status, oracle.stdout + oracle.stderr
    expected = (project.root / "nodes.json").read_text()
    project.write("stock-nodes.json", expected)
    (project.root / "nodes.json").unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    assert candidate.returncode == status, candidate.stdout + candidate.stderr
    assert (project.root / "nodes.json").read_text() == expected
    assert not (project.root / "fixture-ran").exists()
    assert not (project.root / "ids-called").exists()
    if status == 0:
        assert f"2 {outcome}" in oracle.stdout
        assert f"2 {outcome}" in candidate.stdout
        assert json.loads(expected) == [
            "test_empty.py::test_first[NOTSET]",
            "test_empty.py::test_second[NOTSET]",
        ]
    else:
        for result in (oracle, candidate):
            assert "Empty parameter set in 'test_first'" in result.stdout + result.stderr
            assert "1 error" in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("policy", "status", "outcome"),
    [
        ("", 0, "skipped"),
        ("skip", 0, "skipped"),
        ("xfail", 0, "xfailed"),
        ("fail_at_collect", 2, "errors"),
    ],
)
def test_empty_fixture_retains_rows_and_existing_empty_policy(
    tmp_path: Path,
    policy: str,
    status: int,
    outcome: str,
) -> None:
    _run_empty_fixture(
        _empty_fixture_project(tmp_path, policy=policy), outcome=outcome, status=status
    )


@pytest.mark.parametrize("ids", ["", ", ids=[]", ", ids=identify"])
def test_empty_fixture_uses_notset_id_without_calling_explicit_ids(
    tmp_path: Path, ids: str
) -> None:
    _run_empty_fixture(_empty_fixture_project(tmp_path, ids=ids), outcome="skipped", status=0)


def test_empty_fixture_mixed_with_direct_and_nonempty_fixture_rows_keeps_order(
    tmp_path: Path,
) -> None:
    project = _empty_fixture_project(tmp_path)
    project.write(
        "test_empty.py",
        """import json
from pathlib import Path
import pytest

@pytest.fixture(params=[1, 2], scope='module', ids=['one', 'two'])
def normal(request):
    with Path('events').open('a') as output: output.write(f'setup {request.param}\\n')
    yield request.param
    with Path('events').open('a') as output: output.write(f'teardown {request.param}\\n')

@pytest.fixture(params=[])
def empty(request): raise AssertionError('empty fixture executed')

@pytest.mark.parametrize('label', ['left', 'right'])
def test_mixed(normal, empty, label): raise AssertionError('empty product executed')

def test_regular(normal):
    with Path('events').open('a') as output: output.write(f'test {normal}\\n')
""",
    )
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(0)
    nodes = (project.root / "nodes.json").read_text()
    events = (project.root / "events").read_text()
    (project.root / "nodes.json").unlink()
    (project.root / "events").unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    candidate.expect(0)
    assert "2 passed, 4 skipped" in oracle.stdout
    assert "2 passed" in candidate.stdout
    assert "4 skipped" in candidate.stdout
    assert (project.root / "nodes.json").read_text() == nodes
    assert (project.root / "events").read_text() == events


def test_empty_fixture_overridden_dependency_keeps_real_collected_item(tmp_path: Path) -> None:
    project = _empty_fixture_project(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

@pytest.fixture(params=[], scope='module')
def resource(request): raise AssertionError('empty base fixture executed')

def pytest_collection_modifyitems(items):
    Path('nodes.json').write_text(json.dumps([item.nodeid for item in items]))
""",
    )
    project.write(
        "test_empty.py",
        """import pytest
@pytest.fixture
def resource(resource): raise AssertionError('override executed')
def test_first(resource): raise AssertionError('empty override executed')
def test_second(resource): raise AssertionError('empty override executed')
""",
    )
    _run_empty_fixture(project, outcome="skipped", status=0)
