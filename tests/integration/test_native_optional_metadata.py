"""Disabled xdist metadata does not gate unrelated native pytest plugins."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="native execution requires Linux or macOS",
)
def test_native_without_xdist_does_not_require_xdist_distribution_inspection(
    tmp_path: Path,
) -> None:
    observations: list[str] = []
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("conftest.py", "import pytest\n@pytest.fixture\ndef answer(): return 42\n")
        project.write(
            "test_body.py",
            "from pathlib import Path\n"
            "def test_body(answer):\n"
            "    assert answer == 42\n"
            "    Path('executed').write_text('passed')\n",
        )
        probe = project.write(
            "probe/sitecustomize.py",
            "from importlib.metadata import Distribution\n"
            "original_discover = Distribution.discover\n"
            "def discover(cls, **kwargs):\n"
            "    if kwargs.get('name') == 'pytest-xdist':\n"
            "        raise RuntimeError('optional xdist inspection is unavailable')\n"
            "    return original_discover(**kwargs)\n"
            "Distribution.discover = classmethod(discover)\n",
        )
        environment = {
            "PYTHONPATH": str(probe.parent),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        result = (
            project.run_pytest("-q", env=environment, load_xdist=False)
            if route == "pytest"
            else project.run_vfy(
                "test", "--engine=native", "--no-replay", "--no-cov", "-q", env=environment
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observations.append((project.root / "executed").read_text())
    assert observations == ["passed", "passed"]
