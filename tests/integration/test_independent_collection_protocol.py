"""Native collection reports and Python collection protocols share one result."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()


def test_parameter_identifier_escaping_matches_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_ids.py",
        r"""import pytest

@pytest.fixture(params=['mañana', '東京', '😀', b'\xff', 'line\nend'])
def value(request):
    return request.param

def test_fixture(value):
    pass

@pytest.mark.parametrize('value', ['mañana', '東京', '😀', b'\xff', '\\path'])
def test_automatic(value):
    pass

@pytest.mark.parametrize('value', [pytest.param(1, id='mañana'), 2], ids=['unused', '東京'])
def test_explicit(value):
    pass

@pytest.mark.parametrize('value', ['mañana', '東京'], ids=lambda value: value)
def test_callback(value):
    pass
""",
    )
    candidate = project.run_python(
        "-m", "vfy.independent.entry", "--collect-only", env={"PYTHONPATH": _SOURCE}
    )
    oracle = project.run_python("-m", "pytest", "--collect-only", "-q")
    assert candidate.returncode == oracle.returncode == 0, (candidate.stderr, oracle.stderr)
    assert [line for line in candidate.stdout.splitlines() if "::" in line] == [
        line for line in oracle.stdout.splitlines() if "::" in line
    ]


def test_module_skip_is_a_collection_report_without_a_case(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_skipped.py",
        "import pytest\nprint('before skip')\n"
        "pytest.skip('optional dependency', allow_module_level=True)\n",
    )
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

reports = []

def pytest_collectreport(report):
    reports.append([report.nodeid, report.outcome])

def pytest_sessionfinish():
    Path('collection.json').write_text(json.dumps(reports))
""",
    )
    stock = project.run_pytest(load_xdist=False)
    stock.expect(5)
    expected_reports = json.loads((project.root / "collection.json").read_text())
    module_index = expected_reports.index(["test_skipped.py", "skipped"])
    (project.root / "collection.json").rename(project.root / "stock-collection.json")
    result = project.run_python(
        "-m",
        "vfy.independent.entry",
        "--native-result-json=record.json",
        env={"PYTHONPATH": _SOURCE},
    )
    result.expect(5)
    record = json.loads((project.root / "record.json").read_text())
    assert record["collected"] == 0
    assert record["cases"] == []
    assert record["counts"]["skipped"] == 1
    assert [
        [report["nodeid"], report["outcome"]] for report in record["collection_reports"]
    ] == expected_reports
    assert json.loads((project.root / "collection.json").read_text()) == expected_reports
    module = record["collection_reports"][module_index]
    assert module["nodeid"] == "test_skipped.py"
    assert module["outcome"] == "skipped"
    assert module["stdout"] == "before skip\n"


def test_generation_configuration_and_collection_mutation_share_items(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nbase = 10\n")
    project.write(
        "conftest.py",
        """import pytest

def pytest_addoption(parser):
    parser.addini('base', 'generated base', type='int', default=1)

def pytest_generate_tests(metafunc):
    if 'number' in metafunc.fixturenames:
        metafunc.parametrize('number', [metafunc.config.getini('base'), 20])

def pytest_collection_modifyitems(config, items):
    items.reverse()
    removed = items.pop()
    items[0].add_marker(pytest.mark.skip(reason='hook skip'))

def pytest_sessionfinish(session, exitstatus):
    assert session.testscollected == 1
    assert session.testsfailed == 0
    assert exitstatus == 0
""",
    )
    project.write("test_generated.py", "def test_number(number):\n    assert number == 20\n")
    result = project.run_python(
        "-m",
        "vfy.independent.entry",
        "--native-result-json=record.json",
        env={"PYTHONPATH": _SOURCE},
    )
    result.expect(0)
    record = json.loads((project.root / "record.json").read_text())
    assert record["deselected_nodeids"] == ["test_generated.py::test_number[10]"]
    assert record["collected"] == 1
    assert record["cases"][0]["nodeid"] == "test_generated.py::test_number[20]"
    assert record["counts"]["skipped"] == 1
