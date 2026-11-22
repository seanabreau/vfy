"""Disabled built-in coverage stays inert in forked workers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from validation.pytest_runner.check_native_xdist_adoption import junit_records

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="collected fork requires Linux")

_ENV = {
    "VFY_NATIVE_SCHEDULING": "cases",
    "VFY_WORKER_COLLECTION": "0",
}


def _write_project(project: VfyProject) -> None:
    project.write(
        "pytest.ini",
        "[pytest]\naddopts = --cov=app --cov-report=html --cov-append\n",
    )
    project.write("app.py", "def read_value():\n    return 1\n")
    project.write(
        "conftest.py",
        """
import json, os
from pathlib import Path
from vfy.pytest_plugin import VfyCovPlugin

def pytest_collection_finish(session):
    plugin = session.config.pluginmanager.getplugin('_cov')
    assert type(plugin) is VfyCovPlugin
    Path(f'adapter.{os.getpid()}.json').write_text(json.dumps({
        'disabled': plugin._disabled,
        'started': plugin._started,
        'has_coverage': plugin.coverage is not None,
    }))
""",
    )
    project.write(
        "tests/test_cases.py",
        """
import os
from pathlib import Path
import pytest
import app

@pytest.mark.parametrize('value', [0, 1])
def test_body(value, request):
    Path(f'body.{value}').write_text(str(os.getpid()))
    plugin = request.config.pluginmanager.getplugin('_cov')
    assert plugin._disabled is True
    assert plugin.coverage is None
    assert app.read_value() == 1
    print(f'body {value}')
""",
    )


def test_collected_fork_allows_inert_builtin_coverage_under_no_cov(tmp_path: Path) -> None:
    records = {}
    for mode in ("exec", "fork"):
        project = VfyProject(tmp_path / mode)
        _write_project(project)
        completed = project.run_vfy(
            "test",
            "--engine=pytest-native",
            "--workers=2",
            "--history=history.json",
            "--no-replay",
            "--no-cov",
            "--capture=sys",
            "--junitxml=results.xml",
            "-o",
            "junit_logging=all",
            "-o",
            "junit_log_passing_tests=true",
            "-q",
            env={**_ENV, "VFY_WORKER_START": mode},
        )
        completed.expect(0)
        pids = {(project.root / f"body.{index}").read_text() for index in (0, 1)}
        assert len(pids) == 2
        adapters = list(project.root.glob("adapter.*.json"))
        assert len(adapters) == (1 if mode == "fork" else 3)
        assert all(
            json.loads(path.read_text())
            == {"disabled": True, "started": False, "has_coverage": False}
            for path in adapters
        )
        assert not (project.root / ".coverage").exists()
        assert not (project.root / "htmlcov").exists()
        assert (project.root / "history.json").is_file()
        records[mode] = junit_records(
            project.root / "results.xml", project.root, project.root / "unused-tmp"
        )
        assert len(records[mode]) == 2
    assert records["fork"] == records["exec"]
