"""Repeated report delivery preserves phase edits and retained-child chronology."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_OBSERVER = """import json,pytest
from pathlib import Path
originals={}
latest={}
deliveries=[]
def pytest_runtest_logreport(report):
    if isinstance(report,pytest.SubtestReport):
        deliveries.append(['child',report.context.msg,report.outcome])
    else:
        originals.setdefault(report.when,report)
        latest[report.when]=report
        deliveries.append([report.when,report.outcome,report.longrepr])
@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    result=yield
    for revision,when in enumerate(('teardown','setup','call','call'),1):
        report=originals[when]
        report.longrepr='actual '+when+' revision '+str(revision)
        if when=='call':report.outcome='failed'
        item.ihook.pytest_runtest_logreport(report=report)
    return result
def pytest_sessionfinish():
    Path('observations.json').write_text(json.dumps({
        'deliveries':deliveries,
        'latest':[[when,latest[when].outcome,latest[when].longrepr]
                  for when in ('setup','call','teardown')],
    }))
"""


def _observe(root: Path, engine: str, body: str, *, dependency: str = "") -> dict[str, Any]:
    project = VfyProject(root)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", _OBSERVER)
    project.write("test_body.py", f"from pathlib import Path\ndef test_body({dependency}):\n{body}")
    result = (
        project.run_engine("stock", "-q") if engine == "stock" else project.run_engine("native")
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert (project.root / "body-ran").is_file()
    return json.loads((project.root / "observations.json").read_text())


@pytest.mark.parametrize("scenario", ["same-object", "children"])
def test_republished_reports_update_phase_slots_without_reordering_deliveries(
    tmp_path: Path, scenario: str
) -> None:
    children = scenario == "children"
    body = (
        "    Path('body-ran').touch()\n"
        "    with subtests.test('first'): Path('first.body').touch()\n"
        "    with subtests.test('second'): Path('second.body').touch()\n"
        if children
        else "    Path('body-ran').touch()\n"
    )
    observed = {
        engine: _observe(tmp_path / engine, engine, body, dependency="subtests" if children else "")
        for engine in ("stock", "native")
    }
    assert observed["native"] == observed["stock"]
    assert observed["stock"]["latest"] == [
        ["setup", "passed", "actual setup revision 2"],
        ["call", "failed", "actual call revision 4"],
        ["teardown", "passed", "actual teardown revision 1"],
    ]
    deliveries: list[list[object]] = [["setup", "passed", None]]
    if children:
        deliveries.extend([["child", "first", "passed"], ["child", "second", "passed"]])
    deliveries.extend([["call", "passed", None], ["teardown", "passed", None]])
    deliveries.extend(
        [when, "failed" if when == "call" else "passed", detail]
        for when, detail in [
            ("teardown", "actual teardown revision 1"),
            ("setup", "actual setup revision 2"),
            ("call", "actual call revision 3"),
            ("call", "actual call revision 4"),
        ]
    )
    assert observed["stock"]["deliveries"] == deliveries


def test_republished_setup_and_teardown_do_not_create_an_absent_call(tmp_path: Path) -> None:
    observed = {}
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """import json,pytest
from pathlib import Path
reports={}
deliveries=[]
@pytest.fixture
def blocked(): pytest.skip('actual setup skip')
def pytest_runtest_logreport(report):
    reports[report.when]=report
    deliveries.append(report.when)
@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    result=yield
    assert 'call' not in reports
    for when in ('teardown','setup','teardown'):
        report=reports[when]
        if when=='setup':
            report.longrepr=('test_body.py',1,'Skipped: actual edited setup')
        item.ihook.pytest_runtest_logreport(report=report)
    return result
def pytest_sessionfinish():
    Path('observations.json').write_text(json.dumps({
        'deliveries':deliveries,'setup':reports['setup'].longrepr,
    }))
""",
        )
        project.write(
            "test_body.py",
            "from pathlib import Path\ndef test_body(blocked): Path('body-ran').touch()\n",
        )
        result = (
            project.run_engine("stock", "-q") if engine == "stock" else project.run_engine("native")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (project.root / "body-ran").exists()
        observed[engine] = json.loads((project.root / "observations.json").read_text())
    assert observed["native"] == observed["stock"]
    assert observed["stock"] == {
        "deliveries": ["setup", "teardown", "teardown", "setup", "teardown"],
        "setup": ["test_body.py", 1, "Skipped: actual edited setup"],
    }
