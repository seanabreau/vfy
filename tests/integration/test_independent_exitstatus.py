"""Session hooks determine the final status without changing recorded case outcomes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()


@pytest.mark.parametrize(
    ("fails", "requested"), [(False, 1), (True, 0), (False, 42), (False, 300), (False, -1)]
)
def test_sessionfinish_override_is_the_authoritative_serial_status(
    tmp_path: Path, fails: bool, requested: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"""from pathlib import Path
import pytest

def pytest_sessionfinish(session, exitstatus):
    assert session.exitstatus is exitstatus
    assert exitstatus == pytest.ExitCode.{"TESTS_FAILED" if fails else "OK"}
    assert type(exitstatus) is {"pytest.ExitCode" if fails else "int"}
    assert session.testsfailed == {int(fails)}
    session.exitstatus = {requested}

def pytest_unconfigure(config):
    Path('unconfigured').write_text('finished')
""",
    )
    project.write("test_status.py", f"def test_status():\n    assert {not fails}\n")
    oracle = project.run_python("-m", "pytest", "-q")
    assert oracle.returncode == requested % 256, (oracle.stdout, oracle.stderr)
    candidate = project.run_python(
        "-m",
        "vfy.independent.entry",
        "--native-result-json=record.json",
        env={"PYTHONPATH": _SOURCE},
    )
    assert candidate.returncode == requested % 256, (candidate.stdout, candidate.stderr)
    record = json.loads((project.root / "record.json").read_text())
    assert record["exitstatus"] == requested
    assert record["counts"]["failed"] == int(fails)
    assert record["counts"]["passed"] == int(not fails)
    assert (project.root / "unconfigured").read_text() == "finished"


def test_parallel_session_policy_can_fail_passed_cases_without_fabricating_failures(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest
def pytest_sessionfinish(session, exitstatus):
    assert exitstatus == pytest.ExitCode.OK
    assert session.testsfailed == 0
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
""",
    )
    project.write("test_first.py", "def test_first(): pass\n")
    project.write("test_second.py", "def test_second(): pass\n")
    result = project.run_python(
        "-m",
        "vfy.independent.entry",
        "--workers=2",
        "--native-result-json=record.json",
        env={"PYTHONPATH": _SOURCE},
    )
    result.expect(1)
    record = json.loads((project.root / "record.json").read_text())
    assert record["exitstatus"] == 1
    assert record["counts"]["passed"] == 2
    assert record["counts"]["failed"] == record["counts"]["errors"] == 0
    assert record["execution_topology"]["active_workers"] == 2
