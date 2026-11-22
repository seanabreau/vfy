"""Native parallel HTML preserves pytest-metadata's worker updates."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject


def _native_parallel_available() -> bool:
    if os.name == "nt" or not sys.platform.startswith(("linux", "darwin")):
        return False
    try:
        return (
            importlib.util.find_spec("vfy._pytest.native_pytest") is not None
            and importlib.metadata.version("pytest").split(".", maxsplit=1)[0] in {"8", "9"}
            and importlib.util.find_spec("xdist") is not None
            and importlib.util.find_spec("pytest_html") is not None
            and importlib.util.find_spec("pytest_metadata") is not None
        )
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


pytestmark = pytest.mark.skipif(
    not _native_parallel_available(),
    reason=(
        "native parallel metadata parity requires Linux or macOS, xdist, pytest-html, "
        "and pytest-metadata"
    ),
)


@pytest.mark.parametrize("route", ["reference", "native"])
def test_parallel_html_includes_metadata_from_each_worker(tmp_path: Path, route: str) -> None:
    project = VfyProject(tmp_path)
    metadata_module = "pytest_metadata.plugin" if route == "reference" else "vfy.pytest_metadata"
    project.write(
        "conftest.py",
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n\n"
        f"from {metadata_module} import metadata_key\n\n"
        "@pytest.fixture(scope='session', autouse=True)\n"
        "def worker_metadata(metadata):\n"
        "    root = Path('.vfy')\n"
        "    root.mkdir(exist_ok=True)\n"
        "    pid = os.getpid()\n"
        "    metadata[f'Runtime worker {pid}'] = 'seen'\n"
        "    (root / f'metadata-worker-{pid}').touch()\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    pid = os.getpid()\n"
        "    session.config.stash[metadata_key][f'Finished worker {pid}'] = 'yes'\n",
    )
    project.write("tests/test_alpha.py", "def test_alpha():\n    assert True\n")
    project.write("tests/test_beta.py", "def test_beta():\n    assert True\n")

    if route == "reference":
        completed = project.run_pytest(
            "-p",
            "xdist.plugin",
            "-p",
            "pytest_metadata.plugin",
            "-p",
            "pytest_html.plugin",
            "-n",
            "2",
            "--html=report.html",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            load_xdist=False,
        )
    else:
        completed = project.run_vfy(
            "test", "--engine=native", "-j", "2", "--html=report.html", "-q"
        )

    completed.expect(0)
    assert "2 passed" in completed.stdout
    worker_records = list((tmp_path / ".vfy").glob("metadata-worker-*"))
    assert len(worker_records) == 2
    worker_pids = {record.name.removeprefix("metadata-worker-") for record in worker_records}
    assert len(worker_pids) == 2
    report = (tmp_path / "report.html").read_text(encoding="utf-8")
    for pid in worker_pids:
        assert f"Runtime worker {pid}" in report
        assert f"Finished worker {pid}" in report
    assert "seen" in report
    assert "yes" in report
