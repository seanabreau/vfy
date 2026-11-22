"""Conftest hook scopes follow the live registration and item path."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_conftest_scope_follows_path_and_registration_mutations(tmp_path: Path) -> None:
    observations = []
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import json
from pathlib import Path
import pytest

class Spec:
    @pytest.hookspec
    def pytest_scope_probe(): pass

def pytest_addhooks(pluginmanager): pluginmanager.add_hookspecs(Spec)
def pytest_scope_probe(): return 'root'

def pytest_collection_modifyitems(session, config, items):
    manager = config.pluginmanager
    left, right = items
    probe = lambda item: sorted(item.ihook.pytest_scope_probe())
    before = [probe(left), probe(right)]
    extra = object()
    manager.register(extra, 'extra')
    def order():
        labels = []
        for name, obj in manager.list_name_plugin():
            if obj is extra:
                labels.append('extra')
            elif getattr(obj, '__file__', None) in [
                str(config.rootpath / 'conftest.py'),
                str(config.rootpath / 'left' / 'conftest.py'),
                str(config.rootpath / 'right' / 'conftest.py'),
            ]:
                is_root = obj.__file__ == str(config.rootpath / 'conftest.py')
                labels.append('root' if is_root else Path(obj.__file__).parent.name)
        return labels
    initial_order = order()
    path = left.path
    left.path = right.path
    moved = probe(left)
    left.path = path
    plugin = next(p for p in manager.get_plugins()
                  if getattr(p, '__file__', None) == str(config.rootpath / 'right' / 'conftest.py'))
    name = manager.get_name(plugin)
    assert manager.unregister(plugin) is plugin
    removed = [probe(left), probe(right)]
    manager.register(plugin, name)
    restored = [probe(left), probe(right)]
    result = [before, moved, removed, restored, initial_order, order()]
    Path('scope.json').write_text(json.dumps(result))
""",
        )
        for side in ("left", "right"):
            project.write(f"{side}/conftest.py", f"def pytest_scope_probe(): return {side!r}\n")
            project.write(f"{side}/test_{side}.py", f"def test_{side}(): pass\n")
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = (
            project.run_pytest("-q", load_xdist=False, env=env)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=env)
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observations.append(json.loads((project.root / "scope.json").read_text()))
    assert observations[0] == [
        [["left", "root"], ["right", "root"]],
        ["right", "root"],
        [["left", "root"], ["root"]],
        [["left", "root"], ["right", "root"]],
        ["root", "left", "right", "extra"],
        ["root", "left", "extra", "right"],
    ]
    assert observations[1] == observations[0]
