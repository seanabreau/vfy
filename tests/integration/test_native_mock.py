"""Upstream pytest-mock fixtures run through the native pytest lifecycle."""

from __future__ import annotations

import json
import os
import sys
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="native pytest-mock tests require Linux or macOS"
)


@pytest.mark.parametrize(
    "loading", ["pytest_mock", "pytest_mock.plugin", "conftest", "environment", "configured"]
)
def test_mock_patch_spy_reset_and_cleanup_match_upstream(tmp_path: Path, loading: str) -> None:
    source = """import json
from pathlib import Path
import pytest
import app

def test_patch(mocker):
    mocked = mocker.patch('app.multiply', autospec=True, return_value=12)
    assert app.multiply(2, 3) == 12
    mocked.assert_called_once_with(2, 3)
    with pytest.raises(TypeError):
        app.multiply()
    mocker.resetall()
    mocked.assert_not_called()
    mocker.stop(mocked)
    assert app.multiply(2, 3) == 6
    mocker.patch.dict(app.values, {'added': 2})
    mocker.patch.object(app, 'VALUE', 7)

def test_spy_and_stub(mocker):
    assert app.VALUE == 1 and app.values == {'original': 1}
    spy = mocker.spy(app, 'multiply')
    assert app.multiply(4, 5) == 20
    spy.assert_called_once_with(4, 5)
    assert spy.spy_return == 20 and spy.spy_return_list == [20]
    stub = mocker.stub(name='callback')
    stub(42)
    stub.assert_called_once_with(42)
    mocker.stopall()
    assert app.multiply(3, 4) == 12
    Path('trace.json').write_text(json.dumps([app.VALUE, app.values, app.multiply(3, 4)]))
"""
    traces = []
    for engine in ("upstream", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "app.py", "VALUE = 1\nvalues = {'original': 1}\ndef multiply(a, b):\n    return a * b\n"
        )
        project.write("test_app.py", source)
        arguments = ["-p", loading] if loading in {"pytest_mock", "pytest_mock.plugin"} else []
        environment = {"PYTEST_PLUGINS": "pytest_mock"} if loading == "environment" else {}
        if loading == "conftest":
            project.write("conftest.py", "pytest_plugins = ['pytest_mock']\n")
        if loading == "configured":
            project.write("pytest.ini", "[pytest]\naddopts = -p pytest_mock\n")
        completed = (
            project.run_pytest(*arguments, "-q", load_xdist=False, env=environment)
            if engine == "upstream"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=1",
                "--no-cov",
                *arguments,
                "-q",
                env=environment,
            )
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "2 passed" in completed.stdout
        traces.append(json.loads((project.root / "trace.json").read_text()))
    assert traces[0] == traces[1] == [1, {"original": 1}, 12]


@pytest.mark.parametrize("workers", [1, 2])
def test_scoped_mockers_restore_patches_at_owner_teardown(tmp_path: Path, workers: int) -> None:
    conftest = """import pytest
import app
@pytest.fixture(scope='session')
def check_cleanup():
    yield
    assert [app.session, app.package, app.module, app.cls, app.function] == [0] * 5
@pytest.fixture(scope='session')
def session_mocker(check_cleanup, session_mocker):
    return session_mocker
@pytest.fixture(scope='session')
def session_patch(session_mocker):
    session_mocker.patch.object(app, 'session', 1)
@pytest.fixture(scope='package')
def package_patch(package_mocker):
    package_mocker.patch.object(app, 'package', 2)
@pytest.fixture(scope='module')
def module_patch(module_mocker):
    module_mocker.patch.object(app, 'module', 3)
@pytest.fixture(scope='class')
def class_patch(class_mocker):
    class_mocker.patch.object(app, 'cls', 4)
"""
    source = """import pytest
import app
@pytest.mark.usefixtures('session_patch', 'package_patch', 'module_patch', 'class_patch')
class TestScoped:
    def test_first(self, mocker):
        assert [app.session, app.package, app.module, app.cls, app.function] == [1, 2, 3, 4, 0]
        mocker.patch.object(app, 'function', 5)
    def test_second(self):
        assert [app.session, app.package, app.module, app.cls, app.function] == [1, 2, 3, 4, 0]
def test_after_class():
    assert app.cls == app.function == 0
"""
    for engine in ("upstream", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("app.py", "session = package = module = cls = function = 0\n")
        project.write("conftest.py", conftest)
        for package in ("a", "b"):
            project.write(f"{package}/__init__.py", "")
            project.write(
                f"{package}/conftest.py", "from pytest_mock.plugin import package_mocker\n"
            )
            project.write(f"{package}/test_scope.py", source)
        project.write(
            "test_z_after.py",
            "import app\ndef test_after_packages():\n"
            "    assert app.package == app.module == app.cls == app.function == 0\n",
        )
        completed = (
            project.run_pytest("-p", "pytest_mock", "-q", load_xdist=False)
            if engine == "upstream"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                f"--workers={workers}",
                "--no-cov",
                "-p",
                "pytest_mock",
                "-q",
            )
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "7 passed" in completed.stdout


@pytest.mark.parametrize("introspection", [True, False])
@pytest.mark.parametrize("early_stop", [True, False])
def test_failure_cleanup_and_assertion_introspection_match_upstream(
    tmp_path: Path, introspection: bool, early_stop: bool
) -> None:
    source = """import app
import pytest
from pathlib import Path
@pytest.fixture(scope='session', autouse=True)
def check_cleanup():
    yield
    assert app.value == 'original'
    Path('cleaned.txt').write_text('clean')
def test_failed_assertion(mocker):
    mocked = mocker.patch.object(app, 'value')
    mocked(1, named='actual')
    mocked.assert_called_once_with(2, named='expected')
def test_clean_after_failure():
    assert app.value == 'original'
"""
    diagnostics = []
    for engine in ("upstream", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("app.py", "value = 'original'\n")
        project.write("test_app.py", source)
        project.write("pytest.ini", f"[pytest]\nmock_traceback_monkeypatch={introspection}\n")
        arguments = ["--maxfail=1"] if early_stop else []
        completed = (
            project.run_pytest("-p", "pytest_mock", "-q", *arguments, load_xdist=False)
            if engine == "upstream"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=1",
                "--no-cov",
                "-p",
                "pytest_mock",
                "-q",
                *arguments,
            )
        )
        assert completed.returncode == 1, completed.stdout + completed.stderr
        assert "1 failed" in completed.stdout
        if not early_stop:
            assert "1 failed, 1 passed" in completed.stdout
        assert (project.root / "cleaned.txt").read_text() == "clean"
        assert ("pytest introspection follows" in completed.stdout) == introspection
        if introspection:
            assert "Args:" in completed.stdout
            assert "Kwargs:" in completed.stdout
        diagnostics.append(
            [line for line in completed.stdout.splitlines() if line.startswith("E ")]
        )
    assert diagnostics[0] == diagnostics[1]


def test_entrypoint_autoload_preserves_mock_fixture_registration(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_app.py", "def test_mocker(mocker):\n    assert mocker.Mock(return_value=5)() == 5\n"
    )
    blocked = [
        argument
        for plugin in entry_points(group="pytest11")
        if plugin.name not in {"pytest_mock", "vfy"}
        for argument in ("-p", f"no:{plugin.name}")
    ]
    completed = project.run_pytest(
        "-p",
        "vfy._pytest.native_pytest",
        "-p",
        "no:cacheprovider",
        *blocked,
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None},
        load_xdist=False,
    )
    completed.expect(0)
    assert "1 passed" in completed.stdout


@pytest.mark.skipif(os.name != "posix", reason="collected mutation children require fork")
def test_mutation_children_load_same_mock_plugin_as_baseline(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def result():\n    return 1\ndef external():\n    return 9\n")
    project.write(
        "test_app.py",
        "import app\ndef test_result(mocker):\n"
        "    mocked = mocker.patch('app.external', return_value=4)\n"
        "    assert app.external() == 4\n"
        "    mocked.assert_called_once_with()\n"
        "    assert app.result() == 1\n",
    )
    outcomes = []
    for engine in ("auto", "portable"):
        completed = project.run_vfy(
            "mutate",
            "--engine=pytest-native",
            "--all",
            "--source",
            "app.py",
            "--operator",
            "integer_plus_one",
            "--max-mutations",
            "1",
            "--no-cache",
            "--format",
            "json",
            "-j",
            "1",
            "-p",
            "pytest_mock",
            env={"VFY_MUTATION_ENGINE": engine},
        )
        completed.expect(0)
        report = json.loads(completed.stdout)
        if engine == "auto":
            assert report["metrics"]["fast_results"] == 1, report
        outcomes.append(
            [(row["status"], row["killer"], row["phase"]) for row in report["mutations"]]
        )
    assert outcomes[0] == outcomes[1] == [("protected", "test_app.py::test_result", "call")]


def test_repeated_sessions_restore_upstream_mock_assertion_methods(tmp_path: Path) -> None:
    for engine in ("upstream", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_app.py",
            "import unittest.mock\ndef test_wrapped(mocker):\n"
            "    assert unittest.mock.NonCallableMock.assert_called_with.__module__ "
            "== 'pytest_mock.plugin'\n",
        )
        native = (
            ["-p", "no:cacheprovider", "-p", "vfy._pytest.native_pytest"]
            if engine == "pytest-native"
            else []
        )
        project.write(
            "run_sessions.py",
            "import pytest, unittest.mock\n"
            "original = unittest.mock.NonCallableMock.assert_called_with\n"
            f"arguments = {[*native, '-p', 'pytest_mock', '-q']!r}\n"
            "for _ in range(2):\n"
            "    assert pytest.main(arguments) == 0\n"
            "    assert unittest.mock.NonCallableMock.assert_called_with is original\n",
        )
        completed = project.run_python("run_sessions.py")
        completed.expect(0)
        assert completed.stdout.count("1 passed") == 2


@pytest.mark.parametrize("project_metadata", [False, True])
def test_untrusted_mock_modules_fail_before_configure(
    tmp_path: Path, project_metadata: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest_mock.py",
        "from pathlib import Path\ndef pytest_configure(config):\n"
        "    Path('configured.txt').write_text('ran')\n",
    )
    project.write("test_app.py", "def test_pass():\n    assert True\n")
    if project_metadata:
        project.write("pytest_mock-99.dist-info/METADATA", "Name: pytest-mock\nVersion: 99\n")
    completed = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=1", "--no-cov", "-p", "pytest_mock"
    )
    assert completed.returncode != 0
    transcript = completed.stdout + completed.stderr
    assert "native pytest requires the installed upstream pytest-mock plugin" in transcript
    assert not (project.root / "configured.txt").exists()


@pytest.mark.parametrize("plugin_name", ["pytest_mock", "pytest_mock.plugin"])
def test_project_distribution_metadata_cannot_admit_mock_configure_hook(
    tmp_path: Path, plugin_name: str
) -> None:
    observations: list[tuple[CommandResult, bool]] = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        configure = (
            "from pathlib import Path\ndef pytest_configure(config):\n"
            "    Path('configured.txt').write_text('project plugin ran')\n"
        )
        project.write("pytest_mock/__init__.py", configure if plugin_name == "pytest_mock" else "")
        project.write("pytest_mock/plugin.py", configure)
        project.write(
            "pytest_mock-3.15.1.dist-info/METADATA", "Name: pytest-mock\nVersion: 3.15.1\n"
        )
        project.write("test_app.py", "def test_pass():\n    assert True\n")
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "-p", plugin_name, "-q"
        )
        observations.append((result, (project.root / "configured.txt").exists()))

    upstream, native = observations
    assert upstream[0].returncode == 0, upstream[0].stdout + upstream[0].stderr
    assert upstream[1]
    assert native[0].returncode != 0
    assert "native pytest requires the installed upstream pytest-mock plugin" in (
        native[0].stdout + native[0].stderr
    )
    assert not native[1]


@pytest.mark.parametrize("plugin_name", ["pytest_mock", "pytest_mock.plugin"])
def test_project_metadata_does_not_hide_installed_mock_plugin(
    tmp_path: Path, plugin_name: str
) -> None:
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("pytest_mock-99.dist-info/METADATA", "Name: pytest-mock\nVersion: 99\n")
        project.write(
            "test_app.py",
            "def test_mock(mocker):\n    assert mocker.Mock(return_value=42)() == 42\n",
        )
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "-p", plugin_name, "-q"
        )
        result.expect(0)
        assert "1 passed" in result.stdout
