"""Independent workers start together and balance one large file by default."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CONFTEST = """import os
from pathlib import Path

def pytest_unconfigure(config):
    Path(f'unconfigured-{os.getpid()}').touch()
"""


def test_default_workers_split_one_large_file(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_many.py",
        "import os, pytest\nfrom pathlib import Path\n"
        "@pytest.mark.parametrize('n', range(40))\n"
        "def test_case(n):\n    Path(f'ran-{n}-{os.getpid()}').touch()\n",
    )

    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-q",
        "-n",
        "2",
        "--native-result-json=results.json",
    )

    result.expect(0)
    assert "40 passed" in result.stdout
    record = json.loads((tmp_path / "results.json").read_text())
    workers = set(record["execution_topology"]["worker_pids"])
    assert len(workers) == 2
    runners = {int(path.name.rsplit("-", 1)[1]) for path in tmp_path.glob("ran-*")}
    assert runners == workers
    assert len(list(tmp_path.glob("ran-*"))) == 40


def test_workers_beyond_the_collected_width_finish_their_sessions(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _CONFTEST)
    project.write("test_one.py", "def test_only():\n    pass\n")

    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-q",
        "-n",
        "3",
        "--native-result-json=results.json",
    )

    result.expect(0)
    assert "1 passed" in result.stdout
    record = json.loads((tmp_path / "results.json").read_text())
    assert len(record["execution_topology"]["worker_pids"]) == 1
    # The controller, the assigned worker, and both unassigned workers unconfigure.
    assert len(list(tmp_path.glob("unconfigured-*"))) == 4
