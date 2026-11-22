"""Unrelated missing descriptor metadata must select canonical fixture reads."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.native import run_parameter_suite

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or pytest.__version__ != "9.1.1",
    reason="native dense reader requires Unix and pinned pytest",
)

_SOURCE = """\
import gc
import json
import os
import sys
from pathlib import Path
import pytest
from _pytest.python import CallSpec2

restore_metadata = None
armed = False

def arm_metadata(session):
    global restore_metadata, armed
    assert len(session.items) == 1
    assert sys.gettrace() is None and sys.getprofile() is None
    original = CallSpec2.__getattribute__
    def getter(spec, name):
        if name == 'indices':
            raise AttributeError('indices metadata unavailable')
        return original(spec, name)
    CallSpec2.__getattribute__ = getter
    def restore():
        CallSpec2.__getattribute__ = original
    restore_metadata = restore
    armed = True

def restore_before_setup(item):
    global armed
    assert armed and restore_metadata is not None
    restore_metadata()
    armed = False

@pytest.fixture
def value(request):
    assert not armed
    return request.param

@pytest.fixture
def ordinary():
    assert not armed
    return 'ordinary'

@pytest.mark.parametrize('value', [17], indirect=True)
def test_unrelated_metadata(value, ordinary):
    assert gc.isenabled() and not armed
    result = [ordinary, value]
    assert result == ['ordinary', 17]
    Path('observed.json').write_text(json.dumps(result))
"""


def test_dense_reader_falls_back_when_unrelated_descriptor_name_is_missing(tmp_path: Path) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_metadata.py", _SOURCE)
        project.write(
            "conftest.py",
            "import gc\n"
            "def pytest_collection():\n    gc.disable()\n"
            "def pytest_collection_finish(session):\n"
            "    import test_metadata\n"
            "    test_metadata.arm_metadata(session)\n"
            "    gc.enable()\n"
            "def pytest_runtest_setup(item):\n"
            "    import test_metadata\n"
            "    test_metadata.restore_before_setup(item)\n",
        )
        environment = {
            "NAME_ROUTE": route,
        }
        completed = run_parameter_suite(project, route, env=environment)
        output = completed.stdout + completed.stderr
        assert completed.returncode == 0, output
        assert "1 passed" in output, output
        observations[route] = json.loads((project.root / "observed.json").read_text())
    assert observations["native"] == observations["pytest"] == ["ordinary", 17]
