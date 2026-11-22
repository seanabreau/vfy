"""Hypothesis admission uses one owned plugin and the installed distribution."""

from __future__ import annotations

import json
from importlib.metadata import distributions, version
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_ALIASES = (
    "hypothesispytest",
    "_hypothesis_pytestplugin",
    "hypothesis.extra.pytestplugin",
)


def _other_plugins() -> list[str]:
    names = {
        entry.name
        for distribution in distributions()
        for entry in distribution.entry_points
        if entry.group == "pytest11" and entry.name not in (*_ALIASES, "pytest_cov")
    }
    return [argument for name in sorted(names) for argument in ("-p", f"no:{name}")]


def _project(root: Path) -> VfyProject:
    project = VfyProject(root)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "admission_observer.py",
        """import atexit, json, sys
from pathlib import Path
imports = []

def audit(event, arguments):
    if event == 'import' and arguments[0].split('.')[0] in (
        'pytest', '_pytest', '_hypothesis_pytestplugin'
    ):
        imports.append(arguments[0])

sys.addaudithook(audit)

def snapshot():
    Path('audit.json').write_text(json.dumps({
        'engine_imports': imports,
    }))

atexit.register(snapshot)
""",
    )
    project.write(
        "conftest.py",
        """import atexit, json, sys
from pathlib import Path
import pytest

def shutdown():
    Path('shutdown.json').write_text(json.dumps({
        'private_modules': sorted(name for name in sys.modules
                                  if name == '_pytest' or name.startswith('_pytest.')),
    }))

atexit.register(shutdown)

def pytest_configure(config):
    manager = config.pluginmanager
    plugin = manager.getplugin('hypothesispytest')
    distributions = [
        [distribution.metadata['Name'].lower(), distribution.version, plugin is entry_plugin]
        for entry_plugin, distribution in manager.list_plugin_distinfo()
        if distribution.metadata['Name'].lower() == 'hypothesis'
    ]
    Path('admission.json').write_text(json.dumps({
        'registered': plugin is not None,
        'names': [name for name, value in manager.list_name_plugin()
                  if plugin is not None and value is plugin],
        'aliases': [manager.getplugin(name) is plugin for name in
                    ('hypothesispytest', '_hypothesis_pytestplugin',
                     'hypothesis.extra.pytestplugin')],
        'distributions': distributions,
        'call_implementations': sum(implementation.plugin is plugin for implementation in
                                   manager.hook.pytest_runtest_call.get_hookimpls())
                                   if plugin is not None else 0,
    }))

def pytest_sessionfinish():
    private = sorted(name for name in sys.modules
                     if name == '_pytest' or name.startswith('_pytest.'))
    identities = {}
    if private:
        compat = sys.modules['_pytest.compat']
        fixtures = sys.modules['_pytest.fixtures']
        monkeypatch = sys.modules['_pytest.monkeypatch']
        identities = {
            'canonical': compat.NOTSET is fixtures.NOTSET is monkeypatch.NOTSET,
            'enum': compat.NotSetType is fixtures.NotSetType is monkeypatch.NotSetType,
            'fixture_class': monkeypatch.MonkeyPatch is pytest.MonkeyPatch,
        }
    Path('imports.json').write_text(json.dumps({
        'private_identities': identities,
        'upstream_plugin': '_hypothesis_pytestplugin' in sys.modules,
        'private': private,
    }))
""",
    )
    project.write(
        "test_one.py",
        """from pathlib import Path
def test_one(): Path('ran').write_text('yes')
""",
    )
    return project


def _run(project: VfyProject, arguments: list[str], *, stock: bool, autoload: bool = False) -> None:
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None} if autoload else None
    arguments = ["-p", "admission_observer", "-p", "pytest_cov", *arguments]
    result = project.run_engine(
        "stock" if stock else "independent", *arguments, stock=("-q",), env=environment
    )
    result.expect(0)
    assert (project.root / "ran").is_file()


def _assert_owned(project: VfyProject) -> None:
    audit = json.loads((project.root / "audit.json").read_text())
    assert audit == {"engine_imports": []}
    imports = json.loads((project.root / "imports.json").read_text())
    assert imports["upstream_plugin"] is False
    private = imports["private"]
    # Builtin subtests and exception providers own these aliases without Hypothesis.
    expected = {
        "_pytest",
        "_pytest._code",
        "_pytest._code.code",
        "_pytest.config",
        "_pytest.config.argparsing",
        "_pytest.outcomes",
        "_pytest.fixtures",
        "_pytest.compat",
        "_pytest.monkeypatch",
        "_pytest.nodes",
        "_pytest.python",
        "_pytest.doctest",
        "_pytest.terminal",
        "_pytest.scope",
        "_pytest.cacheprovider",
        "_pytest.subtests",
        "_pytest.unraisableexception",
        "_pytest.threadexception",
        "_pytest.faulthandler",
        "_pytest.warning_types",
        "_pytest._io",
        "_pytest.assertion",
        "_pytest.assertion.rewrite",
        "_pytest.logging",
    }
    assert set(private) == expected
    assert imports["private_identities"] == {
        "canonical": True,
        "enum": True,
        "fixture_class": True,
    }
    shutdown = json.loads((project.root / "shutdown.json").read_text())
    assert shutdown["private_modules"] == []


def _assert_admission(project: VfyProject, *, enabled: bool, owned: bool) -> None:
    state = json.loads((project.root / "admission.json").read_text())
    assert state["registered"] is enabled
    assert state["names"] == (["hypothesispytest"] if enabled else [])
    assert state["call_implementations"] == int(enabled)
    expected = [["hypothesis", version("hypothesis"), True]] if enabled else []
    assert state["distributions"] == expected
    if owned and enabled:
        assert state["aliases"] == [True, True, True]


@pytest.mark.parametrize("alias", _ALIASES)
def test_hypothesis_alias_admits_owned_plugin_with_real_distribution(
    tmp_path: Path, alias: str
) -> None:
    stock = _project(tmp_path / "stock")
    actual = _project(tmp_path / "actual")
    _run(stock, ["-p", "hypothesispytest"], stock=True)
    _run(actual, ["-p", alias], stock=False)
    _assert_admission(stock, enabled=True, owned=False)
    _assert_admission(actual, enabled=True, owned=True)
    _assert_owned(actual)


def test_repeated_hypothesis_aliases_share_one_plugin_and_distribution(tmp_path: Path) -> None:
    stock = _project(tmp_path / "stock")
    actual = _project(tmp_path / "actual")
    _run(stock, [argument for _ in _ALIASES for argument in ("-p", "hypothesispytest")], stock=True)
    _run(actual, [argument for alias in _ALIASES for argument in ("-p", alias)], stock=False)
    _assert_admission(stock, enabled=True, owned=False)
    _assert_admission(actual, enabled=True, owned=True)
    _assert_owned(actual)


@pytest.mark.parametrize("alias", _ALIASES)
def test_hypothesis_alias_block_prevents_upstream_entry_point_autoload(
    tmp_path: Path, alias: str
) -> None:
    stock = _project(tmp_path / "stock")
    actual = _project(tmp_path / "actual")
    _run(stock, [*_other_plugins(), "-p", "no:hypothesispytest"], stock=True, autoload=True)
    _run(actual, [*_other_plugins(), "-p", f"no:{alias}"], stock=False, autoload=True)
    _assert_admission(stock, enabled=False, owned=False)
    _assert_admission(actual, enabled=False, owned=True)
    _assert_owned(actual)


def test_hypothesis_upstream_entry_point_autoload_retains_distribution(tmp_path: Path) -> None:
    stock = _project(tmp_path / "stock")
    actual = _project(tmp_path / "actual")
    _run(stock, _other_plugins(), stock=True, autoload=True)
    _run(actual, _other_plugins(), stock=False, autoload=True)
    _assert_admission(stock, enabled=True, owned=False)
    _assert_admission(actual, enabled=True, owned=True)
    _assert_owned(actual)


def test_hypothesis_actual_distribution_version_controls_admission_before_mutation(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "observer.py",
        """import atexit, json, sys
from importlib.metadata import version
from pathlib import Path
imports = []

def audit(event, arguments):
    if event == 'import' and arguments[0].split('.')[0] in (
        'pytest', '_pytest', '_hypothesis_pytestplugin'
    ):
        imports.append(arguments[0])

sys.addaudithook(audit)

def snapshot():
    Path('version.json').write_text(json.dumps({
        'version': version('hypothesis'),
        'hypothesis': 'hypothesis' in sys.modules,
        'globals': '_hypothesis_globals' in sys.modules,
        'upstream_plugin': '_hypothesis_pytestplugin' in sys.modules,
        'private': sorted(name for name in sys.modules
                          if name == '_pytest' or name.startswith('_pytest.')),
        'engine_imports': imports,
    }))

atexit.register(snapshot)
""",
    )
    project.write("test_one.py", "def test_one(): pass\n")
    result = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-p", "observer", "-p", "hypothesispytest"
    )
    current = version("hypothesis")
    major, minor, patch = (int(part) for part in current.split("."))
    supported = major == 6 and (minor, patch) >= (152, 1)
    assert result.returncode == (0 if supported else 4), result.stdout + result.stderr
    state = json.loads((tmp_path / "version.json").read_text())
    assert state["version"] == current
    assert state["private"] == []
    assert state["upstream_plugin"] is False
    assert state["engine_imports"] == []
    if not supported:
        assert state["hypothesis"] is state["globals"] is False
        assert (
            f"requires a 6.x release from 6.152.1; found {current}" in result.stdout + result.stderr
        )
