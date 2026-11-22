"""The plug-in comparison must not overwrite or reuse previous results."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "validation/coverage_plugins/package_smoke.py"


def test_rejects_nonempty_output_without_changing_it(tmp_path: Path) -> None:
    sentinel = tmp_path / "coverage.coverage"
    sentinel.write_bytes(b"previous result")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 2
    assert "output directory must be empty" in result.stderr
    assert list(tmp_path.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"previous result"
