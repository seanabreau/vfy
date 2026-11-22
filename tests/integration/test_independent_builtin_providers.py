"""Builtin services use actual plugin declarations and ordinary fixture hooks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import ENGINES, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@ENGINES
def test_warning_scopes_follow_registration_when_hasplugin_is_replaced(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

manager = None
saved = None
warnings = []

def pytest_configure(config):
    global manager
    manager = config.pluginmanager
    original = manager.hasplugin
    manager.hasplugin = lambda name: False if name == 'warnings' else original(name)

def pytest_warning_recorded(warning_message, nodeid):
    message = str(warning_message.message)
    if message.startswith('registration:'):
        warnings.append([nodeid.rsplit('::', 1)[-1], message])

def pytest_runtest_logreport(report):
    global saved
    if report.when != 'teardown':
        return
    name = report.nodeid.rsplit('::', 1)[-1]
    if name == 'test_registered':
        saved = manager.unregister(name='warnings')
        assert saved is not None
    elif name == 'test_unregistered':
        assert manager.get_plugin('warnings') is None
        manager.register(saved, 'warnings')

def pytest_sessionfinish():
    Path('warnings.json').write_text(json.dumps(warnings))
""",
    )
    project.write(
        "test_registration.py",
        """import warnings

def check(request, present, message):
    manager = request.config.pluginmanager
    assert manager.hasplugin('warnings') is False
    assert (manager.get_plugin('warnings') is not None) is present
    warnings.warn('registration:' + message, UserWarning)

def test_registered(request):
    check(request, True, 'registered')

def test_unregistered(request):
    check(request, False, 'unregistered')

def test_restored(request):
    check(request, True, 'restored')
""",
    )
    result = project.run_engine(engine, "-q")
    result.expect(0)
    assert json.loads((tmp_path / "warnings.json").read_text()) == [
        ["test_registered", "registration:registered"],
        ["test_restored", "registration:restored"],
    ]


@ENGINES
def test_disabled_capture_does_not_follow_a_replaced_hasplugin_answer(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """def pytest_configure(config):
    manager = config.pluginmanager
    original = manager.hasplugin
    manager.hasplugin = lambda name: True if name == 'capture' else original(name)

def pytest_runtest_logreport(report):
    assert report.capstdout == report.capstderr == ''
""",
    )
    project.write(
        "test_capture.py",
        """def test_capture(request):
    manager = request.config.pluginmanager
    assert manager.hasplugin('capture') is True
    assert manager.get_plugin('capture') is None
    print('uncaptured body output')
""",
    )
    result = project.run_engine(engine, "-q", "-p", "no:capture")
    result.expect(0)
    assert "uncaptured body output" in result.stdout


@ENGINES
def test_builtin_declarations_expose_real_fixture_requests_and_cache_to_hooks(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
names={'pytestconfig','tmp_path_factory','tmp_path','tmpdir_factory','tmpdir',
       'monkeypatch','recwarn','caplog','record_testsuite_property'}
registered={}
setups={}
finalized={}
def pytest_plugin_registered(plugin, plugin_name):
    if plugin_name in {'fixtures','tmpdir','legacypath-tmpdir','monkeypatch',
                       'recwarn','logging','junitxml'}:
        registered[plugin_name]=sorted(name for name in names if hasattr(plugin,name))
def pytest_fixture_setup(fixturedef, request):
    if fixturedef.argname in names:
        assert request._get_fixturestack()[-1] is fixturedef
        setups[fixturedef.argname]=[fixturedef.scope,request.fixturename,
                                   fixturedef.func.__name__]
def pytest_fixture_post_finalizer(fixturedef, request):
    if fixturedef.argname in names and fixturedef.cached_result is not None:
        finalized[fixturedef.argname]=request.fixturename

def pytest_sessionfinish():
    Path('hooks.json').write_text(json.dumps([registered,setups,finalized]))
""",
    )
    project.write(
        "test_services.py",
        """import logging, os, warnings
from pathlib import Path
def test_resources(pytestconfig,tmp_path_factory,tmp_path,tmpdir_factory,tmpdir,
                   monkeypatch,recwarn,caplog,record_testsuite_property):
    assert pytestconfig._tmp_path_factory is tmp_path_factory
    assert tmp_path.parent == tmp_path_factory.getbasetemp()
    assert Path(str(tmpdir)) == tmp_path
    assert Path(str(tmpdir_factory.getbasetemp())) == tmp_path_factory.getbasetemp()
    monkeypatch.setenv('ORDINARY_PROVIDER_WITNESS','set')
    assert os.environ['ORDINARY_PROVIDER_WITNESS'] == 'set'
    warnings.warn('ordinary recorder',UserWarning)
    assert str(recwarn.pop(UserWarning).message) == 'ordinary recorder'
    logging.warning('ordinary log resource')
    assert caplog.messages == ['ordinary log resource']
    record_testsuite_property('provider','ordinary')
    Path('body-ran').touch()
""",
    )
    result = project.run_engine(engine, "-q")
    result.expect(0)
    assert (tmp_path / "body-ran").is_file()
    registered, setups, finalized = json.loads((tmp_path / "hooks.json").read_text())
    assert registered == {
        "fixtures": ["pytestconfig"],
        "tmpdir": ["tmp_path", "tmp_path_factory"],
        "legacypath-tmpdir": ["tmpdir", "tmpdir_factory"],
        "monkeypatch": ["monkeypatch"],
        "recwarn": ["recwarn"],
        "logging": ["caplog"],
        "junitxml": ["record_testsuite_property"],
    }
    scopes = {
        "pytestconfig": "session",
        "tmp_path_factory": "session",
        "tmpdir_factory": "session",
        "record_testsuite_property": "session",
        "tmp_path": "function",
        "tmpdir": "function",
        "monkeypatch": "function",
        "recwarn": "function",
        "caplog": "function",
    }
    assert setups == {name: [scope, name, name] for name, scope in scopes.items()}
    assert finalized == {name: name for name in scopes}


@ENGINES
@pytest.mark.parametrize("acquire_in_configure", [False, True])
def test_configured_temp_owner_is_lazy_and_preserves_configure_created_files(
    tmp_path: Path, engine: str, acquire_in_configure: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("base/old", "prior session")
    project.write(
        "conftest.py",
        f"""import json, pytest
from pathlib import Path
@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    assert hasattr(config,'_tmp_path_factory')
    assert hasattr(config,'_tmpdirhandler')
    observations=[Path('base/old').read_text()]
    if {acquire_in_configure!r}:
        base=config._tmp_path_factory.getbasetemp()
        assert not (base/'old').exists()
        (base/'configure-created').write_text('preserved')
    Path('configure.json').write_text(json.dumps(observations))
""",
    )
    project.write(
        "test_temp.py",
        f"""from pathlib import Path
def test_same_owner(tmp_path_factory,tmpdir_factory,pytestconfig):
    assert tmp_path_factory is pytestconfig._tmp_path_factory
    base=tmp_path_factory.getbasetemp()
    assert base == Path('base').resolve() and base.is_dir()
    assert Path(str(tmpdir_factory.getbasetemp())) == base
    assert not (base/'old').exists()
    assert (base/'configure-created').exists() is {acquire_in_configure!r}
    if {acquire_in_configure!r}:
        assert (base/'configure-created').read_text() == 'preserved'
    Path('body-ran').touch()
""",
    )
    result = project.run_engine(engine, "-q", "--basetemp=base")
    result.expect(0)
    assert (tmp_path / "body-ran").is_file()
    assert json.loads((tmp_path / "configure.json").read_text()) == ["prior session"]


@ENGINES
def test_collect_only_does_not_acquire_or_clear_the_configured_temp_resource(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("base/old", "preserve collection bytes")
    project.write(
        "test_temp.py",
        "def test_temp(tmp_path):\n    raise AssertionError('collection cannot run the body')\n",
    )
    result = project.run_engine(engine, "-q", "--collect-only", "--basetemp=base")
    result.expect(0)
    assert "test_temp.py::test_temp" in result.stdout
    assert (tmp_path / "base/old").read_text() == "preserve collection bytes"


@ENGINES
def test_config_cleanup_restores_configured_factory_attributes(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
import pytest
from pathlib import Path

factory = None

@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    # Registered first, so this cleanup runs after every other configuration cleanup.
    def observe():
        Path('cleanup.json').write_text(json.dumps([
            hasattr(config, '_tmp_path_factory'),
            hasattr(config, '_tmpdirhandler'),
            str(factory.getbasetemp().relative_to(Path.cwd())),
        ]))
    config.add_cleanup(observe)

def pytest_sessionfinish(session):
    global factory
    factory = session.config._tmp_path_factory
""",
    )
    project.write(
        "test_temp.py", "def test_temp(tmp_path):\n    tmp_path.joinpath('body').touch()\n"
    )
    result = project.run_engine(engine, "-q", "--basetemp=base")
    result.expect(0)
    assert json.loads((tmp_path / "cleanup.json").read_text()) == [False, False, "base"]
    assert list((tmp_path / "base").glob("*/body"))


@ENGINES
def test_legacy_factory_bypasses_overrides_and_tmp_path_uses_actual_dependency(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest
from pathlib import Path
@pytest.fixture(scope='session')
def tmp_path_factory(tmp_path_factory):
    class Factory:
        def __getattr__(self,name): return getattr(tmp_path_factory,name)
        def getbasetemp(self): return tmp_path_factory.getbasetemp()
        def mktemp(self,basename,numbered=True):
            with Path('factory-calls').open('a') as stream: stream.write(basename+'\\n')
            return tmp_path_factory.mktemp('wrapped_'+basename,numbered=numbered)
    return Factory()
""",
    )
    project.write(
        "test_temp.py",
        """from pathlib import Path
def test_dependency(tmpdir_factory,tmp_path,tmp_path_factory,pytestconfig):
    base=pytestconfig._tmp_path_factory.getbasetemp()
    legacy=Path(str(tmpdir_factory.mktemp('legacy')))
    assert legacy.parent == base and legacy.name.startswith('legacy')
    assert tmp_path.parent == base and tmp_path.name.startswith('wrapped_test_dependency')
    assert Path('factory-calls').read_text().splitlines() == ['test_dependency']
    Path('body-ran').touch()
""",
    )
    result = project.run_engine(engine, "-q")
    result.expect(0)
    assert (tmp_path / "body-ran").is_file()


@ENGINES
@pytest.mark.parametrize(
    ("plugin", "fixture"),
    [
        ("tmpdir", "tmp_path_factory"),
        ("legacypath", "tmpdir_factory"),
        ("monkeypatch", "monkeypatch"),
        ("recwarn", "recwarn"),
        ("logging", "caplog"),
    ],
)
@pytest.mark.parametrize("history", ["blocked", "reenabled", "unregistered", "late_blocked"])
def test_provider_availability_uses_actual_plugin_registration_history(
    tmp_path: Path, engine: str, plugin: str, fixture: str, history: str
) -> None:
    project = VfyProject(tmp_path)
    if history in {"unregistered", "late_blocked"}:
        operation = (
            f"config.pluginmanager.unregister(name={plugin!r})"
            if history == "unregistered"
            else f"config.pluginmanager.set_blocked({plugin!r})"
        )
        project.write(
            "conftest.py",
            "import pytest\n@pytest.hookimpl(tryfirst=True)\ndef pytest_configure(config):\n    "
            + operation
            + "\n",
        )
    project.write(
        "test_history.py",
        f"""import pytest
from pathlib import Path
def test_history(request):
    if {history == "blocked"!r}:
        with pytest.raises(pytest.FixtureLookupError):
            request.getfixturevalue({fixture!r})
    elif {plugin == "legacypath" and history == "reenabled"!r}:
        # Reenabling changes configure order; the legacy handler is not created.
        with pytest.raises(AttributeError,match='_tmpdirhandler'):
            request.getfixturevalue({fixture!r})
    else:
        value=request.getfixturevalue({fixture!r})
        assert value is request.getfixturevalue({fixture!r})
    Path('body-ran').touch()
""",
    )
    arguments = ("-p", "no:" + plugin) if history in {"blocked", "reenabled"} else ()
    if history == "reenabled":
        arguments += ("-p", plugin)
    result = project.run_engine(engine, "-q", *arguments)
    result.expect(0)
    assert (tmp_path / "body-ran").is_file()


@ENGINES
def test_tmp_path_keeps_a_none_factory_override_as_the_actual_dependency(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_factory.py",
        """import pytest
from pathlib import Path
@pytest.fixture(scope='session')
def tmp_path_factory(): return None
def test_dependency(request):
    with pytest.raises(AttributeError,match='mktemp'):
        request.getfixturevalue('tmp_path')
    Path('body-ran').touch()
""",
    )
    result = project.run_engine(engine, "-q")
    result.expect(0)
    assert (tmp_path / "body-ran").is_file()


@ENGINES
def test_unused_configured_factory_keeps_its_lazy_post_session_lifetime(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("base/old", "first acquisition must clear this")
    project.write(
        "conftest.py",
        """import json
import pytest
from pathlib import Path

factory = None

@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    def observe():
        assert Path('base/old').read_text() == 'first acquisition must clear this'
        base = factory.getbasetemp()
        assert base == Path('base').resolve() and not (base / 'old').exists()
        after = factory.mktemp('after-session', numbered=False)
        assert after == base / 'after-session' and after.is_dir()
        Path('after-session.json').write_text(json.dumps([base.name, after.name]))
    config.add_cleanup(observe)

def pytest_sessionfinish(session):
    global factory
    factory = session.config._tmp_path_factory
""",
    )
    project.write(
        "test_body.py", "from pathlib import Path\ndef test_body(): Path('body-ran').touch()\n"
    )
    result = project.run_engine(engine, "-q", "--basetemp=base")
    result.expect(0)
    assert (tmp_path / "body-ran").is_file()
    assert json.loads((tmp_path / "after-session.json").read_text()) == ["base", "after-session"]
