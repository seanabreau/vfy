"""Plugin instances expose inherited fixture methods to collected tests."""

from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_plugin_instance_inherited_fixture_binding(tmp_path: Path) -> None:
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import pytest

class Base:
    @pytest.fixture
    def inherited(self):
        return self.value

class Provider(Base):
    def __init__(self):
        self.value = 'bound plugin'
    @pytest.fixture
    def local(self):
        return self.value + ' local'

class Slotted:
    __slots__ = ()
    @pytest.fixture
    def slotted(self):
        return 'slotted'

def pytest_configure(config):
    config.pluginmanager.register(Provider(), 'fixture-provider')
    config.pluginmanager.register(Slotted(), 'slotted-provider')
""",
        )
        project.write(
            "test_case.py",
            "def test_case(inherited, local, slotted):\n"
            "    assert (inherited, local, slotted) == "
            "('bound plugin', 'bound plugin local', 'slotted')\n",
        )
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = (
            project.run_pytest("-q", load_xdist=False, env=env)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=env)
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout


def test_plugin_fixture_alias_order_and_descriptor_errors(tmp_path: Path) -> None:
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import pytest

class Broken:
    def __get__(self, instance, owner):
        raise RuntimeError('descriptor must be ignored')

class Base:
    @pytest.fixture(name='chosen')
    def a(self):
        return 'inherited'

class Provider(Base):
    bad = Broken()
    @pytest.fixture(name='chosen')
    def z(self):
        return 'class'

def pytest_configure(config):
    provider = Provider()
    provider.z = pytest.fixture(name='different')(lambda: 'instance')
    config.pluginmanager.register(provider, 'fixture-provider')
""",
        )
        project.write("test_case.py", "def test_case(chosen): assert chosen == 'instance'\n")
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = (
            project.run_pytest("-q", load_xdist=False, env=env)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=env)
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
