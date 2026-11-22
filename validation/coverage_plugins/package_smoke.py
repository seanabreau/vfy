"""Run pinned Coverage.py plug-in packages against both coverage engines.

Usage: python validation/coverage_plugins/package_smoke.py OUTPUT_DIRECTORY
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("output_directory", type=Path)
root = parser.parse_args().output_directory.resolve()
if root.exists() and (not root.is_dir() or any(root.iterdir())):
    parser.error(f"output directory must be empty: {root}")
root.mkdir(parents=True, exist_ok=True)
probe = Path(__file__).with_name("package_probe.py").resolve()
fixtures = Path(__file__).with_name("fixtures")
cases = {
    "conditional-ini": "conditional",
    "conditional-toml": "conditional",
    "django": "django",
    "context": "context",
}
results = {}
for name, scenario in cases.items():
    work = root / name
    shutil.copytree(fixtures / name, work)
    case = {}
    for engine in ("coverage", "vfy"):
        command = [sys.executable, str(probe), engine, scenario, str(work)]
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("COVERAGE_", "VFY_", "PYTEST_"))
            and key not in ("PYTHONPATH", "PYTHONHOME")
        }
        completed = subprocess.run(
            command, cwd=work, env=env, text=True, capture_output=True, check=False
        )
        (work / f"{engine}.stdout").write_text(completed.stdout, encoding="utf-8")
        (work / f"{engine}.stderr").write_text(completed.stderr, encoding="utf-8")
        case[engine] = {"command": command, "exit": completed.returncode}
    if all(value["exit"] == 0 for value in case.values()):
        reference = json.loads((work / "coverage-canonical.json").read_text())
        actual = json.loads((work / "vfy-canonical.json").read_text())
        case["equal"] = reference == actual
    results[name] = case
sys.stdout.write(json.dumps(results, indent=2) + "\n")
(root / "run-results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
raise SystemExit(0 if all(case.get("equal") for case in results.values()) else 1)
