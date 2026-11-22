"""Current-test updates follow the registered runner's live Python callbacks."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


_LIVE_HELPER = r"""import builtins
import json
import os
import sys
import types
from pathlib import Path
import pytest
import _pytest.runner as runner

MODE = MODE_VALUE
patched = False

def record(kind, phase, nodeid):
    value = [kind, phase, nodeid, os.environ.get('PYTEST_CURRENT_TEST')]
    with Path('events.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value) + '\n')

@pytest.fixture(autouse=True)
def observe(request):
    global patched
    if not patched:
        original = runner.__dict__['_update_current_test_var']
        runner.__dict__['_current_test_original'] = types.FunctionType(
            original.__code__, original.__globals__, original.__name__, original.__defaults__
        )
        runner.__dict__['_current_test_record'] = record
        def changed(item, when):
            _current_test_original(item, when)
            if when != 'setup':
                _current_test_record('helper', when, item.nodeid)
        if MODE == 'code':
            original.__code__ = changed.__code__
        else:
            runner.__dict__['_update_current_test_var'] = types.FunctionType(
                changed.__code__, runner.__dict__
            )
        if MODE == 'builtin':
            builtins._update_current_test_var = runner.__dict__.pop('_update_current_test_var')
        if MODE == 'alias':
            decoy = types.ModuleType('_pytest.runner')
            decoy.__dict__.update(runner.__dict__)
            def wrong_helper(item, when):
                raise AssertionError('sys.modules decoy helper was called')
            decoy._update_current_test_var = wrong_helper
            sys.modules['_pytest.runner'] = decoy
        if MODE == 'class':
            class RunnerModule(types.ModuleType):
                def __getattribute__(self, name):
                    if name == '_update_current_test_var':
                        raise AssertionError('module attribute callback was called')
                    return super().__getattribute__(name)
            runner.__class__ = RunnerModule
        patched = True
    record('fixture', 'setup', request.node.nodeid)
    yield
    record('fixture', 'teardown', request.node.nodeid)
"""


@pytest.mark.parametrize("mode", ["helper", "code", "alias", "class", "builtin"])
@pytest.mark.parametrize(
    "workers",
    [
        1,
        pytest.param(
            2,
            marks=pytest.mark.skipif(sys.platform != "linux", reason="fork workers require Linux"),
        ),
    ],
)
def test_current_test_updates_retain_live_registered_runner_callbacks(
    tmp_path: Path, mode: str, workers: int
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", _LIVE_HELPER.replace("MODE_VALUE", repr(mode)))
        for name in ("a", "b"):
            project.write(
                f"test_{name}.py",
                "import os\n"
                "from conftest import record\n"
                "def test_case(request):\n"
                "    record('body', 'call', request.node.nodeid)\n"
                "    assert os.environ['PYTEST_CURRENT_TEST'] == request.node.nodeid + ' (call)'\n",
            )
        result = (
            project.run_pytest("-q", "--capture=sys", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                f"--workers={workers}",
                "--no-replay",
                "--no-cov",
                "--capture=sys",
                "-q",
                env={
                    "VFY_WORKER_START": "fork" if workers == 2 else None,
                    "VFY_NATIVE_SCHEDULING": "demand" if workers == 2 else None,
                },
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        rows = [
            json.loads(line) for line in (project.root / "events.jsonl").read_text().splitlines()
        ]
        observed[engine] = sorted(rows, key=lambda row: (row[2], row[0], str(row[1])))
        for name in ("a", "b"):
            nodeid = f"test_{name}.py::test_case"
            assert ["helper", "call", nodeid, nodeid + " (call)"] in rows
            assert ["helper", "teardown", nodeid, nodeid + " (teardown)"] in rows
            assert ["helper", None, nodeid, None] in rows
    assert observed["pytest-native"] == observed["pytest"]


_ENVIRONMENT = r"""import json
import os
import sys
from pathlib import Path
import pytest

MODE = MODE_VALUE
events = []
original_putenv = os.putenv
original_unsetenv = os.unsetenv
environment_alias = os.environ

class Environment(type(environment_alias)):
    def __setitem__(self, key, value):
        if key == 'PYTEST_CURRENT_TEST':
            events.append(['mapping-set', value])
        super().__setitem__(key, value)
    def __delitem__(self, key):
        if key == 'PYTEST_CURRENT_TEST':
            events.append(['mapping-delete'])
        super().__delitem__(key)

os.environ = Environment(
    environment_alias._data, environment_alias.encodekey, environment_alias.decodekey,
    environment_alias.encodevalue, environment_alias.decodevalue,
)

def normalize(value):
    return os.fsdecode(value)

def putenv(key, value):
    if normalize(key) == 'PYTEST_CURRENT_TEST':
        events.append(['putenv', normalize(value)])
    original_putenv(key, value)

def unsetenv(key):
    if normalize(key) == 'PYTEST_CURRENT_TEST':
        events.append(['unsetenv'])
    original_unsetenv(key)

def audit(event, args):
    if event in ('os.putenv', 'os.unsetenv') and normalize(args[0]) == 'PYTEST_CURRENT_TEST':
        events.append(['audit', event, *map(normalize, args)])

os.putenv = putenv
os.unsetenv = unsetenv
sys.addaudithook(audit)

def pytest_collection_modifyitems(items):
    for item in items:
        if hasattr(item, '_nodeid'):
            item._nodeid += '\x00sentinel'
        else:
            item._id = type(item.id).parse(item.nodeid + '\x00sentinel')

@pytest.fixture(autouse=True)
def observe(request):
    yield
    events.append(['finalizer', os.environ['PYTEST_CURRENT_TEST']])
    if MODE == 'missing':
        os.environ.pop('PYTEST_CURRENT_TEST')

def pytest_sessionfinish():
    events.append(['finished', os.environ.get('PYTEST_CURRENT_TEST')])
    Path('environment.json').write_text(json.dumps(events))
"""


@pytest.mark.parametrize("mode", ["normal", "missing"])
def test_current_test_normalization_environment_callbacks_and_cleanup_errors(
    tmp_path: Path, mode: str
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", _ENVIRONMENT.replace("MODE_VALUE", repr(mode)))
        project.write(
            "test_case.py",
            "import os\n"
            "from conftest import environment_alias, events\n"
            "def test_case(request):\n"
            "    events.append(['body', os.environ['PYTEST_CURRENT_TEST']])\n"
            "    nodeid = request.node.nodeid.replace('\\x00', '(null)')\n"
            "    assert os.environ['PYTEST_CURRENT_TEST'] == nodeid + ' (call)'\n"
            "    assert environment_alias['PYTEST_CURRENT_TEST'] "
            "== os.environ['PYTEST_CURRENT_TEST']\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=pytest-native", "--workers=1", "--no-replay", "--no-cov", "-q"
            )
        )
        assert result.returncode == (0 if mode == "normal" else 1), result.stdout + result.stderr
        if mode == "missing":
            assert "KeyError" in result.stdout + result.stderr
        events = json.loads((project.root / "environment.json").read_text())
        assert ["body", "test_case.py::test_case(null)sentinel (call)"] in events
        assert ["putenv", "test_case.py::test_case(null)sentinel (call)"] in events
        assert ["putenv", "test_case.py::test_case(null)sentinel (teardown)"] in events
        assert ["finished", None] in events
        observed[engine] = events
    assert observed["pytest-native"] == observed["pytest"]
