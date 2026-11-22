"""Live fixture callables are classified without repeated inspect dispatch."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_direct_parameter_preserves_generator_without_inspect_dispatch(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
import inspect
import json
from pathlib import Path

counts = []
originals = {}

def pytest_fixture_setup(fixturedef, request):
    if fixturedef.argname != 'value':
        return
    target = fixturedef.func
    record = {'builtin': inspect.isbuiltin(target), 'calls': []}
    counts.append(record)
    for name in ('iscoroutinefunction', 'isasyncgenfunction', 'isgeneratorfunction'):
        original = getattr(inspect, name)
        originals[name] = original
        def observe(obj, _original=original, _name=name):
            if obj is target:
                record['calls'].append(_name)
            return _original(obj)
        setattr(inspect, name, observe)

def pytest_fixture_post_finalizer(fixturedef):
    if fixturedef.argname == 'value':
        for name, original in originals.items():
            setattr(inspect, name, original)

def pytest_sessionfinish():
    Path('classification.json').write_text(json.dumps(counts))
""",
        )
        project.write(
            "test_generator.py",
            """\
import pytest

@pytest.mark.parametrize('value', [(number for number in (1, 2))])
def test_generator_value(value):
    assert next(value) == 1
    assert next(value) == 2
""",
        )
        result = (
            project.run_pytest("-q", load_xdist=False, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=1",
                "--no-replay",
                "--no-cov",
                "-q",
                env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
        observations[engine] = json.loads((project.root / "classification.json").read_text())
    assert len(observations["pytest"]) == 1
    assert observations["native"] == [{"builtin": True, "calls": []}]


@requires_native_pytest
def test_live_callable_fixture_is_classified_once_without_async_plugin(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
import inspect
import json
from pathlib import Path

counts = []
originals = {}

class Provider:
    def __call__(self, request):
        return 23

def pytest_fixture_setup(fixturedef, request):
    if fixturedef.argname != 'value':
        return
    target = Provider()
    fixturedef.func = target
    for name in ('iscoroutinefunction', 'isasyncgenfunction', 'isgeneratorfunction'):
        original = getattr(inspect, name)
        originals[name] = original
        def observe(obj, _original=original, _name=name):
            if obj is target:
                counts.append(_name)
            return _original(obj)
        setattr(inspect, name, observe)

def pytest_fixture_post_finalizer(fixturedef):
    if fixturedef.argname == 'value':
        for name, original in originals.items():
            setattr(inspect, name, original)

def pytest_sessionfinish():
    Path('classification.json').write_text(json.dumps(counts))
""",
        )
        project.write(
            "test_callable.py",
            "import pytest\n@pytest.mark.parametrize('value', [1])\n"
            "def test_value(value): assert value == 23\n",
        )
        result = (
            project.run_pytest(
                "-p",
                "no:asyncio",
                "-q",
                load_xdist=False,
                env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            )
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=1",
                "--no-replay",
                "--no-cov",
                "-p",
                "no:asyncio",
                "-q",
                env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
        observations[engine] = json.loads((project.root / "classification.json").read_text())
    assert observations["native"] == [
        "iscoroutinefunction",
        "isasyncgenfunction",
        "isgeneratorfunction",
    ]


@requires_native_pytest
def test_builtin_fixture_returns_generator_without_driving_it(tmp_path: Path) -> None:
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
def pytest_fixture_setup(fixturedef, request):
    if fixturedef.argname == 'value':
        fixturedef.func = [(number for number in (1, 2))].pop
""",
        )
        project.write(
            "test_builtin.py",
            """\
import pytest

@pytest.fixture
def value():
    raise AssertionError('replacement was not used')

def test_generator_value(value):
    assert next(value) == 1
    assert next(value) == 2
""",
        )
        result = (
            project.run_pytest("-q", load_xdist=False, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=1",
                "--no-replay",
                "--no-cov",
                "-q",
                env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout


@requires_native_pytest
def test_bound_test_method_uses_independent_classification(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
import inspect
import json
from pathlib import Path

counts = []
originals = {}

def pytest_runtest_setup(item):
    target = item.obj.__func__
    for name in ('iscoroutinefunction', 'isasyncgenfunction', 'isgeneratorfunction'):
        original = getattr(inspect, name)
        originals[name] = original
        def observe(obj, _original=original, _name=name):
            if getattr(obj, '__func__', None) is target:
                counts.append(_name)
            return _original(obj)
        setattr(inspect, name, observe)

def pytest_runtest_teardown(item):
    for name, original in originals.items():
        setattr(inspect, name, original)

def pytest_sessionfinish():
    Path('classification.json').write_text(json.dumps(counts))
""",
        )
        project.write(
            "test_bound.py",
            "class TestBound:\n    def test_method(self): assert 2 + 2 == 4\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=1",
                "--no-replay",
                "--no-cov",
                "-q",
                env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
        observations[engine] = json.loads((project.root / "classification.json").read_text())
    assert observations["native"] == []
