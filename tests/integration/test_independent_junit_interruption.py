"""Interrupted items retain actual setup-only JUnit reporters without results."""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()
_CONFTEST = """import json
from pathlib import Path
import pytest

reports = []
original = None

def pytest_collection_modifyitems(items):
    assert len(items) == 3
    for item in items[:2]:
        item._nodeid = 'shared.py::same'

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    report.duration = {'setup': .125, 'call': .25, 'teardown': .5}[report.when]
    return report

def pytest_runtest_logreport(report):
    reports.append({'nodeid': report.nodeid, 'when': report.when,
                    'outcome': report.outcome, 'duration': report.duration,
                    'longrepr': str(report.longrepr)})

@pytest.fixture(autouse=True)
def resource(request):
    Path('setup-' + request.node.name).touch()
    yield
    Path('finalized-' + request.node.name).touch()

def stop():
    global original
    if CONTROL == 'keyboard':
        original = KeyboardInterrupt('actual control object')
        raise original
    pytest.exit('actual exit control', returncode=42)

def pytest_keyboard_interrupt(excinfo):
    if CONTROL == 'keyboard':
        assert excinfo.value is original
        Path('same-exception').touch()

def pytest_sessionfinish():
    Path('reports.json').write_text(json.dumps(reports))
"""
_CASE = """from pathlib import Path
from conftest import stop

def test_prefix():
    Path('body-prefix').touch()

def test_interrupted():
    Path('body-interrupted').touch()
    stop()

def test_never():
    Path('body-never').touch()
"""


@pytest.mark.parametrize(("control", "status"), [("keyboard", 2), ("exit", 42)])
@pytest.mark.parametrize("duration", ["total", "call"])
def test_setup_only_reporter_preserves_actual_interruption_and_xml_shape(
    tmp_path: Path, control: str, status: int, duration: str
) -> None:
    observed = []
    for module in ("pytest", "vfy.independent.entry"):
        project = VfyProject(tmp_path / module)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", f"CONTROL = {control!r}\n" + _CONFTEST)
        project.write("test_case.py", _CASE)
        arguments = [
            "-q",
            "--junitxml=results.xml",
            "-o",
            f"junit_duration_report={duration}",
        ]
        environment = {"PYTHONPATH": _SOURCE, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        if module == "pytest":
            result = project.run_python("-m", "pytest", *arguments, env=environment)
        else:
            result = project.run_vfy(
                "test",
                "--engine=native",
                "--no-cov",
                "--native-result-json=native.json",
                *arguments,
                env=environment,
            )
        assert result.returncode == status, result.stdout + result.stderr
        reports = json.loads((project.root / "reports.json").read_text())
        assert reports == [
            {
                "nodeid": "shared.py::same",
                "when": phase,
                "outcome": "passed",
                "duration": elapsed,
                "longrepr": "None",
            }
            for phase, elapsed in [
                ("setup", 0.125),
                ("call", 0.25),
                ("teardown", 0.5),
                ("setup", 0.125),
            ]
        ]
        assert (project.root / "body-prefix").exists()
        assert (project.root / "body-interrupted").exists()
        assert not (project.root / "body-never").exists()
        for name in ("test_prefix", "test_interrupted"):
            assert (project.root / f"setup-{name}").exists()
            assert (project.root / f"finalized-{name}").exists()
        assert not (project.root / "setup-test_never").exists()
        assert (project.root / "same-exception").exists() is (control == "keyboard")
        suite = ET.parse(project.root / "results.xml").getroot().find("testsuite")
        assert suite is not None
        assert {key: suite.attrib[key] for key in ("tests", "failures", "errors", "skipped")} == {
            "tests": "1",
            "failures": "0",
            "errors": "0",
            "skipped": "0",
        }
        cases = list(suite)
        assert all(case.tag == "testcase" and not list(case) for case in cases)
        expected = [
            {
                "classname": "shared",
                "name": "same",
                "time": "0.875" if duration == "total" else "0.250",
            }
        ]
        if duration == "total":
            expected.append({"time": "0.125"})
        assert [case.attrib for case in cases] == expected
        observed.append((reports, [case.attrib for case in cases]))
        if module == "vfy.independent.entry":
            record = json.loads((project.root / "native.json").read_text())
            assert record["exitstatus"] == status
            assert record["collected"] == 3
            assert record["counts"]["passed"] == 1
            assert [case["nodeid"] for case in record["cases"]] == ["shared.py::same"]
            assert [case["test_index"] for case in record["cases"]] == [0]
    assert observed[0] == observed[1]
