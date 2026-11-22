"""Dependency pruning preserves packages with inactive pytest requirements."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PRUNE = Path(__file__).parents[2] / "validation/pytest_runner/real_packages/prune.py"


@pytest.mark.skipif(os.name == "nt", reason="corpus environments use POSIX file URLs")
def test_pruning_evaluates_markers_without_importing_pytest_or_the_project(tmp_path: Path) -> None:
    checkout = tmp_path / "corpus_project"
    checkout.mkdir()
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    requirements = {
        "pytest": [],
        "pluggy": [],
        "iniconfig": ["pytest"],
        "active-plugin": ['pytest; python_version >= "3"'],
        "inactive-plugin": ['pytest; python_version < "3"'],
        "extra-plugin": ['pytest; extra == "test"'],
        "compound-plugin": ['pytest; extra == "test" or python_version >= "3"'],
        "negative-extra-plugin": ['pytest; extra != "test"'],
        "transitive-plugin": ["active-plugin"],
        "optional-consumer": ['pluggy; python_version < "3"'],
    }
    for name, required in requirements.items():
        directory = metadata / f"{name.replace('-', '_')}-1.0.dist-info"
        directory.mkdir()
        directory.joinpath("METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
            + "".join(f"Requires-Dist: {line}\n" for line in required),
            encoding="utf-8",
        )
        if name == "iniconfig":
            directory.joinpath("direct_url.json").write_text(
                json.dumps({"url": checkout.as_uri()}), encoding="utf-8"
            )
    # Restrict distribution discovery to real fixture metadata while retaining
    # the interpreter's installed packaging parser and standard library.
    code = """
import importlib.metadata
import runpy
import sys

class NoProjectImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'pytest', '_pytest', 'iniconfig', 'corpus_project'}:
            raise AssertionError(f'pruning imported {fullname}')

sys.meta_path.insert(0, NoProjectImports())
found = list(importlib.metadata.distributions(path=[sys.argv[2]]))
importlib.metadata.distributions = lambda: found
script, _, checkout = sys.argv[1:]
sys.argv = [script, checkout]
runpy.run_path(script, run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(PRUNE), str(metadata), str(checkout)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [
        "active-plugin",
        "compound-plugin",
        "negative-extra-plugin",
        "pluggy",
        "pytest",
        "transitive-plugin",
    ]
