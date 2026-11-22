"""A rejected plug-in is not left registered on the independent engine."""

from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_failed_hook_validation_leaves_the_plugin_unregistered(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_registration.py",
        "import pytest\n\n"
        "class Plugin:\n"
        "    def pytest_collection_modifyitems(self, bogus):\n"
        "        pass\n\n"
        "def test_registration(pytestconfig):\n"
        "    manager = pytestconfig.pluginmanager\n"
        "    plugin = Plugin()\n"
        "    for _ in range(2):\n"
        "        with pytest.raises(Exception, match='Plugin .myplug. for hook') as error:\n"
        "            manager.register(plugin, 'myplug')\n"
        "        assert type(error.value).__name__ == 'PluginValidationError'\n"
        "    assert not manager.is_registered(plugin)\n"
        "    assert manager.get_plugin('myplug') is None\n",
    )

    result = project.run_engine("native", "test_registration.py", "-q")

    result.expect(0)
