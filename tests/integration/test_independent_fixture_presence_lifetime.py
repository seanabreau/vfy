"""Fixture factory descriptor results have pytest's temporary lifetime."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_fixture_factory_self_is_released_before_second_lookup(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

events = []

class Temporary:
    def __del__(self):
        events.append("released")

class Factory:
    @property
    def __self__(self):
        events.append("lookup")
        return Temporary()

    def __call__(self):
        events.append("called")
        return "fixture-value"

@pytest.fixture
def resource():
    raise AssertionError("fixture factory was not replaced")

@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup(fixturedef):
    if fixturedef.argname == "resource":
        fixturedef.func = Factory()

def pytest_sessionfinish():
    Path("events.json").write_text(json.dumps(events))
""",
    )
    project.write(
        "test_case.py",
        """from conftest import events

class TestFactory:
    def test_value(self, resource):
        assert resource == "fixture-value"
        events.append("body")
""",
    )
    observations = tmp_path / "events.json"
    for module in ("pytest", "vfy.independent.entry"):
        result = project.run_python("-m", module, "-q", env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
        result.expect(0)
        assert json.loads(observations.read_text()) == [
            "lookup",
            "released",
            "lookup",
            "released",
            "called",
            "body",
        ]
        observations.unlink()
