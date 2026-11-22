"""Controller defaults register after upstream autoload without moving user plugins."""

from __future__ import annotations

import json
from importlib.metadata import distributions
from pathlib import Path
from typing import Any

import pytest

from tests.support import VfyProject
from tests.support.history import read_history
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_REQUIRED = {"hypothesispytest", "pytest_cov", "xdist", "xdist.looponfail"}

_CONFTEST = """import hashlib, json, os, sys
from pathlib import Path
import pytest

roles = {'pytest_cov.plugin': 'coverage',
         'vfy.independent.coverage_support': 'coverage',
         '_hypothesis_pytestplugin': 'hypothesis',
         'vfy.independent.hypothesis_support': 'hypothesis',
         'xdist.plugin': 'xdist'}
configure_calls = []
previous = sys.getprofile()
def profile(frame, event, argument):
    if event == 'call' and frame.f_code.co_name == 'pytest_configure':
        role = roles.get(frame.f_globals.get('__name__'))
        if role is not None:
            configure_calls.append(role)
    if previous is not None:
        previous(frame, event, argument)
sys.setprofile(profile)

@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    # Registered after installed plugins, this actual trylast hook observes
    # their completed configuration, including xdist's real trylast hook.
    sys.setprofile(previous)
    manager = config.pluginmanager
    plugins = {}
    for role, names in [('coverage', ['pytest_cov', 'vfy.independent.coverage_support']),
                        ('hypothesis', ['hypothesispytest']),
                        ('xdist', ['xdist', 'xdist.plugin']),
                        ('looponfail', ['xdist.looponfail'])]:
        selected = next((manager.getplugin(name) for name in names
                         if manager.getplugin(name) is not None), None)
        if selected is None:
            continue
        module_name = getattr(selected, '__name__', type(selected).__module__)
        module = sys.modules[module_name]
        origin = module.__file__
        plugins[role] = {'module': module_name, 'origin': origin,
                         'sha256': hashlib.sha256(Path(origin).read_bytes()).hexdigest()}
    Path('configure-' + str(os.getpid()) + '.json').write_text(json.dumps({
        'markers': config.getini('markers'),
        'calls': configure_calls,
        'plugins': plugins,
        'pid': os.getpid(),
    }))
"""

_ENGINE = """import hashlib, json, os
from pathlib import Path
import hypothesis
from hypothesis import given, settings, strategies as st

@settings(max_examples=3, derandomize=True, deadline=None)
@given(st.integers(min_value=1, max_value=3))
def test_engine(value):
    assert value + 1 > value
    Path('engine.json').write_text(json.dumps({
        'module': hypothesis.__name__, 'origin': hypothesis.__file__,
        'sha256': hashlib.sha256(Path(hypothesis.__file__).read_bytes()).hexdigest(),
        'pid': os.getpid(),
    }))
"""

_SERVICES = """import os
from uuid import UUID
def test_services(worker_id, testrun_uid, cov):
    # Like pytest-xdist, workers report gwN; serial runs report master.
    assert worker_id == os.environ.get('PYTEST_XDIST_WORKER', 'master')
    assert UUID(testrun_uid).hex == testrun_uid
    assert cov is None
"""


def _other_plugins() -> list[str]:
    names = {
        entry.name
        for distribution in distributions()
        for entry in distribution.entry_points
        if entry.group == "pytest11" and entry.name not in _REQUIRED
    }
    return [argument for name in sorted(names) for argument in ("-p", f"no:{name}")]


def _project(root: Path, *, services: bool = True) -> VfyProject:
    project = VfyProject(root)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", _CONFTEST)
    if services:
        project.write("test_engine.py", _ENGINE)
        project.write("test_services.py", _SERVICES)
    else:
        project.write("test_plain.py", "def test_plain():\n    assert 1 + 1 == 2\n")
    return project


def _snapshots(project: VfyProject) -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in sorted(project.root.glob("configure-*.json"))]


def _assert_snapshot(
    candidate: dict[str, Any], stock: dict[str, Any], *, services: bool = True
) -> None:
    assert candidate["markers"] == stock["markers"]
    assert candidate["calls"] == stock["calls"]
    assert set(candidate["plugins"]) == set(stock["plugins"])
    assert candidate["calls"].count("coverage") == 1
    assert candidate["plugins"]["coverage"]["module"] == "vfy.independent.coverage_support"
    assert stock["plugins"]["coverage"]["module"] == "pytest_cov.plugin"
    if services:
        assert candidate["calls"].count("hypothesis") == 1
        assert candidate["calls"].count("xdist") == 1
        for role, module in [("xdist", "xdist.plugin"), ("looponfail", "xdist.looponfail")]:
            assert (
                candidate["plugins"][role]["module"] == stock["plugins"][role]["module"] == module
            )


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("explicit_coverage", [False, True])
def test_controller_coverage_preserves_real_autoload_and_explicit_plugin_order(
    tmp_path: Path, workers: int, explicit_coverage: bool
) -> None:
    stock = _project(tmp_path / "stock")
    candidate = _project(tmp_path / "candidate")
    arguments = [*_other_plugins(), *(["-p", "pytest_cov"] if explicit_coverage else []), "-q"]
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}
    oracle = stock.run_pytest(*arguments, load_xdist=False, env=environment)
    oracle.expect(0)
    result = candidate.run_vfy(
        "test",
        "--engine=native",
        "--workers",
        str(workers),
        "--no-cov",
        *arguments,
        env=environment,
    )
    result.expect(0)
    baseline = _snapshots(stock)
    snapshots = _snapshots(candidate)
    assert len(baseline) == 1
    assert len(snapshots) == (1 if workers == 1 else 3)
    for observed in snapshots:
        _assert_snapshot(observed, baseline[0])
    assert set(baseline[0]["calls"][:2]) == {"hypothesis", "coverage"}
    if explicit_coverage:
        assert baseline[0]["calls"][:2] == ["hypothesis", "coverage"]
    engine = json.loads((candidate.root / "engine.json").read_text())
    assert engine["module"] == "hypothesis"
    assert "hypothesis" in Path(engine["origin"]).parts
    assert len(engine["sha256"]) == 64


@pytest.mark.parametrize("services", [False, True])
def test_controller_default_coverage_is_available_when_autoload_is_disabled(
    tmp_path: Path, services: bool
) -> None:
    stock = _project(tmp_path / "stock", services=services)
    candidate = _project(tmp_path / "candidate", services=services)
    # Coverage ordering compares the same unrelated plugin activation on both routes.
    requested = [
        "-p",
        "no:asyncio",
        *(["-p", "hypothesispytest", "-p", "xdist", "-p", "xdist.looponfail"] if services else []),
    ]
    environment: dict[str, str | None] = {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "VFY_TEST_RESULTS": None,
    }
    oracle = stock.run_pytest(
        *requested, "-p", "pytest_cov", "-q", load_xdist=False, env=environment
    )
    oracle.expect(0)
    result = candidate.run_vfy(
        "test", "--engine=native", "--no-cov", *requested, "-q", env=environment
    )
    result.expect(0)
    baseline = _snapshots(stock)
    observed = _snapshots(candidate)
    assert len(baseline) == len(observed) == 1
    _assert_snapshot(observed[0], baseline[0], services=services)
    if not services:
        assert set(observed[0]["plugins"]) == {"coverage"}


def test_explicit_coverage_block_survives_controller_default_service_activation(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, services=False)
    result = project.run_python(
        "-m",
        "vfy.independent.entry",
        "-p",
        "vfy.independent.results",
        "-p",
        "no:pytest_cov",
        "-q",
        env={
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "VFY_TEST_RESULTS": str(project.root / "result.json"),
        },
    )
    result.expect(0)
    observed = _snapshots(project)
    assert len(observed) == 1
    assert "coverage" not in observed[0]["plugins"]
    assert observed[0]["calls"] == []


@pytest.mark.parametrize(
    "owned_name", ["vfy.independent.results", "vfy.independent.coverage_support"]
)
def test_owned_controller_plugins_cannot_be_replaced_by_entry_points(
    tmp_path: Path, owned_name: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "user_plugin-1.0.dist-info/METADATA",
        "Metadata-Version: 2.1\nName: user-plugin\nVersion: 1.0\n",
    )
    project.write(
        "user_plugin-1.0.dist-info/entry_points.txt",
        f"[pytest11]\n{owned_name} = collision\nuser_plugin = real_user_plugin\n",
    )
    project.write("collision.py", "raise AssertionError('controller plugin was replaced')\n")
    project.write(
        "real_user_plugin.py", "from pathlib import Path\nPath('user-plugin-loaded').touch()\n"
    )
    project.write("subject.py", "def value():\n    return 42\n")
    project.write(
        "test_subject.py",
        "from subject import value\n"
        "def test_value(request):\n"
        "    manager = request.config.pluginmanager\n"
        "    for name in ('vfy.independent.results', 'vfy.independent.coverage_support'):\n"
        "        assert manager.getplugin(name).__name__ == name\n"
        "    assert value() == 42\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--workers=1",
        "-q",
        "--cov=subject",
        "-p",
        "user_plugin",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(0)
    assert (tmp_path / "user-plugin-loaded").is_file()
    assert project.lines("subject.py") == [1, 2]
    history = read_history(tmp_path / ".vfy/test-history.sqlite3")
    assert (
        history["tests"]["test_subject.py::test_value"]["observations"][-1]["outcome"] == "passed"
    )
