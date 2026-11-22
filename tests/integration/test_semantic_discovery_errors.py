"""Semantic reports must not silently omit discovered source files."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vfy import Coverage


def test_malformed_discovered_source_fails_report(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    bad = tmp_path / "bad.py"
    good.write_text("if flag:\n    pass\n", encoding="utf-8")
    bad.write_text("if flag:\n    pass\nif broken(:\n    pass\n", encoding="utf-8")
    coverage = Coverage(data_file=None, decisions=True, config_file=False)

    with pytest.raises(ValueError, match=r"failed to parse semantic source.*bad\.py"):
        coverage.decision_report(source=[str(good), str(bad)])


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions required")
def test_inaccessible_discovered_directory_fails_report(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text("if flag:\n    pass\n", encoding="utf-8")
    root = tmp_path / "source"
    hidden = root / "hidden"
    hidden.mkdir(parents=True)
    (hidden / "hidden.py").write_text("if flag:\n    pass\n", encoding="utf-8")
    hidden.chmod(0)
    try:
        if os.access(hidden, os.R_OK):
            pytest.skip("process can read the restricted directory")
        coverage = Coverage(data_file=None, decisions=True, config_file=False)
        with pytest.raises(OSError, match=r"read semantic source directory.*hidden"):
            coverage.decision_report(source=[str(good), str(root)])
    finally:
        hidden.chmod(0o700)
