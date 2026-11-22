"""Source locations preserve live filesystem and caller-owned path protocols."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_function_reportinfo_rechecks_code_cwd_and_source_existence(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_body.py", "def test_body():\n    pass\n")
    project.write(
        "conftest.py",
        """import json
import os
from pathlib import Path

def pytest_collection_modifyitems(session, items):
    item = items[0]
    original = item.obj
    root = session.config.rootpath
    records = []
    namespace = {}
    exec(compile('def replacement():\\n    pass\\n', 'dynamic.py', 'exec'), namespace)
    replacement = namespace['replacement']
    item.obj = replacement
    def record():
        path, line, domain = item.reportinfo()
        records.append([isinstance(path, Path), os.fspath(path), line, domain])
    try:
        record()
        (root / 'dynamic.py').write_text('')
        record()
        (root / 'dynamic.py').unlink()
        record()
        other = root / 'other'
        other.mkdir(exist_ok=True)
        (other / 'dynamic.py').write_text('')
        os.chdir(other)
        record()
        replacement.__code__ = replacement.__code__.replace(co_firstlineno=19)
        record()
        replacement.__code__ = replacement.__code__.replace(co_filename='')
        record()
        for filename in ('nul\\x00.py', 'x' * 300 + '.py'):
            replacement.__code__ = replacement.__code__.replace(co_filename=filename)
            record()
    finally:
        os.chdir(root)
        item.obj = original
    (root / 'locations.json').write_text(json.dumps(records))
""",
    )
    oracle = project.run_engine("stock", "-q")
    oracle.expect(0)
    output = tmp_path / "locations.json"
    expected = json.loads(output.read_text())
    output.unlink()
    candidate = project.run_engine("native")
    assert candidate.returncode == oracle.returncode, candidate.stdout + candidate.stderr
    assert json.loads(output.read_text()) == expected
    assert [record[0] for record in expected] == [
        False,
        True,
        False,
        True,
        True,
        False,
        False,
        False,
    ]
    assert [record[2] for record in expected] == [0, 0, 0, 0, 18, 18, 18, 18]


def test_function_location_keeps_reportinfo_descriptors_and_custom_paths(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_body.py", "def test_body():\n    pass\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

def pytest_collection_modifyitems(session, items):
    item = items[0]
    owner = type(item)
    original = owner.reportinfo
    original_relative = session._node_location_to_relpath
    events = []
    records = []
    identities = []
    reported = None
    def relative(path):
        identities.append(path is reported)
        return original_relative(path)
    session._node_location_to_relpath = relative
    class Source:
        def __fspath__(self):
            events.append('fspath')
            return 'virtual.py'
    class ReportInfo:
        def __get__(self, instance, cls):
            events.append('descriptor')
            def reportinfo():
                events.append('reportinfo')
                return Source(), 13, 'descriptor domain'
            return reportinfo
    class CustomPath(type(Path())):
        def __fspath__(self):
            events.append('path-fspath')
            return super().__fspath__()
    def record():
        item.__dict__.pop('location', None)
        location = item.location
        assert item.location is location
        records.append(list(location))
    try:
        owner.reportinfo = ReportInfo()
        record()
        item.reportinfo = lambda: (Source(), 17, 'instance domain')
        record()
        del item.reportinfo
        root = session.config.rootpath
        for path in (Path('relative.py'), root / 'absolute.py',
                     root / 'nested/../parent.py', CustomPath(root / 'custom.py')):
            reported = path
            item.reportinfo = lambda path=path: (path, 23, 'path domain')
            record()
        del item.reportinfo
    finally:
        owner.reportinfo = original
        session._node_location_to_relpath = original_relative
        item.__dict__.pop('location', None)
    Path('locations.json').write_text(json.dumps({
        'events': events, 'records': records, 'identities': identities,
    }))
""",
    )
    oracle = project.run_engine("stock", "-q")
    oracle.expect(0)
    output = tmp_path / "locations.json"
    expected = json.loads(output.read_text())
    output.unlink()
    candidate = project.run_engine("native")
    assert candidate.returncode == oracle.returncode, candidate.stdout + candidate.stderr
    assert json.loads(output.read_text()) == expected
    assert expected["events"] == ["descriptor", "reportinfo", "fspath", "fspath", "path-fspath"]
    assert expected["identities"] == [False] * 6


@pytest.mark.parametrize("mode", ["exists", "stat", "fspath"])
def test_source_locations_keep_standard_path_and_filesystem_overrides(
    tmp_path: Path, mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_body.py", "def test_body(): pass\n")
    project.write(
        "conftest.py",
        f"""import json, os
from pathlib import Path
mode = {mode!r}

def pytest_collection_modifyitems(session, items):
    item = items[0]
    events = []
    reported = session.config.rootpath / 'reported.py'
    original_exists = Path.exists
    original_stat = os.stat
    original_fspath = Path.__fspath__
    own_fspath = "__fspath__" in Path.__dict__
    def exists(path):
        events.append('exists')
        return False
    def stat(path, *args, **kwargs):
        events.append('stat')
        raise FileNotFoundError('stat witness')
    def fspath(path):
        if path is reported:
            events.append('fspath')
            return 'virtual.py'
        return original_fspath(path)
    try:
        if mode == 'exists':
            Path.exists = exists
        elif mode == 'stat':
            os.stat = stat
        else:
            Path.__fspath__ = fspath
            item.reportinfo = lambda: (reported, 23, 'path domain')
            item.__dict__.pop('location', None)
        location = item.location if mode == 'fspath' else item.reportinfo()
        record = [isinstance(location[0], Path), str(location[0]), location[1], location[2]]
    finally:
        if mode == 'exists':
            Path.exists = original_exists
        elif mode == 'stat':
            os.stat = original_stat
        else:
            if own_fspath:
                Path.__fspath__ = original_fspath
            else:
                del Path.__fspath__
            del item.reportinfo
            item.__dict__.pop('location', None)
    Path('locations.json').write_text(json.dumps({{'events': events, 'record': record}}))
""",
    )
    oracle = project.run_engine("stock", "-q")
    oracle.expect(0)
    output = tmp_path / "locations.json"
    expected = json.loads(output.read_text())
    assert expected["events"] == [mode]
    assert expected["record"][0] is False
    output.rename(tmp_path / "stock-locations.json")
    candidate = project.run_engine("native")
    assert candidate.returncode == oracle.returncode, candidate.stdout + candidate.stderr
    assert json.loads(output.read_text()) == expected


def test_session_relative_paths_capture_root(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_body.py", "def test_body(): pass\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path


def pytest_collection_modifyitems(session, items):
    root = session.config.rootpath
    records = []
    records.append(session._node_location_to_relpath(root / 'virtual.py'))
    records.append(session._node_location_to_relpath(Path(root / 'virtual.py')))
    config_values = vars(session.config)
    missing = object()
    saved = {name: config_values.get(name, missing) for name in ('rootpath', '_rootpath')}
    try:
        config_values['rootpath'] = root / 'changed'
        config_values['_rootpath'] = root / 'changed'
        records.append(session._node_location_to_relpath(root / 'uncached.py'))
    finally:
        for name, value in saved.items():
            if value is missing:
                config_values.pop(name, None)
            else:
                config_values[name] = value

    (root / 'locations.json').write_text(json.dumps(records))
""",
    )
    oracle = project.run_engine("stock", "-q")
    oracle.expect(0)
    output = tmp_path / "locations.json"
    expected = json.loads(output.read_text())
    output.unlink()
    candidate = project.run_engine("native")
    assert candidate.returncode == oracle.returncode, candidate.stdout + candidate.stderr
    assert json.loads(output.read_text()) == expected
    assert expected == ["virtual.py", "virtual.py", "uncached.py"]


def test_location_uses_imported_modules_after_sys_modules_replacement(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_body.py", "def test_body(): pass\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import sys
import types

def pytest_collection_modifyitems(session, items):
    item = items[0]
    item.reportinfo = lambda: (Path('test_body.py'), 7, 'sentinel')
    item.__dict__.pop('location', None)
    expected = item.location
    item.__dict__.pop('location', None)
    names = ('os.path', 'pathlib')
    original = {name: sys.modules.get(name) for name in names}
    def fail(*args, **kwargs):
        raise AssertionError('late sys.modules replacement was used')
    decoy_path = types.ModuleType('os.path')
    decoy_path.abspath = fail
    decoy_pathlib = types.ModuleType('pathlib')
    decoy_pathlib.Path = fail
    try:
        sys.modules['os.path'] = decoy_path
        sys.modules['pathlib'] = decoy_pathlib
        observed = item.location
    finally:
        for name, module in original.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        item.__dict__.pop('location', None)
    assert observed == expected
    Path('location-module.json').write_text(json.dumps(observed))
""",
    )
    oracle = project.run_engine("stock", "-q")
    oracle.expect(0)
    output = tmp_path / "location-module.json"
    expected = json.loads(output.read_text())
    output.unlink()
    candidate = project.run_engine("native", "-q")
    assert candidate.returncode == oracle.returncode, candidate.stdout + candidate.stderr
    assert json.loads(output.read_text()) == expected
