"""Default independent-engine locations preserve reportinfo's public and override behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_default_location_uses_live_reportinfo_and_preserves_public_paths(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
import inspect
import json
from pathlib import Path

events = []
activation = []

def pytest_collection_modifyitems(items):
    for item in items:
        if item.name == 'test_virtual':
            Path('missing-source.py').write_text('deleted')
            Path('missing-source.py').unlink()
            item.obj.__code__ = item.obj.__code__.replace(co_filename='missing-source.py')
        if item.name == 'test_metadata':
            def alternate(): pass
            alternate.__code__ = alternate.__code__.replace(co_filename='metadata-source.py')
            item.obj.place_as = alternate
        if item.name == 'test_custom':
            item.reportinfo = lambda: ('custom-source.py', 19, 'custom')
        vars(item).pop('location', None)
        activation.append(inspect.isbuiltin(getattr(item.reportinfo, '__func__', None)))
        original_exists = Path.exists
        calls = []
        def observed_exists(path):
            calls.append(str(path))
            return original_exists(path)
        Path.exists = observed_exists
        try:
            location = item.location
            location_stats = list(calls)
            calls.clear()
            info = item.reportinfo()
            public_stats = list(calls)
        finally:
            Path.exists = original_exists
        events.append([item.name, list(location), str(info[0]), info[1], info[2],
                       len(location_stats), len(public_stats)])

def pytest_sessionfinish():
    Path('locations.json').write_text(json.dumps(events))
    Path('activation.json').write_text(json.dumps(activation))
""",
        )
        project.write(
            "test_locations.py",
            "def test_regular(): pass\ndef test_virtual(): pass\n"
            "def test_metadata(): pass\ndef test_custom(): pass\n",
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
        assert "4 passed" in result.stdout
        rows = json.loads((project.root / "locations.json").read_text())
        observations[engine] = [[row[0], row[1], row[3], row[4]] for row in rows]
        if engine == "native":
            assert json.loads((project.root / "activation.json").read_text()) == [
                True,
                True,
                True,
                False,
            ]
            assert [row[5:] for row in rows] == [[0, 1], [1, 1], [1, 1], [0, 0]]
            assert rows[0][2].endswith("test_locations.py")
            assert rows[1][2] == "missing-source.py"
    assert observations["native"] == observations["pytest"]


@requires_native_pytest
@pytest.mark.parametrize("source_exists", [True, False])
def test_relative_source_follows_domain_directory_changes_when_missing(
    tmp_path: Path, source_exists: bool
) -> None:
    locations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / str(source_exists) / engine)
        project.write(
            "conftest.py",
            """\
import json
import os
from pathlib import Path

def pytest_collection_modifyitems(items):
    item = items[0]
    if SOURCE_EXISTS:
        Path('relative-source.py').write_text('source')
    Path('elsewhere').mkdir()
    destination = str(Path('elsewhere').resolve())
    item.obj.__code__ = item.obj.__code__.replace(co_filename='relative-source.py')
    original_class = type(item)
    original_name = item.name
    calls = []
    class MovingItem(original_class):
        @property
        def name(self):
            calls.append('name')
            os.chdir(destination)
            return original_name
    previous = os.getcwd()
    item.__class__ = MovingItem
    try:
        vars(item).pop('location', None)
        location = item.location
    finally:
        os.chdir(previous)
        item.__class__ = original_class
    Path('location.json').write_text(json.dumps([location, calls]))
""".replace("SOURCE_EXISTS", str(source_exists)),
        )
        project.write("test_relative.py", "def test_one(): pass\n")
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
        location, calls = json.loads((project.root / "location.json").read_text())
        assert calls, "domain lookup did not invoke the directory-changing property"
        locations[engine] = location
    assert locations["native"] == locations["pytest"]
    assert locations["native"][0] == (
        "relative-source.py" if source_exists else "elsewhere/relative-source.py"
    )


@requires_native_pytest
def test_callable_without_source_normalizes_empty_report_path(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
import json
from pathlib import Path

def pytest_collection_modifyitems(items):
    item = items[0]
    original = item.obj
    item.obj = len
    try:
        vars(item).pop('location', None)
        info = item.reportinfo()
        location = item.location
    finally:
        item.obj = original
        vars(item).pop('location', None)
    Path('location.json').write_text(json.dumps([info, location]))
""",
        )
        project.write("test_fallback.py", "def test_one(): pass\n")
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
        observations[engine] = json.loads((project.root / "location.json").read_text())
    assert observations["native"] == observations["pytest"]
    assert observations["native"][0][:2] == ["", -1]
    assert observations["native"][1][0] == "."


@requires_native_pytest
def test_reportinfo_method_binding_matches_pytest(tmp_path: Path) -> None:
    outcomes = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
import inspect
import json
from pathlib import Path

def pytest_collection_modifyitems(items):
    item = items[0]
    unbound = type(item).reportinfo
    bound = item.reportinfo
    owner = type(item)
    owner.reportinfo = lambda self: ('replacement.py', 9, 'replacement')
    replaced = item.reportinfo()
    owner.reportinfo = unbound
    restored = item.reportinfo()
    comparisons = []
    class EqualSelf(str):
        __hash__ = str.__hash__
        def __eq__(self, other):
            comparisons.append(other)
            return other == 'self'
    success = [unbound(item), unbound(self=item), unbound(**{EqualSelf('other'): item}), bound()]
    errors = []
    for callback, args, kwargs in (
        (unbound, (), {}),
        (unbound, (item, item), {}),
        (unbound, (item,), {'self': item}),
        (unbound, (item,), {'unexpected': item}),
        (unbound, (), {1: item}),
        (bound, (), {'self': item}),
        (bound, (item,), {}),
    ):
        try:
            callback(*args, **kwargs)
        except TypeError as error:
            errors.append(str(error).split('() ', 1)[-1])
        else:
            errors.append('NO ERROR')
    result = {
        'unbound': list(inspect.signature(unbound).parameters),
        'bound': list(inspect.signature(bound).parameters),
        'success': [[str(path), line, domain] for path, line, domain in success],
        'replacement': list(replaced),
        'restored': [str(restored[0]), restored[1], restored[2]],
        'compared': bool(comparisons),
        'errors': errors,
    }
    Path('reportinfo-binding.json').write_text(json.dumps(result))
""",
        )
        project.write("test_case.py", "def test_case(): pass\n")
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
        observed = json.loads((project.root / "reportinfo-binding.json").read_text())
        for record in observed["success"]:
            record[0] = Path(record[0]).name
        observed["restored"][0] = Path(observed["restored"][0]).name
        outcomes[engine] = observed
    assert outcomes["native"] == outcomes["pytest"]
    assert outcomes["native"]["unbound"] == ["self"]
    assert outcomes["native"]["bound"] == []
    assert outcomes["native"]["compared"]
