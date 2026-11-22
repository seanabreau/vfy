"""Public mutation baseline collection behavior."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


@pytest.mark.parametrize(
    "engine",
    [
        pytest.param(
            "native",
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="independent mutations require Linux"
            ),
        ),
        pytest.param("pytest-native", marks=requires_native_pytest),
    ],
)
def test_fresh_baseline_without_source_contexts_runs_once(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_unrelated.py",
        "from pathlib import Path\n\n"
        "def test_unrelated():\n"
        "    with Path('runs').open('a') as output:\n"
        "        output.write('run\\n')\n",
    )
    result = project.run_vfy(
        "mutate",
        f"--engine={engine}",
        "--all",
        "--source",
        "app.py",
        "--format",
        "json",
    )
    assert result.returncode == 0, result
    assert json.loads(result.stdout)["summary"] == {"unreached": 1}
    assert (tmp_path / "runs").read_text().splitlines() == ["run"]
