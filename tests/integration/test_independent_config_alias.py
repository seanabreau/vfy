"""Private config imports refer to the active pytest facade's real types."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_config_exit_code_alias_matches_public_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "alias_plugin.py",
        """import json
from pathlib import Path
import pytest
from _pytest.config import Config, ExitCode

def pytest_configure(config):
    Path('alias.json').write_text(json.dumps({
        'exit_code_identity': ExitCode is pytest.ExitCode,
        'config_type': isinstance(config, Config),
        'codes': [[code.name, int(code)] for code in ExitCode],
    }))
""",
    )
    project.write("test_sample.py", "def test_sample(): pass\n")
    observations = []
    for route in ("stock", "independent"):
        result = (
            project.run_python("-m", "pytest", "-q", "-p", "alias_plugin")
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", "-p", "alias_plugin")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observations.append(json.loads((tmp_path / "alias.json").read_text()))
        (tmp_path / "alias.json").unlink()
    assert observations[0] == observations[1]
    assert observations[0]["exit_code_identity"] is True
    assert observations[0]["config_type"] is True
