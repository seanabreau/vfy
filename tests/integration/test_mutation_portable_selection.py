"""Portable body mutations execute their recorded scope with original options."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


@requires_native_pytest
@pytest.mark.parametrize("scope", ["directory", "configured-directory", "argument-file"])
def test_portable_selected_mutation_excludes_other_cases_and_modules(
    tmp_path: Path, scope: str
) -> None:
    project = VfyProject(tmp_path)
    addopts = "tests --keep-location tests/" if scope == "configured-directory" else ""
    project.write("pytest.ini", f"[pytest]\ntestpaths = tests\naddopts = {addopts}\n")
    project.write(
        "app.py",
        "def decorate(function):\n"
        "    def wrapper():\n"
        "        return function()\n"
        "    return wrapper\n"
        "@decorate\n"
        "def value():\n"
        "    return 1\n",
    )
    project.write(
        "conftest.py",
        "import json, os, pytest\nfrom pathlib import Path\n"
        "def pytest_addoption(parser):\n"
        "    parser.addoption('--keep-location')\n"
        "@pytest.fixture(autouse=True)\n"
        "def record_case(request):\n"
        "    with Path('executed.jsonl').open('a') as output:\n"
        "        output.write(json.dumps({'nodeid': request.node.nodeid,\n"
        "                                 'mutant': bool(os.getenv('VFY_MUTATION'))}) + '\\n')\n",
    )
    project.write(
        "tests/test_app.py",
        "import os\nfrom app import value\n"
        "def test_selected(pytestconfig):\n"
        "    assert pytestconfig.getoption('--keep-location') == 'tests/'\n"
        f"    assert pytestconfig.getini('addopts') == {addopts.split()!r}\n"
        "    assert value() in (1, 2)\n"
        "def test_outside_plan():\n"
        "    assert not os.getenv('VFY_MUTATION'), 'out-of-plan test ran'\n",
    )
    project.write(
        "tests/test_poison.py",
        "import os\n"
        "assert not os.getenv('VFY_MUTATION'), 'out-of-plan module imported'\n"
        "def test_unrelated():\n"
        "    pass\n",
    )
    if scope == "configured-directory":
        arguments: list[str] = []
    elif scope == "argument-file":
        project.write("arguments.txt", "--keep-location\ntests/\ntests\n")
        arguments = ["@arguments.txt"]
    else:
        arguments = ["tests", "--keep-location", "tests/"]
    stock = project.run_pytest(*arguments, "-q", load_xdist=False)
    stock.expect(0)
    assert "3 passed" in stock.stdout
    (tmp_path / "executed.jsonl").unlink()

    completed = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--max-mutations=1",
        "--no-cache",
        "--test-isolation=serial",
        "--format=json",
        *arguments,
    )
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"survived": 1}, report
    assert report["metrics"]["portable_fallbacks"] == 1, report
    expected = ["tests/test_app.py::test_selected"]
    assert report["mutations"][0]["tests"] == expected
    rows = [json.loads(line) for line in (tmp_path / "executed.jsonl").read_text().splitlines()]
    assert [row["nodeid"] for row in rows if row["mutant"]] == expected, rows
