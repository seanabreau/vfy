"""Intentional status-zero exits preserve existing CI evidence and exports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.test_native_parallel import _native_parallel_available
from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    not _native_parallel_available(),
    reason="native parallel tests require Linux or macOS and a supported pytest version",
)


@pytest.mark.parametrize("workflow", ["selected", "export"])
def test_ci_intentional_exit_zero_preserves_previous_evidence_and_bundle(
    tmp_path: Path, workflow: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        ".gitignore",
        ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n"
        "history.json\nexecution.json\ntest-run.json\nreplay.coverage\nbaseline.json\nimport-graph.json\n"
        "requested-exit\n",
    )
    project.write("src/app.py", "def read_value():\n    return 1\n")
    project.write("src/other.py", "def read_other():\n    return 2\n")
    project.write(
        "test_case.py",
        "import os\nfrom pathlib import Path\nimport pytest\n"
        "from src.app import read_value\n"
        "def test_case():\n"
        "    assert read_value() == 1\n"
        "    if os.getenv('VFY_REQUEST_EXIT'):\n"
        "        Path('requested-exit').touch()\n"
        "        pytest.exit('CI requested exit sentinel', returncode=0)\n",
    )
    project.write(
        "test_other.py",
        "from src.other import read_other\ndef test_other():\n    assert read_other() == 2\n",
    )
    project.git_init()
    arguments = ("--history=history.json", "--source=src", "--cov-report=", "-q")
    environment = {"PYTEST_NATIVE_RESULT": str(tmp_path / "execution.json")}
    seed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--all",
        "--export-baseline=baseline.json",
        *arguments,
        env=environment,
    )
    seed.expect(0)
    preserved = {
        name: (tmp_path / name).read_bytes()
        for name in (
            ".coverage",
            "history.json",
            "execution.json",
            "test-run.json",
            "replay.coverage",
            "baseline.json",
        )
    }
    project.write("src/app.py", "def read_value():\n    return 1 + 0\n")
    project.git_commit("change covered source before requested exit")

    reference = project.run_pytest(
        "-q", "test_case.py", load_xdist=False, env={"VFY_REQUEST_EXIT": "1"}
    )
    reference.expect(0)
    assert "CI requested exit sentinel" in reference.stdout + reference.stderr
    (tmp_path / "requested-exit").unlink()
    exited = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        *(
            ("--baseline=baseline.json", "--plan-output=.vfy/exit-plan.json")
            if workflow == "selected"
            else ("--all", "--export-baseline=baseline.json")
        ),
        *arguments,
        env={**environment, "VFY_REQUEST_EXIT": "1"},
    )
    output = exited.stdout + exited.stderr
    assert (tmp_path / "requested-exit").exists(), output
    assert exited.returncode == reference.returncode, output
    assert "CI requested exit sentinel" in output
    if workflow == "selected":
        selection = json.loads((tmp_path / ".vfy/exit-plan.json").read_text())["selection"]
        assert selection["selected"] == ["test_case.py::test_case"]
        assert not selection["fallback_full"]
    assert {name: (tmp_path / name).read_bytes() for name in preserved} == preserved
