"""Mutation replay parses configured coverage options without collecting again."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


@requires_native_pytest
@pytest.mark.parametrize("execution", ["auto", "portable"])
@pytest.mark.parametrize("explicit_plugin", [False, True], ids=["options", "plugin-load"])
def test_mutations_keep_coverage_options_and_disable_replay_collection(
    tmp_path: Path, execution: str, explicit_plugin: bool
) -> None:
    project = VfyProject(tmp_path)
    plugin = "-p pytest_cov " if explicit_plugin else ""
    project.write(".coveragerc", "[run]\ncore = ctrace\n")
    project.write(
        "pyproject.toml",
        "[tool.pytest.ini_options]\n"
        "testpaths = ['tests']\n"
        "filterwarnings = ['error']\n"
        f"addopts = '{plugin}--cov=app --cov-report=json:coverage.json --cov-context=test'\n",
    )
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "conftest.py",
        "import json\nfrom pathlib import Path\nfrom vfy import Coverage\n\n"
        "def pytest_collection_modifyitems(config, items):\n"
        "    with Path('coverage-routes.jsonl').open('a') as stream:\n"
        "        stream.write(json.dumps({'disabled': config.option.no_cov,\n"
        "                                 'collecting': Coverage.current() is not None,\n"
        "                                 'nodes': [item.nodeid for item in items]}) + '\\n')\n",
    )
    project.write(
        "tests/test_app.py",
        "from app import value\n\ndef test_value():\n    assert value() == 1\n",
    )
    stock = project.run_pytest("-q", "-p", "pytest_cov", load_xdist=False)
    stock.expect(0)
    expected_coverage = json.loads((tmp_path / "coverage.json").read_text())
    (tmp_path / "coverage-routes.jsonl").unlink()
    (tmp_path / "coverage.json").unlink()
    result = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--data-file=.coverage-vfy",
        "--no-cache",
        "--format=json",
        "-q",
        env={"VFY_MUTATION_ENGINE": execution},
    )
    result.expect(0)
    report = json.loads(result.stdout)
    assert report["summary"] == {"protected": 1}, report
    mutation = report["mutations"][0]
    assert (mutation["phase"], mutation["killer"]) == (
        "call",
        "tests/test_app.py::test_value",
    ), report
    assert report["metrics"]["fast_results"] == int(execution == "auto"), report
    assert report["metrics"]["portable_fallbacks"] == int(execution == "portable"), report
    routes = [
        json.loads(line) for line in (tmp_path / "coverage-routes.jsonl").read_text().splitlines()
    ]
    assert routes[0] == {
        "disabled": False,
        "collecting": True,
        "nodes": ["tests/test_app.py::test_value"],
    }
    assert routes[1:]
    assert all(
        row == {"disabled": False, "collecting": False, "nodes": routes[0]["nodes"]}
        for row in routes[1:]
    )
    assert project.lines("app.py", ".coverage-vfy") == [1, 2]
    coverage = json.loads((tmp_path / "coverage.json").read_text())
    assert (
        coverage["files"]["app.py"]["executed_lines"]
        == expected_coverage["files"]["app.py"]["executed_lines"]
    )


@pytest.mark.parametrize("location", ["cli", "config", "environment"])
def test_replay_options_and_full_coverage_plugin_share_registration(
    tmp_path: Path, location: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "test_app.py", "from app import value\n\ndef test_value():\n    assert value() == 1\n"
    )
    plugin = " -p vfy.pytest_plugin" if location == "config" else ""
    project.write("pytest.ini", f"[pytest]\naddopts = --cov=app{plugin}\n")
    arguments = ["-p", "vfy.pytest_plugin"] if location == "cli" else []
    environment = {"PYTEST_PLUGINS": "vfy.pytest_plugin"} if location == "environment" else {}
    result = project.run_python(
        "-m",
        "pytest",
        "-p",
        "vfy.pytest_options",
        *arguments,
        "--no-cov",
        "-q",
        env=environment,
    )
    result.expect(0)
    assert "1 passed" in result.stdout
    assert not (tmp_path / ".coverage").exists()


@requires_native_pytest
@pytest.mark.parametrize("execution", ["auto", "portable"])
def test_mutations_preserve_coverage_option_branches(tmp_path: Path, execution: str) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = --cov=app --cov-report=\n")
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "test_app.py",
        "from pathlib import Path\nfrom app import value\n\n"
        "def test_value(pytestconfig):\n"
        "    observed = value()\n"
        "    Path('observed').write_text(str(observed))\n"
        "    if pytestconfig.option.no_cov:\n"
        "        assert observed == 1\n"
        "    else:\n"
        "        assert observed >= 0\n",
    )
    baseline = project.run_pytest("-q", "-p", "pytest_cov", load_xdist=False)
    baseline.expect(0)
    assert (tmp_path / "observed").read_text() == "1"
    project.write("app.py", "def value():\n    return 2\n\n")
    stock_mutant = project.run_pytest("-q", "-p", "pytest_cov", load_xdist=False)
    stock_mutant.expect(0)
    assert (tmp_path / "observed").read_text() == "2"
    project.write("app.py", "def value():\n    return 1\n")
    result = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--data-file=.coverage-vfy",
        "--no-cache",
        "--format=json",
        "-q",
        env={"VFY_MUTATION_ENGINE": execution},
    )
    result.expect(0)
    report = json.loads(result.stdout)
    assert report["summary"] == {"survived": 1}, report
    assert report["mutations"][0]["killer"] is None, report
