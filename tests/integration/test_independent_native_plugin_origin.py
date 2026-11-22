"""Native engine plugins retain their implementation and callback witnesses."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_plugin_without_implementation_origin_still_fails_strict_activation(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """from pathlib import Path

class UnownedPlugin:
    __module__ = 'builtins'
    def pytest_runtest_call(self, item):
        with Path('actual-callbacks.txt').open('a') as output:
            output.write(item.nodeid + '\\n')

def pytest_configure(config):
    config.pluginmanager.register(UnownedPlugin(), 'unowned-implementation')
""",
    )
    project.write("test_case.py", "def test_case():\n    pass\n")
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(0)
    callbacks = tmp_path / "actual-callbacks.txt"
    expected = callbacks.read_text()
    callbacks.unlink()
    candidate = project.run_vfy(
        "test", "--engine=native", "--no-cov", "--native-result-json=native.json"
    )
    assert candidate.returncode == oracle.returncode, candidate.stdout + candidate.stderr
    assert callbacks.read_text() == expected == "test_case.py::test_case\n"
    raw = json.loads((tmp_path / "native.json").read_text())
    record = next(row for row in raw["plugins"] if row["name"] == "unowned-implementation")
    assert record["module"] == "builtins"
    assert record["origin"] is None
    assert record["sha256"] is None
    assert record["hook_calls"] == {"pytest_runtest_call": 1}
