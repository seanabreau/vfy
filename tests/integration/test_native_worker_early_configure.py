"""Preserve supported worker configure exits before ordinary configure hooks run."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

pytestmark = requires_native_pytest


@pytest.mark.parametrize("status", [0, 7])
def test_worker_configure_exit_matches_pytest_status(tmp_path: Path, status: int) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("stock", "native")}
    conftest = (
        "import os\nfrom pathlib import Path\nimport pytest\n"
        "def pytest_configure(config):\n"
        "    worker = os.environ.get('VFY_NATIVE_PARALLEL_INDEX')\n"
        "    if worker is not None:\n"
        "        Path(f'configure-{worker}.reached').touch()\n"
        f"        pytest.exit('worker-configure-exit-anchor', returncode={status})\n"
    )
    for project in projects.values():
        project.write("conftest.py", conftest)
        project.write(
            "test_case.py",
            "from pathlib import Path\ndef test_body():\n    Path('body-ran').touch()\n",
        )
    # The serial pytest oracle receives the same worker role read by this hook.
    stock = projects["stock"].run_pytest(
        "-q",
        load_xdist=False,
        env={"VFY_NATIVE_PARALLEL_INDEX": "0", "VFY_WORKER_COLLECTION": None},
    )
    native = projects["native"].run_vfy(
        "test",
        "--engine=native",
        "--workers=2",
        "--no-replay",
        "--no-cov",
        "-q",
        env={"VFY_WORKER_COLLECTION": "1"},
    )
    for project, result in ((projects["stock"], stock), (projects["native"], native)):
        assert result.returncode == status, result.stdout + result.stderr
        assert "worker-configure-exit-anchor" in result.stdout + result.stderr
        assert list(project.root.glob("configure-*.reached"))
        assert not (project.root / "body-ran").exists()
