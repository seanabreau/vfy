"""Hypothesis core recognizes owned outcomes without its pytest plugin enabled."""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
@pytest.mark.parametrize(
    ("outcome", "status", "summary"),
    [("fail", 1, "failed"), ("skip", 0, "skipped"), ("xfail", 0, "xfailed")],
)
def test_hypothesis_core_recognizes_facade_outcomes(
    tmp_path: Path, outcome: str, status: int, summary: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_property.py",
        f"""from pathlib import Path
import pytest
from hypothesis import given, settings, strategies as st

@settings(max_examples=3, derandomize=True, database=None)
@given(st.integers(min_value=0, max_value=9))
def test_property(value):
    with Path('examples').open('a') as output:
        output.write(str(value) + '\\n')
    pytest.{outcome}('property outcome')
""",
    )
    oracle = project.run_python("-m", "pytest", "-q", "-p", "no:hypothesispytest")
    expected = (project.root / "examples").read_text().splitlines()
    (project.root / "examples").unlink()
    actual = project.run_engine("native", "-p", "no:hypothesispytest")
    observed = (project.root / "examples").read_text().splitlines()
    assert oracle.returncode == actual.returncode == status, (
        oracle.stdout + oracle.stderr + actual.stdout + actual.stderr
    )
    assert f"1 {summary}" in oracle.stdout
    assert f"1 {summary}" in actual.stdout
    assert expected[0] == observed[0] == "0"
    if outcome == "fail":
        assert len(expected) > 1
        assert len(observed) > 1
        for result in (oracle, actual):
            if version("hypothesis") == "6.152.1":
                assert "Falsifying example: test_property(" in result.stdout + result.stderr
            else:
                assert "Failing test case: test_property(" in result.stdout + result.stderr
            assert "value=0" in result.stdout + result.stderr
    elif outcome == "skip":
        assert expected == observed == ["0"]


_SHUTDOWN = """import atexit
import json
import sys
from pathlib import Path


def snapshot():
    Path('shutdown.json').write_text(json.dumps({
        'modules': sorted(
            name for name in sys.modules
            if name in ('pluggy', 'packaging', '_hypothesis_pytestplugin')
            or name == '_pytest' or name.startswith('_pytest.')
        ),
    }))


atexit.register(snapshot)
"""


@requires_native_pytest
def test_outcome_aliases_resolve_to_facade_objects_and_leave_no_engine_imports(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("observer.py", _SHUTDOWN)
    project.write(
        "test_property.py",
        """import pytest
from _pytest import outcomes, compat, fixtures, monkeypatch
from hypothesis import given, settings, strategies as st

@settings(max_examples=2, derandomize=True, database=None)
@given(st.booleans())
def test_property(value):
    assert outcomes.Failed is pytest.fail.Exception
    assert outcomes.Skipped is pytest.skip.Exception
    assert outcomes.XFailed is pytest.xfail.Exception
    assert compat.NOTSET is fixtures.NOTSET is monkeypatch.NOTSET
    assert compat.NotSetType is fixtures.NotSetType is monkeypatch.NotSetType
    assert monkeypatch.MonkeyPatch is pytest.MonkeyPatch
""",
    )
    actual = project.run_engine("native", "-p", "observer", "-p", "no:hypothesispytest")
    actual.expect(0)
    assert json.loads((tmp_path / "shutdown.json").read_text()) == {"modules": []}


@requires_native_pytest
def test_outcome_aliases_restore_after_early_configuration_failure(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("observer.py", _SHUTDOWN)
    project.write("conftest.py", "raise RuntimeError('early import failed')\n")
    project.write("test_pass.py", "def test_pass():\n    pass\n")
    oracle = project.run_pytest("-q", load_xdist=False)
    oracle.expect(4)
    actual = project.run_engine("native", "-p", "observer")
    actual.expect(4)
    assert json.loads((tmp_path / "shutdown.json").read_text()) == {"modules": []}
