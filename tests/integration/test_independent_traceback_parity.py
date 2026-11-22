"""Plain pytest failure output under --tb=short and --tb=long matches stock pytest."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CASES = {
    "call_frames": """def helper(value):
    raise ValueError(f"bad {value}")


def middle(value):
    helper(value)


def test_call_frames():
    middle(7)
""",
    "setup_error": """import pytest


@pytest.fixture
def broken():
    raise RuntimeError("setup failed")


def test_setup(broken):
    pass
""",
    "teardown_error": """import pytest


@pytest.fixture
def cleanup():
    yield
    raise RuntimeError("teardown failed")


def test_teardown(cleanup):
    pass
""",
    "cause_chain": """def test_cause():
    try:
        int("x")
    except ValueError as error:
        raise KeyError("wrapped") from error
""",
    "context_chain": """def test_context():
    try:
        {}["missing"]
    except KeyError:
        raise RuntimeError("second")
""",
    "message_only": """import pytest


def test_message_only():
    pytest.fail("message only", pytrace=False)
""",
}


def _observe(root: Path, source: str, engine: str, style: str) -> tuple[int, str]:
    project = VfyProject(root)
    project.write("test_failure.py", source)
    arguments = ("-q", "-p", "no:cacheprovider", f"--tb={style}")
    if engine == "stock":
        result = project.run_python("-m", "pytest", *arguments)
    else:
        result = project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    # Each engine runs in its own directory, so the project root is normalized in output.
    return result.returncode, project.normalize(result.stdout)


@pytest.mark.parametrize("style", ["short", "long"])
@pytest.mark.parametrize("case", sorted(_CASES))
def test_plain_failure_output_matches_stock(tmp_path: Path, case: str, style: str) -> None:
    stock = _observe(tmp_path / "stock", _CASES[case], "stock", style)
    independent = _observe(tmp_path / "independent", _CASES[case], "independent", style)
    assert independent == stock
