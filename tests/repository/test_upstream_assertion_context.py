"""The manual assertion checker must exercise both installed pytest routes."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_restoration_checks_stock_and_native_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker_dir = Path(__file__).resolve().parents[2] / "validation" / "pytest_runner"
    monkeypatch.syspath_prepend(str(checker_dir))
    checker = importlib.import_module("check_upstream_assertion_context")
    python = Path(sys.executable)
    expected = {"call_outcomes": ["failed", "passed"], "assertion_globals_restored": True}
    assert checker._check_failure_restoration(tmp_path, "stock", python, None) == expected
    result_path = tmp_path / "native-result.json"
    assert (
        checker._check_failure_restoration(tmp_path, "candidate", python, result_path) == expected
    )
    record = json.loads(result_path.read_text(encoding="utf-8"))
    assert record["engine"] == "pytest-runner-native-v1"
    assert [
        phase["outcome"]
        for case in record["cases"]
        for phase in case["phases"]
        if phase["phase"] == "call"
    ] == ["failed", "passed"]
