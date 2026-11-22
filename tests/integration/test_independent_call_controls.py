"""Body controls retain completed cases and finalize at sessionfinish priority."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("control", ["exit", "keyboard"])
def test_body_control_preserves_completed_cases_and_fixture_cleanup_order(
    tmp_path: Path, control: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path
events = []

def pytest_configure(config):
    config.add_cleanup(lambda: Path('events.json').write_text(json.dumps(events)))
    config.add_cleanup(lambda: events.append('cleanup'))

@pytest.fixture
def resource():
    events.append('fixture setup')
    yield
    events.append('fixture finalizer')

def pytest_keyboard_interrupt(excinfo):
    events.append('interrupt:' + excinfo.type.__name__)

def pytest_sessionfinish():
    events.append('finish normal')

@pytest.hookimpl(trylast=True, specname='pytest_sessionfinish')
def pytest_finish_last():
    events.append('finish last')

def pytest_unconfigure():
    events.append('unconfigure')
""",
    )
    operation = (
        "pytest.exit('body stop', returncode=42)"
        if control == "exit"
        else "raise KeyboardInterrupt('body stop')"
    )
    project.write(
        "test_body.py",
        f"""import pytest
from conftest import events

def test_01_pass():
    events.append('prior body')

def test_02_control(resource):
    events.append('control body')
    {operation}

def test_03_unreached():
    events.append('unexpected body')
""",
    )
    stock = project.run_python("-m", "pytest", "-q")
    assert stock.returncode == (42 if control == "exit" else 2), stock.stdout + stock.stderr
    expected = json.loads((tmp_path / "events.json").read_text())
    assert expected == [
        "prior body",
        "fixture setup",
        "control body",
        "interrupt:" + ("Exit" if control == "exit" else "KeyboardInterrupt"),
        "finish normal",
        "fixture finalizer",
        "finish last",
        "unconfigure",
        "cleanup",
    ]
    (tmp_path / "events.json").unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        env={"VFY_INDEPENDENT_TOPOLOGY_OUTPUT": str(tmp_path / "native.json")},
    )
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert json.loads((tmp_path / "events.json").read_text()) == expected
    record = json.loads((tmp_path / "native.json").read_text())
    assert record["exitstatus"] == stock.returncode
    assert [case["nodeid"] for case in record["cases"]] == ["test_body.py::test_01_pass"]
    assert record["counts"]["passed"] == 1
