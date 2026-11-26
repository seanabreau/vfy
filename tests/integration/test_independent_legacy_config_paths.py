"""``Config.rootdir`` and ``Config.inifile`` match pytest's legacy path properties."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PROBE = """\
import json
import os
from pathlib import Path


def describe(path):
    if path is None:
        return None
    return {
        "str": str(path),
        "fspath": os.fspath(path),
        "strpath": path.strpath,
        "basename": path.basename,
        "join": path.join("child").strpath,
    }


def pytest_configure(config):
    observed = {
        "rootdir": describe(config.rootdir),
        "rootdir_equals_rootpath": config.rootdir == config.rootpath,
        "inifile": describe(config.inifile),
        "inifile_equals_inipath": config.inifile == config.inipath,
    }
    Path("observed.json").write_text(json.dumps(observed))
"""


@pytest.mark.parametrize("config_file", ["pytest.ini", None], ids=["inifile", "no-inifile"])
def test_legacy_config_paths_match_stock(tmp_path: Path, config_file: str | None) -> None:
    observed = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine / "project")
        if config_file is not None:
            project.write(config_file, "[pytest]\n")
        project.write("conftest.py", _PROBE)
        project.write("test_body.py", "def test_body():\n    pass\n")
        result = project.run_engine(engine, "-q", env={"PYTEST_ADDOPTS": None})
        result.expect(0)
        text = (project.root / "observed.json").read_text()
        observed.append(json.loads(text.replace(str(project.root), "<root>")))
    stock, native = observed
    assert stock["rootdir"]["str"] == "<root>"
    assert (stock["inifile"] is None) == (config_file is None)
    assert native == stock
