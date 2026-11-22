"""A lookalike pytest.exit exception is an internal error with the stock exit status."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_spoofed_exit_class_is_an_internal_error_like_stock(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path

events = []

class PretendExit(Exception):
    @property
    def __class__(self):
        return pytest.exit.Exception

    @property
    def returncode(self):
        return 77

@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session):
    yield
    raise PretendExit('ordinary hook failure')

def pytest_internalerror(excinfo):
    events.append(['internal', type(excinfo.value).__name__])

def pytest_sessionfinish(exitstatus):
    events.append(['finish', int(exitstatus)])

def pytest_unconfigure():
    Path('events.json').write_text(json.dumps(events))
""",
    )
    project.write("test_body.py", "def test_body():\n    pass\n")
    stock = project.run_engine("stock", "-q")
    stock.expect(3)
    expected = json.loads((tmp_path / "events.json").read_text())
    assert expected == [["internal", "PretendExit"], ["finish", 3]]
    (tmp_path / "events.json").unlink()

    candidate = project.run_engine("native")
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert json.loads((tmp_path / "events.json").read_text()) == expected
