"""An admitted Hypothesis summary must not load the optional observability module."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_hypothesis_summary_does_not_import_observability_for_plain_tests(
    tmp_path: Path,
) -> None:
    for route, workers in (("stock", 1), ("independent", 1), ("independent", 2)):
        label = f"{route}-{workers}"
        project = VfyProject(tmp_path / label)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """import json, os, sys
from pathlib import Path
import pytest

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_terminal_summary(terminalreporter):
    manager = terminalreporter.config.pluginmanager
    plugin = manager.getplugin('hypothesispytest')
    implementations = manager.hook.pytest_terminal_summary.get_hookimpls()
    def snapshot():
        return {
            'root_loaded': 'hypothesis' in sys.modules,
            'observability_loaded': 'hypothesis.internal.observability' in sys.modules,
        }
    before = snapshot()
    result = yield
    Path('summary-' + str(os.getpid()) + '.json').write_text(json.dumps({
        'pid': os.getpid(),
        'worker': os.environ.get('VFY_NATIVE_PARALLEL_INDEX'),
        'plugin': plugin is not None,
        'summary_hook_registered': any(h.plugin is plugin for h in implementations),
        'before': before,
        'after': snapshot(),
    }))
    return result
""",
        )
        for name in ("a", "b"):
            project.write(
                f"test_{name}.py",
                f"import os\nfrom pathlib import Path\ndef test_{name}():\n"
                f"    Path('{name}.pid').write_text(str(os.getpid()))\n",
            )
        args = ("-p", "hypothesispytest", "-q")
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        if route == "stock":
            result = project.run_pytest(*args, load_xdist=False, env=env)
        else:
            result = project.run_vfy(
                "test",
                "--engine=native",
                "--no-cov",
                f"--workers={workers}",
                "--native-result-json=actual.json",
                *args,
                env=env,
            )
        result.expect(0)
        assert "2 passed" in result.stdout
        observations = [
            json.loads(path.read_text()) for path in project.root.glob("summary-*.json")
        ]
        assert observations, result.stdout + result.stderr
        assert all(row["plugin"] and row["summary_hook_registered"] for row in observations)
        if route == "independent":
            assert all(not row["before"]["observability_loaded"] for row in observations)
            assert all(not row["after"]["observability_loaded"] for row in observations)
            topology = json.loads((project.root / "actual.json").read_text())["execution_topology"]
            assert topology["requested_workers"] == topology["active_workers"] == workers
            assert len(set(topology["worker_pids"])) == workers
            assert {int((project.root / f"{name}.pid").read_text()) for name in ("a", "b")} == set(
                topology["worker_pids"]
            )
