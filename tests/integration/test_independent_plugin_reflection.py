"""Plugin registration preserves pytest's descriptor access boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("stage", ["configure", "sessionstart"])
def test_registration_filters_nonhook_descriptors_before_access(
    tmp_path: Path, engine: str, stage: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        f"""import json
from pathlib import Path
import pytest

events = []
class Probe:
    @property
    def _is_last_item(self):
        raise AssertionError('private descriptor accessed before session')
    @property
    def pytest_plugins(self):
        raise AssertionError('plugin declarations are not hook methods')
    @pytest.hookimpl(specname='pytest_runtest_setup')
    def ordinary_method(self, item):
        events.append('unprefixed decorated method')
    def pytest_runtest_setup(self, item):
        events.append(item.nodeid)

probe = Probe()
def register(config):
    assert config.pluginmanager.register(probe, 'probe') == 'probe'
    assert [hook.name for hook in config.pluginmanager.get_hookcallers(probe)] == [
        'pytest_runtest_setup'
    ]
def pytest_configure(config):
    if {stage!r} == 'configure': register(config)
def pytest_sessionstart(session):
    if {stage!r} == 'sessionstart': register(session.config)
def pytest_unconfigure(config):
    Path('events.json').write_text(json.dumps(events))
""",
    )
    project.write("test_one.py", "def test_one(): pass\n")
    result = (
        project.run_python("-m", "pytest", "-q")
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / "events.json").read_text()) == ["test_one.py::test_one"]
