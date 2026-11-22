"""Cache provider boundaries retain storage and actual registration order."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import ENGINES, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("umask", [0o027, 0o077])
def test_cache_permissions_match_pytest_under_a_restrictive_umask(
    tmp_path: Path, umask: int
) -> None:
    modes = {}
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_cache.py",
            f"""import json
import os
import stat
from pathlib import Path

def test_cache(cache):
    root = Path('cache-root')
    path = root / 'v' / 'project' / 'value'
    previous = os.umask({umask})
    try:
        cache.set('project/value', {{'state': 'created'}})
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(0o664)
        cache.set('project/value', {{'state': 'replaced'}})
        assert cache.get('project/value', None) == {{'state': 'replaced'}}
        result = [stat.S_IMODE(root.stat().st_mode), mode, stat.S_IMODE(path.stat().st_mode)]
    finally:
        os.umask(previous)
    Path('modes.json').write_text(json.dumps(result))
""",
        )
        project.run_engine(engine, "-q", "-o", "cache_dir=cache-root").expect(0)
        modes[engine] = json.loads((project.root / "modes.json").read_text())

    assert modes["native"] == modes["stock"] == [0o777 & ~umask, 0o666 & ~umask, 0o664]


@ENGINES
def test_startup_can_reenable_the_actual_cache_builtin(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
seen=[]
def pytest_plugin_registered(plugin_name):
    if plugin_name == 'cacheprovider': seen.append(plugin_name)
def pytest_sessionfinish(session):
    Path('registered.json').write_text(json.dumps(seen))
""",
    )
    project.write(
        "test_cache.py",
        """import pytest
def test_cache(cache, pytestconfig):
    assert cache is pytestconfig.cache
    assert isinstance(cache, pytest.Cache)
    provider = pytestconfig.pluginmanager.get_plugin('cacheprovider')
    assert provider is not None and hasattr(provider, 'cache')
    cache.set('project/reenabled', {'saved': True})
    assert cache.get('project/reenabled', None) == {'saved': True}
""",
    )
    result = project.run_engine(engine, "-q", "-p", "no:cacheprovider", "-p", "cacheprovider")
    result.expect(0)
    assert json.loads((tmp_path / "registered.json").read_text()) == ["cacheprovider"]


@ENGINES
@pytest.mark.parametrize("blocked", [False, True], ids=["fresh", "blocked"])
def test_cache_prepares_the_path_before_json_callbacks_and_conversion_errors(
    tmp_path: Path, engine: str, blocked: bool
) -> None:
    project = VfyProject(tmp_path)
    if blocked:
        project.write("cache-root", "old cache root bytes")
    project.write(
        "test_cache.py",
        """import json, warnings
from pathlib import Path
def test_cache(cache):
    root = Path('cache-root')
    callbacks=[]
    class Value(dict):
        def items(self):
            callbacks.append((root / 'v/project').is_dir())
            return super().items()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        try:
            cache.set('project/value', Value(unsupported=object()))
        except BaseException as error:
            failure=type(error).__name__
        else:
            failure=None
    observation={
        'callbacks':callbacks,
        'error':failure,
        'warnings':[type(record.message).__name__ for record in caught],
        'support':all((root / name).is_file()
                      for name in ('README.md','.gitignore','CACHEDIR.TAG')),
        'parent':(root / 'v/project').is_dir(),
        'old_bytes':root.read_text() if root.is_file() else None,
        'value_exists':(root / 'v/project/value').exists(),
    }
    Path('failure.json').write_text(json.dumps(observation))
    if root.is_dir():
        cache.set('project/value', {'recovered': True})
        assert cache.get('project/value', None) == {'recovered': True}
""",
    )
    result = project.run_engine(engine, "-q", "-o", "cache_dir=cache-root")
    result.expect(0)
    assert json.loads((tmp_path / "failure.json").read_text()) == {
        "callbacks": [] if blocked else [True],
        "error": None if blocked else "TypeError",
        "warnings": ["PytestCacheWarning"] if blocked else [],
        "support": not blocked,
        "parent": not blocked,
        "old_bytes": "old cache root bytes" if blocked else None,
        "value_exists": False,
    }


@ENGINES
@pytest.mark.parametrize("order", [("a_plugin", "z_plugin"), ("z_plugin", "a_plugin")])
@pytest.mark.parametrize("action", ["none", "unregister", "block", "reregister", "replace"])
def test_plugin_fixture_priority_uses_each_actual_registration_event(
    tmp_path: Path, engine: str, order: tuple[str, str], action: str
) -> None:
    project = VfyProject(tmp_path)
    for name, label in (("a_plugin", "a"), ("z_plugin", "z")):
        project.write(
            name + ".py",
            f"""import pytest
@pytest.fixture
def cache(cache):
    previous = ['builtin'] if isinstance(cache, pytest.Cache) else cache
    return [*previous, {label!r}]
@pytest.fixture
def external(external):
    return [*external, {label!r}]
""",
        )
    project.write(
        "foundation.py",
        """import pytest
@pytest.fixture
def external():
    return ['foundation']
""",
    )
    trigger = {
        "none": "pass",
        "unregister": "config.pluginmanager.unregister(name='a_plugin')",
        "block": "config.pluginmanager.set_blocked('a_plugin')",
        "reregister": (
            "plugin=config.pluginmanager.unregister(name='a_plugin')\n"
            "    config.pluginmanager.register(plugin,'a_plugin')"
        ),
        "replace": (
            "config.pluginmanager.unregister(name='a_plugin')\n"
            "    import types\n"
            "    replacement=types.ModuleType('replacement')\n"
            "    def cache(cache): return [*cache,'replacement']\n"
            "    def external(external): return [*external,'replacement']\n"
            "    replacement.cache=pytest.fixture(cache)\n"
            "    replacement.external=pytest.fixture(external)\n"
            "    config.pluginmanager.register(replacement,'a_plugin')"
        ),
    }[action]
    project.write(
        "conftest.py",
        "import pytest\n@pytest.hookimpl(tryfirst=True)\ndef pytest_configure(config):\n    "
        + trigger
        + "\n",
    )
    project.write(
        "test_a.py",
        """import json
from pathlib import Path
def test_first(cache, external):
    Path('first.json').write_text(json.dumps({'cache':cache,'external':external}))
""",
    )
    project.write(
        "test_b.py",
        """import json
from pathlib import Path
def test_second(cache, external):
    Path('second.json').write_text(json.dumps({'cache':cache,'external':external}))
""",
    )
    result = project.run_engine(engine, "-q", "-p", "foundation", "-p", order[0], "-p", order[1])
    result.expect(0)
    labels = [name[0] for name in order]
    if action == "reregister":
        labels.append("a")
    elif action == "replace":
        labels.append("replacement")
    expected = {"cache": ["builtin", *labels], "external": ["foundation", *labels]}
    assert json.loads((tmp_path / "first.json").read_text()) == expected
    assert json.loads((tmp_path / "second.json").read_text()) == expected
