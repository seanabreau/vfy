"""The real cache provider uses native storage and ordinary fixture lifetimes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import ENGINES, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@ENGINES
def test_cache_owner_is_shared_across_files_and_persists(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path
owners=[]
events=[]
@pytest.fixture
def cache(cache, request):
    owners.append(cache)
    events.append('setup:' + request.node.name)
    yield cache
    events.append('teardown:' + request.node.name)
def pytest_sessionfinish(session):
    Path('owner.json').write_text(json.dumps({
        'same': all(owner is session.config.cache for owner in owners),
        'owners': len(owners), 'events': events,
    }))
""",
    )
    project.write(
        "test_a.py",
        """import pytest
def test_first(cache, pytestconfig):
    assert isinstance(cache, pytest.Cache)
    assert cache is pytestconfig.cache
    assert repr(cache) == 'Cache()'
    current = cache.get('project/count', 0)
    cache.set('project/count', current + 1)
""",
    )
    project.write(
        "test_b.py",
        """import json
from pathlib import Path
def test_second(cache):
    Path('count.json').write_text(json.dumps(cache.get('project/count', None)))
""",
    )
    for count in (1, 2):
        result = project.run_engine(engine, "-q")
        result.expect(0)
        assert json.loads((tmp_path / "count.json").read_text()) == count
        assert json.loads((tmp_path / "owner.json").read_text()) == {
            "same": True,
            "owners": 2,
            "events": [
                "setup:test_first",
                "teardown:test_first",
                "setup:test_second",
                "teardown:test_second",
            ],
        }


@ENGINES
def test_cache_defaults_json_errors_and_directory_effects(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_cache.py",
        """import json, pytest, sys
from pathlib import Path
def test_storage(cache, pytestconfig):
    default = object()
    assert cache.get('project/missing', default) is default
    cache.set('project/value', {'text': 'é', 'items': [1, None, False]})
    path = cache._getvaluepath('project/value')
    before = path.read_bytes()
    with pytest.raises(TypeError):
        cache.set('project/value', {'unsupported': object()})
    assert path.read_bytes() == before
    assert cache.get('project/value', None) == {'text': 'é', 'items': [1, None, False]}
    path.write_text('{bad json')
    assert cache.get('project/value', default) is default
    path.write_text('9' * (sys.get_int_max_str_digits() + 1))
    assert cache.get('project/value', default) is default
    folder = cache.mkdir('project')
    assert isinstance(folder, Path) and folder.is_dir()
    assert cache.mkdir('project') == folder
    with pytest.raises(ValueError, match='path separators'):
        cache.mkdir('project/nested')
    root = folder.parent.parent
    assert all((root / name).is_file() for name in ('README.md', '.gitignore', 'CACHEDIR.TAG'))
    assert pytestconfig.getini('cache_dir') == 'custom-cache'
    Path('storage.json').write_text(json.dumps({
        'directory': folder.relative_to(pytestconfig.rootpath).as_posix(),
        'value_path': path.relative_to(pytestconfig.rootpath).as_posix(),
    }))
""",
    )
    result = project.run_engine(engine, "-q", "-o", "cache_dir=custom-cache")
    result.expect(0)
    assert json.loads((tmp_path / "storage.json").read_text()) == {
        "directory": "custom-cache/d/project",
        "value_path": "custom-cache/v/project/value",
    }


@ENGINES
def test_cache_write_error_warns_without_replacing_existing_file(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("unavailable-cache", "keep this file")
    project.write(
        "test_cache.py",
        """import pytest
from pathlib import Path
def test_unwritable(cache):
    default = object()
    assert cache.get('project/value', default) is default
    with pytest.warns(pytest.PytestCacheWarning, match='could not create cache path'):
        cache.set('project/value', {'answer': 42})
    assert Path('unavailable-cache').read_text() == 'keep this file'
""",
    )
    result = project.run_engine(engine, "-q", "-o", "cache_dir=unavailable-cache")
    result.expect(0)
    assert (tmp_path / "unavailable-cache").read_text() == "keep this file"


@ENGINES
@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
def test_cache_default_preserves_raw_tox_prefix_and_resolves_storage(
    tmp_path: Path, engine: str, absolute: bool
) -> None:
    project = VfyProject(tmp_path)
    prefix = tmp_path / "tox" if absolute else Path("tox")
    project.write(
        "test_cache.py",
        """import json
from pathlib import Path
def test_default(cache, pytestconfig):
    cache.set('project/tox', {'saved': True})
    assert cache.get('project/tox', None) == {'saved': True}
    Path('tox.json').write_text(json.dumps({
        'raw':pytestconfig.getini('cache_dir'),
        'directory':str(cache._cachedir),
    }))
""",
    )
    result = project.run_engine(engine, "-q", env={"TOX_ENV_DIR": str(prefix)})
    result.expect(0)
    assert json.loads((tmp_path / "tox.json").read_text()) == {
        "raw": str(prefix / ".pytest_cache"),
        "directory": str(tmp_path / "tox" / ".pytest_cache"),
    }


@ENGINES
@pytest.mark.parametrize(
    ("stage", "action"),
    [
        ("startup", "block"),
        ("configure", "block"),
        ("configure", "unregister"),
        ("sessionstart", "block"),
        ("sessionstart", "unregister"),
        ("active", "none"),
    ],
)
def test_cache_provider_registration_history_survives_late_removal(
    tmp_path: Path, engine: str, stage: str, action: str
) -> None:
    project = VfyProject(tmp_path)
    trigger = (
        "config.pluginmanager.set_blocked('cacheprovider')"
        if action == "block"
        else "config.pluginmanager.unregister(name='cacheprovider')"
    )
    observer = """import json, pytest
from pathlib import Path
seen=[]
def pytest_plugin_registered(plugin_name):
    if plugin_name == 'cacheprovider': seen.append(plugin_name)
def pytest_collection_modifyitems(config, items):
    Path('provider.json').write_text(json.dumps({
        'registered':seen, 'cache':hasattr(config, 'cache'),
        'plugin':config.pluginmanager.hasplugin('cacheprovider'),
        'declaration':hasattr(config.pluginmanager.get_plugin('cacheprovider'), 'cache'),
    }))
"""
    if stage == "configure":
        observer += (
            "@pytest.hookimpl(tryfirst=True)\ndef pytest_configure(config):\n    " + trigger + "\n"
        )
    elif stage == "sessionstart":
        observer += (
            "def pytest_sessionstart(session):\n    config=session.config\n    " + trigger + "\n"
        )
    project.write("conftest.py", observer)
    project.write(
        "test_cache.py",
        """import pytest
def test_cache(request):
    if hasattr(request.config, 'cache'):
        cache = request.getfixturevalue('cache')
        assert cache is request.config.cache
        assert cache.get('project/missing', 42) == 42
    else:
        with pytest.raises(pytest.FixtureLookupError):
            request.getfixturevalue('cache')
""",
    )
    arguments = ("-p", "no:cacheprovider") if stage == "startup" else ()
    result = project.run_engine(engine, "-q", *arguments)
    result.expect(0)
    assert json.loads((tmp_path / "provider.json").read_text()) == {
        "registered": [] if stage == "startup" else ["cacheprovider"],
        "cache": stage != "startup",
        "plugin": stage == "active",
        "declaration": stage == "active",
    }
    if stage == "startup":
        assert not (tmp_path / ".pytest_cache").exists()


@ENGINES
def test_cache_public_namespace_uses_the_active_provider(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_cache.py",
        """import pytest
from _pytest.cacheprovider import Cache
def test_namespace(cache):
    assert Cache is pytest.Cache
    assert isinstance(cache, Cache)
    cache.set('project/namespace', {'active': True})
    assert cache.get('project/namespace', None) == {'active': True}
""",
    )
    arguments = ("-p", "vfy.independent.outcomes") if engine == "independent" else ()
    result = project.run_engine(engine, "-q", *arguments)
    result.expect(0)


@ENGINES
def test_failed_fixture_acquisition_keeps_cache_and_runs_cleanup(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_cache.py",
        """import json, pytest
from pathlib import Path
events=[]
@pytest.fixture
def flaky(cache, request):
    attempt = cache.get('project/attempt', 0) + 1
    cache.set('project/attempt', attempt)
    events.append('setup' + str(attempt))
    def cleanup():
        events.append('cleanup' + str(attempt))
        Path('events.json').write_text(json.dumps(events))
    request.addfinalizer(cleanup)
    if attempt == 1:
        raise ValueError('cache acquisition witness')
    return cache
def test_first(flaky):
    raise AssertionError('failed acquisition must skip the body')
def test_second(flaky):
    assert flaky.get('project/attempt', None) == 2
    events.append('body2')
""",
    )
    result = project.run_engine(engine, "-q")
    result.expect(1)
    assert "cache acquisition witness" in result.stdout + result.stderr
    assert json.loads((tmp_path / "events.json").read_text()) == [
        "setup1",
        "cleanup1",
        "setup2",
        "body2",
        "cleanup2",
    ]


@ENGINES
def test_cache_fixture_keeps_function_scope(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_cache.py",
        """import pytest
@pytest.fixture(scope='session')
def broad(cache):
    return cache
def test_invalid_scope(broad):
    raise AssertionError('a broad fixture cannot request the function cache fixture')
""",
    )
    result = project.run_engine(engine, "-q")
    result.expect(1)
    assert "ScopeMismatch" in result.stdout + result.stderr


@pytest.mark.parametrize("option", ["--lf", "--ff", "--cache-clear", "--cache-show"])
def test_unimplemented_cache_selection_and_display_options_are_rejected(
    tmp_path: Path, option: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_cache.py",
        "from pathlib import Path\nPath('imported').touch()\ndef test_value(): pass\n",
    )
    result = project.run_engine("native", "-q", option)
    result.expect(4)
    assert not (tmp_path / "imported").exists()


def test_cache_legacy_directory_adapter_uses_the_same_owner(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_cache.py",
        """def test_legacy(cache):
    directory = cache.makedir('project')
    directory.join('result').write('persisted')
    assert (cache.mkdir('project') / 'result').read_text() == 'persisted'
""",
    )
    result = project.run_engine("native", "-q")
    result.expect(0)
