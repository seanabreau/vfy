"""The dependency-free mock replacement keeps real public objects and cleanup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

_ARGUMENTS = ("test", "--engine=native", "--no-cov", "--workers=1", "--capture=sys", "-q")


@requires_native_pytest
def test_owned_mock_replacement_is_lazy_and_preserves_public_fixture_behavior(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    environment = project.root / ".independent-python"
    created = project.run_python("-m", "venv", "--without-pip", str(environment))
    created.expect(0)
    python = environment / "bin" / "python"
    env = {
        "PYTHONPATH": isolated_vfy_path(),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": None,
    }
    project.write(
        "test_body.py",
        """import sys

def test_unused():
    assert 'unittest.mock' not in sys.modules
    assert 'asyncio' not in sys.modules
    assert 'pytest_mock' not in sys.modules
""",
    )
    result = project.run(str(python), "-m", "vfy", *_ARGUMENTS, env=env)
    result.expect(0)
    project.write(
        "test_body.py",
        """import types, typing, unittest.mock, pytest_mock

assert type(pytest_mock) is types.ModuleType
assert pytest_mock.MockerFixture.Mock is unittest.mock.Mock
assert pytest_mock.Mock is unittest.mock.Mock
assert typing.get_type_hints(pytest_mock._mocker)['return'].__args__[0] is pytest_mock.MockerFixture
assert (typing.get_type_hints(pytest_mock.mocker._get_wrapped_function())['return'].__args__[0]
        is pytest_mock.MockerFixture)

class Target:
    session = module = package = cls = function = 'original'

class TestScopes:
    def test_patch(self, session_mocker, module_mocker, class_mocker, package_mocker, mocker):
        assert type(mocker) is pytest_mock.MockerFixture
        for fixture, name in ((session_mocker, 'session'), (module_mocker, 'module'),
                              (package_mocker, 'package'), (class_mocker, 'cls'),
                              (mocker, 'function')):
            fixture.patch.object(Target, name, name)

    def test_function_scope_cleanup(self):
        assert Target.function == 'original'
        assert (Target.session, Target.module, Target.package, Target.cls) == (
            'session', 'module', 'package', 'cls')

def test_class_scope_cleanup():
    assert Target.cls == 'original'
    assert (Target.session, Target.module, Target.package) == ('session', 'module', 'package')
""",
    )
    result = project.run(str(python), "-m", "vfy", *_ARGUMENTS, env=env)
    result.expect(0)
    project.write(
        "test_body.py",
        """import importlib, importlib.abc, importlib.machinery, sys, threading, time, types

def test_concurrent_import():
    entered, release, second_started = (threading.Event() for _ in range(3))
    results, errors = [], []
    class BlockingLoader(importlib.abc.Loader):
        def __init__(self, loader): self.loader = loader
        def create_module(self, spec): return self.loader.create_module(spec)
        def exec_module(self, module):
            entered.set()
            assert release.wait(5), 'mock import was not released'
            self.loader.exec_module(module)
    class BlockMock(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == 'unittest.mock':
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                assert spec is not None and spec.loader is not None
                spec.loader = BlockingLoader(spec.loader)
                return spec
    finder = BlockMock()
    sys.meta_path.insert(0, finder)
    def load(started=None):
        try:
            if started is not None: started.set()
            module = importlib.import_module('pytest_mock')
            results.append((module, module.Mock))
        except BaseException as error:
            errors.append(error)
    first = threading.Thread(target=load)
    second = threading.Thread(target=load, args=(second_started,))
    first.start()
    try:
        assert entered.wait(5), 'mock import did not start'
        second.start()
        assert second_started.wait(5)
        deadline = time.monotonic() + 5
        while True:
            frame = sys._current_frames().get(second.ident)
            while frame is not None and not (frame.f_code.co_name == 'acquire'
                                             and 'importlib' in frame.f_code.co_filename):
                frame = frame.f_back
            if frame is not None: break
            assert second.is_alive() and time.monotonic() < deadline, 'second import did not wait'
            threading.Event().wait(0.001)
    finally:
        release.set()
        first.join(5)
        if second.ident is not None: second.join(5)
        sys.meta_path.remove(finder)
    assert not first.is_alive() and not second.is_alive()
    assert not errors, errors
    assert len(results) == 2 and results[0][0] is results[1][0]
    assert type(results[0][0]) is types.ModuleType
    assert results[0][1] is results[1][1] is sys.modules['unittest.mock'].Mock
""",
    )
    result = project.run(str(python), "-m", "vfy", *_ARGUMENTS, env=env)
    result.expect(0)


@requires_native_pytest
@pytest.mark.parametrize("replace", [False, True])
def test_owned_mock_replacement_alias_is_removed_when_session_closes(
    tmp_path: Path, replace: bool
) -> None:
    project = VfyProject(tmp_path)
    environment = project.root / ".independent-python"
    created = project.run_python("-m", "venv", "--without-pip", str(environment))
    created.expect(0)
    project.write(
        "observer.py",
        """import atexit, importlib.util, json, sys
from pathlib import Path

def snapshot():
    module = sys.modules.get('pytest_mock')
    Path('alias.json').write_text(json.dumps({
        'loaded': module is not None,
        'replacement': getattr(module, 'replacement', False),
        'findable': module is None and importlib.util.find_spec('pytest_mock') is not None,
    }))

atexit.register(snapshot)
""",
    )
    project.write(
        "test_body.py",
        f"""import sys
from types import ModuleType

def test_body():
    import pytest_mock
    assert sys.modules['pytest_mock'] is pytest_mock
    if {replace!r}:
        replacement = ModuleType('replacement')
        replacement.replacement = True
        sys.modules['pytest_mock'] = replacement
""",
    )
    result = project.run(
        str(environment / "bin" / "python"),
        "-m",
        "vfy",
        *_ARGUMENTS,
        "-p",
        "observer",
        env={
            "PYTHONPATH": isolated_vfy_path(),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": None,
        },
    )
    result.expect(0)
    assert json.loads((tmp_path / "alias.json").read_text()) == {
        "loaded": replace,
        "replacement": replace,
        "findable": False,
    }
