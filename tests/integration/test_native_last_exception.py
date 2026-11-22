"""Native calls keep pytest's ``sys.last_*`` post-mortem state."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CONFTEST = """\
import json
import sys
from pathlib import Path

import pytest

records = []


def innermost(traceback):
    while traceback is not None and traceback.tb_next is not None:
        traceback = traceback.tb_next
    return None if traceback is None else traceback.tb_frame.f_code.co_name


def observe(phase):
    value = getattr(sys, "last_value", None)
    return (
        phase,
        repr(value),
        getattr(sys, "last_type", None) is (None if value is None else type(value)),
        getattr(sys, "last_exc", None) is value,
        innermost(getattr(sys, "last_traceback", None)),
    )


@pytest.fixture(autouse=True)
def last_exception(request):
    records.append((request.node.name, *observe("setup")))
    yield
    records.append((request.node.name, *observe("teardown")))


def pytest_sessionfinish():
    Path("records.json").write_text(json.dumps(records))
"""

_TESTS = """\
import pytest


def test_error():
    raise ValueError("boom")


def test_after_error():
    pass


def test_skip():
    pytest.skip("unavailable")


def test_fail():
    pytest.fail("explicit")


def test_assertion():
    assert 1 == 2
"""


def test_failed_call_keeps_last_exception_until_the_next_call(tmp_path: Path) -> None:
    records = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", _CONFTEST)
        project.write("test_calls.py", _TESTS)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(1)
        records[engine] = json.loads((project.root / "records.json").read_text())
    assert records["pytest-native"] == records["pytest"]
    teardown = {name: row for name, phase, *row in records["pytest-native"] if phase == "teardown"}
    assert teardown["test_error"] == ["ValueError('boom')", True, True, "test_error"]
    assert teardown["test_skip"][0] == "None"
