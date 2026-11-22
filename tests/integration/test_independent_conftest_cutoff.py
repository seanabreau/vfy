"""Report roots do not replace pytest's configuration-based conftest cutoff."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("engine", ["stock", "native"])
@pytest.mark.parametrize("configuration", ["parent", "sibling"])
def test_conftest_cutoff_preserves_real_ancestor_hooks_and_fixture_chains(
    tmp_path: Path, engine: str, configuration: str
) -> None:
    project = VfyProject(tmp_path)
    parent = configuration == "parent"
    ini = "pytest.ini" if parent else "configuration/pytest.ini"
    project.write(ini, "[pytest]\n")
    common = """import json, pytest
from pathlib import Path
events = []
def pytest_addoption(parser):
    parser.addoption('--project-flag', action='store', default='default')
def pytest_runtest_logreport(report):
    events.append([report.nodeid, report.when, report.outcome])
def pytest_sessionfinish():
    Path('phases.json').write_text(json.dumps(events))
"""
    project.write(
        "conftest.py",
        common
        + "@pytest.fixture\ndef source(): return ['parent']\n"
        + "def pytest_sessionstart():\n    Path('parent-active').touch()\n",
    )
    project.write(
        "tests/conftest.py",
        ("import pytest\n" if parent else common)
        + (
            "@pytest.fixture\ndef source(source): return source + ['tests']\n"
            if parent
            else "@pytest.fixture\ndef source(): return ['tests']\n"
        ),
    )
    expected = ["parent", "tests"] if parent else ["tests"]
    project.write(
        "tests/test_body.py",
        f"""import json
from pathlib import Path
def test_body(source, pytestconfig):
    assert source == {expected!r}
    assert pytestconfig.getoption('project_flag') == 'selected'
    assert pytestconfig.rootpath.name == 'tests'
    assert pytestconfig.inipath.name == 'pytest.ini'
    Path('body.json').write_text(json.dumps(source))
""",
    )
    arguments = ("-q", "-c", ini, "--rootdir=tests", "--project-flag=selected", "tests")
    result = (
        project.run_pytest(*arguments, load_xdist=False)
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    )
    project.write("runner.stdout", result.stdout)
    project.write("runner.stderr", result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / "body.json").read_text()) == expected
    assert (tmp_path / "parent-active").exists() is parent
    assert json.loads((tmp_path / "phases.json").read_text()) == [
        ["test_body.py::test_body", phase, "passed"] for phase in ["setup", "call", "teardown"]
    ]


@pytest.mark.parametrize("engine", ["stock", "native"])
def test_initial_conftests_follow_actual_arguments_instead_of_the_report_root(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("reported/conftest.py", "raise AssertionError('unselected report root')\n")
    project.write(
        "selected/conftest.py",
        "import pytest\n@pytest.fixture\ndef actual(): return 'selected fixture'\n",
    )
    project.write(
        "selected/test_body.py",
        "from pathlib import Path\ndef test_body(actual):\n"
        "    assert actual == 'selected fixture'\n    Path('body-ran').touch()\n",
    )
    arguments = ("-q", "--rootdir=reported", "selected/test_body.py")
    result = (
        project.run_pytest(*arguments, load_xdist=False)
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    )
    project.write("runner.stdout", result.stdout)
    project.write("runner.stderr", result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "body-ran").is_file()
