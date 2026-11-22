"""Require fork workers to finish unconfigure before accepting results or exits."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="collected fork requires Linux")


@pytest.mark.parametrize("scheduling", ["cases", "demand"])
@pytest.mark.parametrize("intentional_exit", [False, True])
def test_collected_fork_rejects_incomplete_cleanup_after_results_or_exit(
    tmp_path: Path, scheduling: str, intentional_exit: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_cases.py",
        """
import os
from pathlib import Path
import pytest


def incomplete_cleanup():
    Path('shutdown-worker').write_text(str(os.getpid()))
    os._exit(0)


@pytest.mark.parametrize('value', range(32))
def test_case(value, request):
    if value == 0:
        Path('body-worker').write_text(str(os.getpid()))
        if os.environ.get('INCOMPLETE_FORK_SHUTDOWN') == '1':
            request.config.add_cleanup(incomplete_cleanup)
            if os.environ.get('INTENTIONAL_FORK_EXIT') == '1':
                Path('intentional-worker').touch()
                pytest.exit('fork exit before incomplete cleanup', returncode=0)
""",
    )
    environment = {
        "VFY_WORKER_START": "fork",
        "VFY_NATIVE_SCHEDULING": scheduling,
        "VFY_WORKER_COLLECTION": "0",
        "VFY_NATIVE_STARTUP_GRACE_MS": "0",
        "PYTEST_NATIVE_RESULT": str(tmp_path / "execution.json"),
    }
    arguments = (
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--history=history.json",
        "--no-replay",
        "--no-cov",
        "--capture=sys",
        "--junitxml=results.xml",
        "-q",
    )
    seed = project.run_vfy(*arguments, env={**environment, "INCOMPLETE_FORK_SHUTDOWN": "0"})
    seed.expect(0)
    assert "32 passed" in seed.stdout
    names = ("history.json", "execution.json", "results.xml")
    preserved = {name: (tmp_path / name).read_bytes() for name in names}
    (tmp_path / "body-worker").unlink()
    rejected = project.run_vfy(
        *arguments,
        env={
            **environment,
            "INCOMPLETE_FORK_SHUTDOWN": "1",
            "INTENTIONAL_FORK_EXIT": "1" if intentional_exit else "0",
        },
    )
    output = rejected.stdout + rejected.stderr
    assert (tmp_path / "body-worker").exists(), output
    assert (tmp_path / "shutdown-worker").exists(), output
    assert (tmp_path / "intentional-worker").exists() is intentional_exit
    assert rejected.returncode == pytest.ExitCode.INTERNAL_ERROR, output
    assert "without a valid collection completion" in output
    assert {name: (tmp_path / name).read_bytes() for name in names} == preserved
    for name in ("body-worker", "shutdown-worker"):
        with pytest.raises(ProcessLookupError):
            os.kill(int((tmp_path / name).read_text()), 0)
