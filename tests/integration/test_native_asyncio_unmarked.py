"""Unmarked tests do not consult asyncio adapter metadata."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.test_native_parallel import _native_parallel_available
from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    not _native_parallel_available(),
    reason="native execution requires Unix and a supported pytest version",
)


def test_unmarked_stash_callback_matches_pytest_asyncio(tmp_path: Path) -> None:
    pytest.importorskip("pytest_asyncio")
    source = """\
import json
import os
import sys
from pathlib import Path
from _pytest.stash import Stash

reads = []
original_get = Stash.get

def observe_get(self, key, default):
    caller = sys._getframe(1).f_code.co_name
    if caller in ('warn_asyncio_test', 'pytest_pyfunc_call'):
        reads.append('asyncio-stash')
    return original_get(self, key, default)

def pytest_collection_modifyitems(items):
    Stash.get = observe_get

def pytest_sessionfinish(session):
    Path('reads.json').write_text(json.dumps(reads))
"""
    traces = {}
    for route in ("upstream", "vfy", "native", "deferred"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope=function\n")
        project.write("conftest.py", source)
        project.write("test_case.py", "def test_plain():\n    assert True\n")
        result = (
            project.run_pytest("-q", "-p", "pytest_asyncio.plugin", load_xdist=False)
            if route == "upstream"
            else project.run_vfy(
                "test",
                "--engine=pytest" if route == "vfy" else "--engine=pytest-native",
                "--workers=2" if route == "deferred" else "--workers=1",
                "--no-cov",
                "-q",
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
        traces[route] = json.loads((project.root / "reads.json").read_text())
    assert traces == {route: [] for route in traces}
