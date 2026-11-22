"""Phase wrappers observe the pinned pytest builtin wrapper ordering."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("priority", ["tryfirst", "normal", "trylast"])
def test_phase_wrapper_results_match_pytest(tmp_path: Path, workers: int, priority: str) -> None:
    project = VfyProject(tmp_path)
    options = "" if priority == "normal" else f", {priority}=True"
    project.write(
        "conftest.py",
        f"""import json
from pathlib import Path
import pytest

def event(item, phase, result):
    with Path(item.nodeid + '.events').open('a') as output:
        output.write(json.dumps([phase, result]) + '\\n')

@pytest.hookimpl(wrapper=True{options})
def pytest_runtest_setup(item):
    result = yield
    event(item, 'setup', result)
    return result

@pytest.hookimpl(wrapper=True{options})
def pytest_runtest_call(item):
    result = yield
    event(item, 'call', result)
    return result

@pytest.hookimpl(wrapper=True{options})
def pytest_runtest_teardown(item, nextitem):
    result = yield
    event(item, 'teardown', result)
    return result
""",
    )
    for name in ("a", "b"):
        project.write(f"test_{name}.py", "def test_pass():\n    pass\n")
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(0)
    expected = {path.name: path.read_text() for path in project.root.glob("*.events")}
    for path in project.root.glob("*.events"):
        path.unlink()
    native = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        env={"PYTHONPATH": isolated_vfy_path()},
    )
    native.expect(0)
    observed = {path.name: path.read_text() for path in project.root.glob("*.events")}
    assert observed == expected
