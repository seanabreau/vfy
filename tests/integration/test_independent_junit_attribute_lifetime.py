"""JUnit attribute recorders follow pytest reporter lifetime and conversion order."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("double_failure", [False, True])
def test_saved_attribute_recorder_tracks_finalization(tmp_path: Path, double_failure: bool) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

recorder = None
events = []

def event(label):
    events.append(label)
    Path('attribute-events.json').write_text(json.dumps(events))

class Name:
    def __str__(self):
        event('name')
        return 'custom'

class Value:
    def __str__(self):
        event('value')
        return 'data'

class FailingValue:
    def __str__(self):
        event('failing value')
        raise ValueError('conversion failed')

def invoke(stage):
    event(stage)
    try:
        recorder(Name(), Value())
    except AttributeError:
        event('AttributeError')
    else:
        event('accepted')
    try:
        recorder(Name(), FailingValue())
    except ValueError as error:
        event(str(error))

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report):
    if report.when == 'teardown':
        invoke('before finalization')

@pytest.hookimpl(trylast=True, specname='pytest_runtest_logreport')
def pytest_late_runtest_logreport(report):
    if report.when == 'teardown':
        invoke('after finalization')

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session):
    invoke('session finish')
""",
    )
    project.write(
        "test_case.py",
        f"""import pytest
import conftest

@pytest.fixture
def resource():
    yield
    if {double_failure!r}:
        raise RuntimeError('teardown failure')

def test_case(record_xml_attribute, resource):
    conftest.recorder = record_xml_attribute
    record_xml_attribute('initial', 'value')
    if {double_failure!r}:
        raise AssertionError('call failure')
""",
    )
    arguments = ("-q", "--junitxml=results.xml", "-o", "junit_family=legacy")
    snapshots = []
    for engine in ("pytest", "vfy.independent.entry"):
        (tmp_path / "attribute-events.json").unlink(missing_ok=True)
        result = (
            project.run_python("-m", "pytest", *arguments)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
        )
        assert result.returncode == (1 if double_failure else 0), result.stdout + result.stderr
        snapshots.append(json.loads((tmp_path / "attribute-events.json").read_text()))
    assert (
        snapshots
        == [
            [
                "before finalization",
                "value",
                "name",
                "accepted",
                "failing value",
                "conversion failed",
                "after finalization",
                "value",
                "AttributeError",
                "failing value",
                "conversion failed",
                "session finish",
                "value",
                "AttributeError",
                "failing value",
                "conversion failed",
            ]
        ]
        * 2
    )


@pytest.mark.parametrize("failing_phase", ["call", "teardown"])
def test_double_failure_property_conversion_controls_recorder_lifetime(
    tmp_path: Path, failing_phase: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        f"""import json
from pathlib import Path
import pytest

recorder = None
events = []

class Value:
    def __str__(self):
        events.append('property conversion')
        raise ValueError('property conversion failed')

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == {failing_phase!r}:
        report.user_properties = [('property', Value())]
    return report

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session):
    try:
        recorder('late', 'value')
    except AttributeError:
        events.append('finalized')
    else:
        events.append('open')
    Path('events.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_case.py",
        """import pytest
import conftest

@pytest.fixture
def resource():
    yield
    raise RuntimeError('teardown failure')

def test_case(record_xml_attribute, resource):
    conftest.recorder = record_xml_attribute
    raise AssertionError('call failure')
""",
    )
    observations = []
    for engine in ("pytest", "native"):
        (tmp_path / "events.json").unlink(missing_ok=True)
        result = (
            project.run_python("-m", "pytest", "-q", "--junitxml=results.xml")
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=native", "--no-cov", "-q", "--junitxml=results.xml"
            )
        )
        assert result.returncode == 3, result.stdout + result.stderr
        assert "property conversion failed" in result.stdout + result.stderr
        observations.append(json.loads((tmp_path / "events.json").read_text()))
    assert (
        observations
        == [["property conversion", "open" if failing_phase == "call" else "finalized"]] * 2
    )
