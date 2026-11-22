"""Reversible mutation journals follow pytest's Python object protocols."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _compare(project: VfyProject, *, private: bool = False) -> None:
    arguments = ["-q"]
    stock = project.run_python("-m", "pytest", *arguments)
    project.write("stock.stdout", stock.stdout)
    project.write("stock.stderr", stock.stderr)
    stock.expect(0)
    expected = json.loads((project.root / "observed.json").read_text())
    project.write("stock-observed.json", json.dumps(expected, indent=2))
    (project.root / "observed.json").unlink()
    if private:
        arguments += ["-p", "vfy.independent.outcomes"]
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    project.write("independent.stdout", candidate.stdout)
    project.write("independent.stderr", candidate.stderr)
    candidate.expect(0)
    assert json.loads((project.root / "observed.json").read_text()) == expected


def test_public_attribute_mapping_environment_and_path_undo_order(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_changes.py",
        """import json
import os
import sys
import warnings
from pathlib import Path
import pytest

def test_changes(monkeypatch):
    events = []
    class Subject:
        value = 'old'
        def __repr__(self): return '<subject>'
        def __setattr__(self, name, value):
            events.append(['attribute', name, value])
            object.__setattr__(self, name, value)
        def __delattr__(self, name):
            events.append(['delete attribute', name])
            object.__delattr__(self, name)
    class Values(dict):
        def get(self, name, default=None):
            events.append(['get', name])
            return super().get(name, default)
        def __setitem__(self, name, value):
            events.append(['item', name, value])
            super().__setitem__(name, value)
        def __delitem__(self, name):
            events.append(['delete item', name])
            super().__delitem__(name)
    subject = Subject()
    values = Values(key='original')
    original_path = sys.path[:]
    original_cwd = Path.cwd()
    first = original_cwd / 'first'
    second = original_cwd / 'second'
    first.mkdir(exist_ok=True)
    second.mkdir(exist_ok=True)
    monkeypatch.setattr(subject, 'value', 'one')
    monkeypatch.setitem(values, 'key', 'one')
    monkeypatch.setattr(subject, 'value', 'two')
    monkeypatch.setitem(values, 'new', 'two')
    monkeypatch.syspath_prepend(first)
    monkeypatch.syspath_prepend(second)
    monkeypatch.chdir(first)
    monkeypatch.chdir(second)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        monkeypatch.setenv('VFY_PRIVATE_MONKEYPATCH_VALUE', 42)
    warning = [type(caught[0].message).__name__, str(caught[0].message),
               Path(caught[0].filename).name]
    assert os.environ['VFY_PRIVATE_MONKEYPATCH_VALUE'] == '42'
    monkeypatch.undo()
    assert Path.cwd() == original_cwd and sys.path == original_path
    assert 'VFY_PRIVATE_MONKEYPATCH_VALUE' not in os.environ
    assert subject.value == 'old'
    assert values == {'key': 'original'}
    monkeypatch.undo()
    failures = []
    for operation in [lambda: monkeypatch.setattr(subject, 'missing', 1),
                      lambda: monkeypatch.delattr(subject, 'missing'),
                      lambda: monkeypatch.delitem(values, 'missing')]:
        try: operation()
        except (AttributeError, KeyError) as error:
            failures.append([type(error).__name__, list(error.args)])
    monkeypatch.delattr(subject, 'missing', raising=False)
    monkeypatch.delitem(values, 'missing', raising=False)
    with pytest.MonkeyPatch.context() as nested:
        nested.setitem(values, 'temporary', 'context')
    assert values == {'key': 'original'}
    Path('observed.json').write_text(json.dumps([events, warning, failures]))
""",
    )
    _compare(project)


def test_dotted_imports_descriptors_attribute_failures_and_direct_append(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "targets.py",
        """class Base:
    inherited = 'parent'
class Subject(Base):
    @staticmethod
    def static(): return 'static'
    @classmethod
    def method(cls): return cls.__name__
value = 'module value'
""",
    )
    project.write(
        "test_attributes.py",
        """import json
from pathlib import Path
import pytest
import targets
from _pytest.compat import NOTSET

def test_attributes():
    patch = pytest.MonkeyPatch()
    cls = targets.Subject
    original_static = vars(cls)['static']
    original_method = vars(cls)['method']
    patch.setattr(cls, 'static', lambda: 'patched')
    patch.setattr(cls, 'method', lambda: 'patched')
    patch.setattr(cls, 'inherited', 'child')
    patch.setattr('targets.value', 'changed')
    patch.setattr('targets.created', 'new', raising=False)
    patch.undo()
    assert vars(cls)['static'] is original_static and vars(cls)['method'] is original_method
    assert 'inherited' not in vars(cls) and cls.inherited == 'parent'
    assert targets.value == 'module value' and not hasattr(targets, 'created')
    events = []
    class Subject:
        value = 'old'
        fail = False
        def __repr__(self): return '<subject>'
        def __setattr__(self, name, value):
            events.append(['set', name, value])
            if type(self).fail: raise ValueError('attribute write rejected')
            object.__setattr__(self, name, value)
        def __delattr__(self, name):
            events.append(['del', name])
            if type(self).fail: raise ValueError('attribute delete rejected')
            object.__delattr__(self, name)
    subject = Subject()
    Subject.fail = True
    with pytest.raises(ValueError): patch.setattr(subject, 'value', 'new')
    assert patch._setattr == []
    Subject.fail = False
    patch.setattr(subject, 'value', 'new')
    Subject.fail = True
    with pytest.raises(ValueError): patch.delattr(subject, 'value')
    assert len(patch._setattr) == 2
    with pytest.raises(ValueError): patch.undo()
    assert len(patch._setattr) == 2
    Subject.fail = False
    patch.undo()
    patch._setattr.append((targets, 'future', NOTSET))
    targets.future = 'external'
    patch.undo()
    assert not hasattr(targets, 'future')
    class Raising:
        def __bool__(self):
            events.append(['raising bool'])
            return False
    patch.setattr(subject, 'extra', 'created', raising=Raising())
    patch.delattr(subject, 'absent', raising=Raising())
    patch.undo()
    class NonScalarRepr:
        def __repr__(self): return chr(0xD800)
    errors = []
    for operation in [lambda: patch.setattr(NonScalarRepr(), 'missing', 1),
                      lambda: patch.setattr('targets.absent', 1),
                      lambda: patch.delattr('targets.absent'),
                      lambda: patch.setattr('undotted', 1),
                      lambda: patch.setattr(42, 1),
                      lambda: patch.setattr(subject, 1, 2),
                      lambda: patch.delattr(42),
                      lambda: patch.setattr('targets.missing.member', 1)]:
        try: operation()
        except (TypeError, AttributeError, ImportError) as error:
            errors.append([type(error).__name__, str(error),
                           None if error.__cause__ is None else type(error.__cause__).__name__])
    Path('observed.json').write_text(json.dumps([events, errors]))
""",
    )
    _compare(project, private=True)


def test_syspath_namespace_protocol_warning_and_first_snapshot(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_path.py",
        """import json
import sys
import types
import warnings
from pathlib import Path
import pytest

def test_path(monkeypatch):
    directory = Path.cwd() / 'namespace'
    (directory / 'legacy' / 'nested').mkdir(parents=True, exist_ok=True)
    events = []
    class Directory:
        def __str__(self):
            events.append('str')
            return str(directory)
    resources = types.ModuleType('pkg_resources')
    resources._namespace_packages = {None: ['legacy'], 'legacy.nested': []}
    def fixup(path): events.append(['fixup', path == str(directory)])
    resources.fixup_namespace_packages = fixup
    monkeypatch.setitem(sys.modules, 'pkg_resources', resources)
    original = sys.path[:]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        monkeypatch.syspath_prepend(Directory())
    warning = [type(caught[0].message).__name__, str(caught[0].message),
               Path(caught[0].filename).name]
    assert sys.path[0] == str(directory)
    monkeypatch.undo()
    assert sys.path == original
    absent = pytest.MonkeyPatch()
    absent.setitem(sys.modules, 'pkg_resources', None)
    try: absent.syspath_prepend(directory)
    except ModuleNotFoundError as error: failure = [type(error).__name__, str(error)]
    else: raise AssertionError('None module entry was accepted')
    absent.undo()
    assert sys.path == original
    assert type(caught[0].message) is pytest.PytestRemovedIn10Warning
    assert type(caught[0].message).__module__ == 'pytest'
    Path('observed.json').write_text(json.dumps([events, warning, failure]))
""",
    )
    _compare(project)
