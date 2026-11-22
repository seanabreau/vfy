"""Lookup failures preserve live bindings and item failure protocols."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _run(project: VfyProject, *, expected_status: int) -> None:
    stock = project.run_python("-m", "pytest", "-q", "-p", "pytest_cov")
    project.write("stock.stdout", stock.stdout)
    project.write("stock.stderr", stock.stderr)
    record = project.root / "observations.json"
    expected = json.loads(record.read_text()) if record.exists() else None
    project.write("stock-observations.json", json.dumps(expected, indent=2))
    record.unlink(missing_ok=True)
    candidate = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-q", "-p", "vfy.independent.outcomes"
    )
    project.write("candidate.stdout", candidate.stdout)
    project.write("candidate.stderr", candidate.stderr)
    actual = json.loads(record.read_text()) if record.exists() else None
    project.write("candidate-observations.json", json.dumps(actual, indent=2))
    project.write("statuses.json", json.dumps([stock.returncode, candidate.returncode]))
    assert isinstance(expected, list), stock.stdout + stock.stderr
    assert expected, stock.stdout + stock.stderr
    assert isinstance(actual, list), candidate.stdout + candidate.stderr
    assert actual, candidate.stdout + candidate.stderr
    assert actual == expected
    assert stock.returncode == candidate.returncode == expected_status


def test_lookup_report_uses_actual_item_repr_failure_override(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

records = []
class Representation:
    def __str__(self): return 'ITEM-OVERRIDE-MESSAGE'
    def toterminal(self, writer): writer.line('ITEM-OVERRIDE-MESSAGE')
representation = Representation()
def pytest_collection_modifyitems(items):
    item = items[0]
    def failure(info):
        records.append(['override', info.value.request is item._request])
        return representation
    item.repr_failure = failure
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == 'call':
        records.append(['report', report.longrepr is representation, str(report.longrepr)])
    return report
def pytest_sessionfinish():
    Path('observations.json').write_text(json.dumps(records))
""",
    )
    project.write(
        "test_lookup.py", "def test_body(request): request.getfixturevalue('missing_fixture')\n"
    )
    _run(project, expected_status=1)
    assert json.loads((tmp_path / "stock-observations.json").read_text()) == [
        ["override", True],
        ["report", True, "ITEM-OVERRIDE-MESSAGE"],
    ]


def test_lookup_longrepr_field_mutations_reach_real_terminal(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

records = []
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == 'setup' and report.failed:
        representation = report.longrepr
        representation.tblines[:] = ['HOOK-SELECTED-SOURCE']
        representation.errorstring = 'HOOK-SELECTED-ERROR\\nsecond line'
        representation.filename = Path('hook-selected.py')
        representation.firstlineno = 40
        representation.argname = 'hook_selected'
        records.append([report.longrepr is representation, str(representation)])
    return report
def pytest_sessionfinish():
    Path('observations.json').write_text(json.dumps(records))
""",
    )
    project.write("test_lookup.py", "def test_body(missing_fixture): pass\n")
    _run(project, expected_status=1)
    expected = (
        "HOOK-SELECTED-SOURCE\nE       HOOK-SELECTED-ERROR\n"
        ">       second line\n\nhook-selected.py:41"
    )
    assert json.loads((tmp_path / "stock-observations.json").read_text()) == [[True, expected]]
    for route in ["stock", "candidate"]:
        output = (tmp_path / f"{route}.stdout").read_text()
        assert "HOOK-SELECTED-SOURCE" in output
        assert "HOOK-SELECTED-ERROR" in output
        assert "hook-selected.py:41" in output
