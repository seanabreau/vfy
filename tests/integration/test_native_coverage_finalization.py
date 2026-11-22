"""Native serial coverage commits parent and subprocess data atomically."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(os.name != "posix", reason="native execution requires Unix")


def test_corrupt_serial_child_coverage_preserves_previous_artifacts(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\ndef baseline_only():\n    return 2\n")
    project.write(
        "test_app.py",
        "import json\nimport os\nfrom pathlib import Path\n"
        "from app import value, baseline_only\n"
        "def test_value():\n"
        "    assert value() == 1\n"
        "    if os.getenv('BAD_SHARD'):\n"
        "        base = Path(json.loads(os.environ['VFY_PROCESS_OPTIONS'])['data_file'])\n"
        "        base.with_name(base.name + '.bad').write_bytes(b'invalid coverage shard')\n"
        "        Path('created-bad-shard').touch()\n"
        "    else:\n"
        "        assert baseline_only() == 2\n",
    )
    arguments = (
        "test",
        "--engine=pytest-native",
        "--workers=1",
        "--no-replay",
        "--cov=app",
        "--cov-report=json:coverage.json",
        "-q",
    )
    baseline = project.run_vfy(*arguments)
    baseline.expect(0)
    artifacts = (".coverage", "coverage.json", ".vfy/test-history.sqlite3")
    preserved = {name: (tmp_path / name).read_bytes() for name in artifacts}

    rejected = project.run_vfy(*arguments, env={"BAD_SHARD": "1"})

    assert (tmp_path / "created-bad-shard").exists(), rejected.stdout + rejected.stderr
    assert rejected.returncode != 0, rejected.stdout + rejected.stderr
    assert "file is not a database" in rejected.stdout + rejected.stderr
    assert {name: (tmp_path / name).read_bytes() for name in artifacts} == preserved
    recovered = project.run_vfy(*arguments)
    recovered.expect(0)
    assert project.lines("app.py") == [1, 2, 3, 4]
