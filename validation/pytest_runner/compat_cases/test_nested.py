# ruff: noqa: ANN001, ANN201
from __future__ import annotations

import json
import os
import subprocess
import sys


def test_nested_native_process(tmp_path):
    (tmp_path / "test_inner.py").write_text(
        "def test_inner():\n    assert 6 * 7 == 42\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTEST_NATIVE_RESULT"] = str(tmp_path / "native-result.json")
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        env=environment,
        check=False,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout
    record = json.loads((tmp_path / "native-result.json").read_text(encoding="utf-8"))
    assert record["engine"] == "pytest-runner-native-v1"
