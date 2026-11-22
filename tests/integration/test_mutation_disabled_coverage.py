"""Private mutation selection preserves disabled public pytest coverage."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@pytest.mark.parametrize("configured_coverage", [False, True])
@pytest.mark.parametrize(
    "engine",
    [
        pytest.param(
            "native",
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="independent mutations require Linux"
            ),
        ),
        pytest.param("pytest-native", marks=requires_native_pytest),
    ],
)
def test_disabled_coverage_retains_public_state_and_collects_private_contexts(
    tmp_path: Path, configured_coverage: bool, engine: str
) -> None:
    project = VfyProject(tmp_path)
    addopts = (
        "--cov=app --cov-fail-under=100 --cov-report=xml:coverage.xml"
        if configured_coverage
        else ""
    )
    project.write(
        "pytest.ini",
        f"[pytest]\naddopts = {addopts}\nfilterwarnings = error\n",
    )
    project.write("app.py", "def value():\n    return 1\n")
    project.write(".coverage", "prior public data\n")
    project.write("coverage.xml", "prior public report\n")
    project.write(
        "stock_observer.py",
        """import json
from pathlib import Path

def pytest_runtest_logreport(report):
    with Path('phases.jsonl').open('a') as output:
        output.write(json.dumps([report.when, report.outcome]) + '\\n')
""",
    )
    project.write(
        "test_app.py",
        """import json
from pathlib import Path
from coverage import Coverage
from app import value

def test_value(pytestconfig, cov):
    observed = value()
    with Path('public.jsonl').open('a') as output:
        output.write(json.dumps({
            'disabled': pytestconfig.option.no_cov,
            'getoption': pytestconfig.getoption('no_cov'),
            'cov_fixture': cov is None,
            'coverage_current': Coverage.current() is None,
            'source': pytestconfig.option.cov_source,
            'context': pytestconfig.option.cov_context,
            'threshold': pytestconfig.option.cov_fail_under,
            'reports': pytestconfig.option.cov_report,
        }) + '\\n')
    assert observed == 1
""",
    )
    stock = project.run_python(
        "-B", "-m", "pytest", "-p", "pytest_cov", "-p", "stock_observer", "--no-cov", "-q"
    )
    stock.expect(0)
    expected = json.loads((tmp_path / "public.jsonl").read_text())
    assert expected["disabled"] is True
    assert expected["cov_fixture"] is True
    assert expected["coverage_current"] is True
    (tmp_path / "public.jsonl").unlink()
    (tmp_path / "phases.jsonl").unlink()

    result = project.run_vfy(
        "mutate",
        f"--engine={engine}",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--data-file=.coverage-selection",
        "--no-cache",
        "--format=json",
        "--no-cov",
        "-q",
    )
    result.expect(0)
    report = json.loads(result.stdout)
    assert report["summary"] == {"protected": 1}, report
    mutation = report["mutations"][0]
    assert mutation["killer"] == "test_app.py::test_value", report
    assert mutation["phase"] == "call", report
    observations = [
        json.loads(line) for line in (tmp_path / "public.jsonl").read_text().splitlines()
    ]
    assert observations
    assert all(observation == expected for observation in observations)
    data = project.data(".coverage-selection")
    try:
        assert "test_app.py::test_value|run" in data.measured_contexts()
    finally:
        data.close()
    assert (tmp_path / ".coverage").read_text() == "prior public data\n"
    assert (tmp_path / "coverage.xml").read_text() == "prior public report\n"

    project.write("app.py", "def value():\n    return 2\n")
    for bytecode in (tmp_path / "__pycache__").glob("app.*.pyc"):
        bytecode.unlink()
    mutant = project.run_python(
        "-B", "-m", "pytest", "-p", "pytest_cov", "-p", "stock_observer", "--no-cov", "-q"
    )
    mutant.expect(1)
    assert [json.loads(line) for line in (tmp_path / "phases.jsonl").read_text().splitlines()] == [
        ["setup", "passed"],
        ["call", "failed"],
        ["teardown", "passed"],
    ]


@pytest.mark.parametrize("options_first", [False, True])
def test_full_coverage_plugin_owns_fixtures_in_either_load_order(
    tmp_path: Path, options_first: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nfilterwarnings = error\n")
    project.write(
        "test_fixtures.py",
        """def test_fixtures(pytestconfig, cov, no_cover):
    assert cov is None
    assert no_cover is None
    assert pytestconfig.option.no_cov is True
    assert pytestconfig.pluginmanager.hasplugin('vfy.pytest_plugin')
    assert not pytestconfig.pluginmanager.hasplugin('_vfy_coverage_replay_fixtures')
""",
    )
    plugins = ["vfy.pytest_options", "vfy.pytest_plugin"]
    if not options_first:
        plugins.reverse()
    result = project.run_python(
        "-m", "pytest", "-p", plugins[0], "-p", plugins[1], "--no-cov", "-q"
    )
    result.expect(0)
