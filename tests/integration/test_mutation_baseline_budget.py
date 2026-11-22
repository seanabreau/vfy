"""A healthy full baseline has its own budget before short mutation watchdogs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject


@pytest.mark.skipif(sys.platform != "linux", reason="independent mutations require Linux")
def test_slow_clean_baseline_keeps_a_short_fixed_mutant_watchdog(tmp_path: Path) -> None:
    project = VfyProject(tmp_path, timeout=90)
    source = "def value():\n    return 1\n"
    project.write("app.py", source)
    project.write(
        "test_app.py",
        "import time\nfrom pathlib import Path\nfrom app import value\n\n"
        "def test_value():\n"
        "    observed = value()\n"
        "    if observed == 1:\n"
        "        time.sleep(31)\n"
        "        Path('baseline-finished').touch()\n"
        "    else:\n"
        "        Path('mutant-started').touch()\n"
        "        time.sleep(2)\n"
        "        Path('mutant-finished').touch()\n"
        "    assert observed == 1\n",
    )
    project.write("app.py", source.replace("return 1", "return 2"))
    stock = project.run_python("-B", "-m", "pytest", "-q")
    stock.expect(1)
    assert "1 failed" in stock.stdout
    (tmp_path / "mutant-started").unlink()
    (tmp_path / "mutant-finished").unlink()
    project.write("app.py", source)

    completed = project.run_vfy(
        "mutate",
        "--engine=native",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--timeout=1",
        "--timeout-policy=fixed",
        "--no-cache",
        "--format=json",
    )
    completed.expect(1)
    report = json.loads(completed.stdout)
    assert (tmp_path / "baseline-finished").exists(), report
    assert (tmp_path / "mutant-started").exists(), report
    assert not (tmp_path / "mutant-finished").exists(), report
    assert report["summary"] == {"timeout": 1}, report
    assert report["mutations"][0]["killer"] is None, report
    assert report["mutations"][0]["returncode"] is None, report
