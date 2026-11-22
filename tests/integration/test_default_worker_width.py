"""Default independent runs resolve automatic widths before native argument parsing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="independent execution requires Unix"
)


@pytest.mark.parametrize(("width", "expected"), [("auto", 2), ("logical", 3)])
@pytest.mark.parametrize("source", ["cli", "config"])
def test_default_worker_width_runs_the_resolved_owned_plan(
    tmp_path: Path, width: str, expected: int, source: str
) -> None:
    project = VfyProject(tmp_path)
    bootstrap = project.write(
        "bootstrap/sitecustomize.py",
        "import os\nos.cpu_count = lambda: 3\nos.sched_getaffinity = lambda pid: {0, 1}\n",
    )
    project.write(
        "conftest.py",
        "import os\nfrom pathlib import Path\nimport pytest\n"
        "@pytest.fixture(scope='session', autouse=True)\ndef lifetime():\n"
        "    yield\n    Path(f'finished-{os.getpid()}').touch()\n",
    )
    for name in ("a", "b", "c"):
        project.write(
            f"test_{name}.py",
            "import os\nfrom pathlib import Path\n"
            f"def test_{name}():\n    Path('{name}.pid').write_text(str(os.getpid()))\n",
        )
    arguments = (f"--workers={width}",) if source == "cli" else ()
    if source == "config":
        project.write("pyproject.toml", f'[tool.vfy.test]\nworkers = "{width}"\n')

    result = project.run_vfy(
        "test",
        *arguments,
        "--no-cov",
        "--native-result-json=owned.json",
        env={"PYTHONPATH": str(bootstrap.parent)},
    )

    result.expect(0)
    record = json.loads((tmp_path / "owned.json").read_text())
    assert record["engine"] == "vfy-independent-v1"
    topology = record["execution_topology"]
    assert topology["requested_workers"] == topology["active_workers"] == expected
    body_pids = {(tmp_path / f"{name}.pid").read_text() for name in ("a", "b", "c")}
    assert body_pids == {str(pid) for pid in topology["worker_pids"]}
    assert {
        path.name.removeprefix("finished-") for path in tmp_path.glob("finished-*")
    } == body_pids
    assert [case["nodeid"] for case in record["cases"]] == [
        f"test_{name}.py::test_{name}" for name in ("a", "b", "c")
    ]
    assert record["counts"]["passed"] == 3
