"""Independent registration rejects unavailable builtin hooks before publication."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

Engine = Literal["stock", "independent"]


@requires_native_pytest
def test_independent_explicit_plugin_can_inspect_installed_metadata(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "local_plugin.py",
        "import importlib.metadata\n"
        "import pytest\n"
        "@pytest.fixture\n"
        "def installed_pytest_version():\n"
        "    return importlib.metadata.version('pytest')\n",
    )
    project.write(
        "test_example.py",
        "from pathlib import Path\n"
        "def test_example(installed_pytest_version):\n"
        "    Path('observed-version').write_text(installed_pytest_version)\n",
    )
    reference = project.run_pytest("-q", "-p", "local_plugin", load_xdist=False)
    reference.expect(0)
    expected = (tmp_path / "observed-version").read_text()
    assert expected
    (tmp_path / "observed-version").unlink()
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q", "-p", "local_plugin")
    result.expect(0)
    assert "1 passed" in result.stdout
    assert (tmp_path / "observed-version").read_text() == expected


def _run(project: VfyProject, engine: Engine) -> tuple[int, str]:
    result = project.run_engine(engine, stock=("-q",))
    return result.returncode, result.stdout + result.stderr


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("stage", ["configure", "sessionstart"])
@pytest.mark.parametrize("optional", [False, True])
@pytest.mark.parametrize("spelling", ["direct", "specname"])
def test_late_unavailable_builtin_hook_is_rejected_before_registration(
    tmp_path: Path, engine: Engine, stage: str, optional: bool, spelling: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    attribute = "pytest_collect_directory" if spelling == "direct" else "pytest_alternate_collector"
    project.write(
        "conftest.py",
        f"""import json
from pathlib import Path
import pytest

def event(value):
    with Path('events').open('a') as output: output.write(value + '\\n')

class Late:
    @pytest.hookimpl(optionalhook={optional}, specname='pytest_collect_directory')
    def {attribute}(self, path, parent):
        event('late collect')
    def pytest_configure(self, config): event('late configure')
    def pytest_unconfigure(self, config): event('late unconfigure')

late = Late()

def register(config):
    event('registration requested')
    config.pluginmanager.register(late, 'late')
    event('registration returned')

def pytest_configure(config):
    config.add_cleanup(lambda: event('cleanup'))
    event('root configure')
    if {stage!r} == 'configure': register(config)

def pytest_sessionstart(session):
    event('root sessionstart')
    if {stage!r} == 'sessionstart': register(session.config)

def pytest_unconfigure(config):
    event('root unconfigure')
    Path('registration.json').write_text(json.dumps({{
        'registered': config.pluginmanager.hasplugin('late'),
        'hookcallers': None if config.pluginmanager.get_hookcallers(late) is None else [
            hook.name for hook in config.pluginmanager.get_hookcallers(late)
        ],
    }}))
""",
    )
    project.write(
        "test_one.py",
        "from pathlib import Path\ndef test_one(): Path('ran').write_text('yes')\n",
    )
    code, output = _run(project, engine)
    events = (tmp_path / "events").read_text().splitlines()
    state = json.loads((tmp_path / "registration.json").read_text())
    if engine == "stock":
        assert code == 0, output
        assert (tmp_path / "ran").is_file()
        assert state["registered"] is True
        assert "late configure" in events
        assert "late collect" in events
    else:
        assert code == 4, output
        assert "does not support hook 'pytest_collect_directory'" in output
        assert not (tmp_path / "ran").exists()
        assert state == {"registered": False, "hookcallers": None}
        assert "late configure" not in events
        assert "late unconfigure" not in events
        assert "registration returned" not in events
    assert events[-2:] == ["root unconfigure", "cleanup"]


@requires_native_pytest
@pytest.mark.parametrize("stage", ["configure", "sessionstart"])
@pytest.mark.parametrize("specification", ["before", "after"])
def test_late_custom_hooks_and_normal_lifecycle_order_match_stock(
    tmp_path: Path, stage: str, specification: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        f"""from pathlib import Path
import pytest

def event(value):
    with Path('events').open('a') as output: output.write(value + '\\n')

class Spec:
    @pytest.hookspec
    def pytest_project_value(value): pass

class Late:
    def pytest_configure(self, config): event('late configure')
    def pytest_sessionstart(self, session): event('late sessionstart')
    def pytest_unconfigure(self, config): event('late unconfigure')
    def pytest_project_value(self, value):
        event('custom ' + value)
        return 'reply ' + value

def register(config):
    manager = config.pluginmanager
    if {specification!r} == 'before': manager.add_hookspecs(Spec)
    assert manager.register(Late(), 'late') == 'late'
    if {specification!r} == 'after': manager.add_hookspecs(Spec)
    assert manager.hook.pytest_project_value(value='v') == ['reply v']

def pytest_configure(config):
    config.add_cleanup(lambda: event('cleanup'))
    event('root configure')
    if {stage!r} == 'configure': register(config)

def pytest_sessionstart(session):
    event('root sessionstart')
    if {stage!r} == 'sessionstart': register(session.config)

def pytest_unconfigure(config): event('root unconfigure')
""",
    )
    project.write(
        "test_one.py",
        "from pathlib import Path\ndef test_one():\n"
        "    with Path('events').open('a') as output: output.write('test\\n')\n",
    )
    code, output = _run(project, "stock")
    assert code == 0, output
    oracle = (tmp_path / "events").read_text().splitlines()
    (tmp_path / "events").unlink()
    code, output = _run(project, "independent")
    assert code == 0, output
    assert (tmp_path / "events").read_text().splitlines() == oracle
    assert "custom v" in oracle
    assert ("late sessionstart" in oracle) == (stage == "configure")


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("case", ["blocked", "duplicate_name", "duplicate_object", "signature"])
def test_registration_error_precedence_preserves_original_failure(
    tmp_path: Path, engine: Engine, case: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        f"""import json
from pathlib import Path

class Candidate:
    reject_reflection = True
    def __dir__(self):
        if self.reject_reflection:
            Path('reflected').write_text('yes')
            raise RuntimeError('duplicate/blocked candidate inspected')
        return []
    def pytest_collect_directory(self, path, parent): pass

class InvalidSignature:
    def pytest_addoption(self, missing_argument): pass
    def pytest_collect_directory(self, path, parent): pass

def pytest_configure(config):
    manager = config.pluginmanager
    candidate = Candidate()
    original = None
    name = 'candidate'
    case = {case!r}
    if case == 'blocked':
        manager.set_blocked(name)
    elif case == 'duplicate_name':
        original = object()
        manager.register(original, name)
    elif case == 'duplicate_object':
        candidate.reject_reflection = False
        manager.register(candidate, 'original')
        candidate.reject_reflection = True
    else:
        candidate = InvalidSignature()
    record = {{}}
    try:
        record['result'] = manager.register(candidate, name)
        record['error'] = None
    except Exception as error:
        record['error'] = type(error).__name__
        record['message'] = str(error)
        record['original_plugin'] = getattr(error, 'plugin', None) is candidate
    record['registered'] = manager.is_registered(candidate)
    record['reflected'] = Path('reflected').exists()
    record['original_preserved'] = original is None or manager.get_plugin(name) is original
    if isinstance(candidate, Candidate): candidate.reject_reflection = False
    Path('precedence.json').write_text(json.dumps(record))
""",
    )
    project.write("test_one.py", "def test_one(): pass\n")
    code, output = _run(project, engine)
    assert code == 0, output
    record = json.loads((tmp_path / "precedence.json").read_text())
    assert record["reflected"] is False
    assert record["original_preserved"] is True
    if case == "blocked":
        assert record["error"] is None
        assert record["result"] is None
        assert record["registered"] is False
    elif case.startswith("duplicate"):
        assert record["error"] == "ValueError"
        assert record["registered"] is (case == "duplicate_object")
    else:
        assert record["error"] == "PluginValidationError"
        assert record["original_plugin"] is True
        assert "missing_argument" in record["message"]
        assert record["registered"] is (engine == "stock")


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("failure", ["reflection", "historic"])
def test_original_registration_error_and_retry_preserve_lifecycle_state(
    tmp_path: Path, engine: Engine, failure: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        f"""import json
from pathlib import Path
marker = RuntimeError('original registration error')

def event(value):
    with Path('events').open('a') as output: output.write(value + '\\n')

class Retried:
    fail = True
    def __dir__(self):
        if self.fail and {failure!r} == 'reflection': raise marker
        return ['pytest_configure', 'pytest_unconfigure']
    def pytest_configure(self, config):
        if self.fail and {failure!r} == 'historic': raise marker
        event('late configure')
    def pytest_unconfigure(self, config): event('late unconfigure')

def pytest_configure(config):
    config.add_cleanup(lambda: event('cleanup'))
    manager = config.pluginmanager
    candidate = Retried()
    try:
        manager.register(candidate, 'late')
    except RuntimeError as error:
        assert error is marker
        published = manager.is_registered(candidate)
    else:
        raise AssertionError('expected original registration error')
    if published: manager.unregister(candidate)
    candidate.fail = False
    assert manager.register(candidate, 'late') == 'late'
    Path('retry.json').write_text(json.dumps({{'published_after_error': published}}))

def pytest_unconfigure(config): event('root unconfigure')
""",
    )
    project.write("test_one.py", "def test_one(): pass\n")
    code, output = _run(project, engine)
    assert code == 0, output
    record = json.loads((tmp_path / "retry.json").read_text())
    assert record["published_after_error"] is (engine == "stock" or failure == "historic")
    assert (tmp_path / "events").read_text().splitlines() == [
        "late configure",
        "late unconfigure",
        "root unconfigure",
        "cleanup",
    ]


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("valid", [False, True])
def test_historic_addhooks_validates_new_custom_spec_before_implementation_publication(
    tmp_path: Path, engine: Engine, valid: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    argument = "value" if valid else "missing_argument"
    project.write(
        "conftest.py",
        f"""import json
from pathlib import Path
import pytest

class Spec:
    @pytest.hookspec
    def pytest_project_value(value): pass

class Candidate:
    def pytest_addhooks(self, pluginmanager):
        Path('addhooks').write_text('yes')
        pluginmanager.add_hookspecs(Spec)
    def pytest_project_value(self, {argument}): return {argument}

def pytest_configure(config):
    manager = config.pluginmanager
    candidate = Candidate()
    record = {{}}
    try:
        record['result'] = manager.register(candidate, 'candidate')
    except Exception as error:
        record['error'] = type(error).__name__
        record['original_plugin'] = getattr(error, 'plugin', None) is candidate
    else:
        record['called'] = manager.hook.pytest_project_value(value='answer')
    record['registered'] = manager.is_registered(candidate)
    record['implementations'] = len(manager.hook.pytest_project_value.get_hookimpls())
    Path('custom.json').write_text(json.dumps(record))
""",
    )
    project.write("test_one.py", "def test_one(): pass\n")
    code, output = _run(project, engine)
    assert code == 0, output
    assert (tmp_path / "addhooks").is_file()
    record = json.loads((tmp_path / "custom.json").read_text())
    assert record == (
        {
            "result": "candidate",
            "called": ["answer"],
            "registered": True,
            "implementations": 1,
        }
        if valid
        else {
            "error": "PluginValidationError",
            "original_plugin": True,
            "registered": True,
            "implementations": 0,
        }
    )
