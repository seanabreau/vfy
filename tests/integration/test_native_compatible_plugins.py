"""Installed plug-ins whose hooks native execution supports load unchanged."""

from __future__ import annotations

import json
import shutil
import signal
import sys
import textwrap
import venv
from importlib.metadata import distribution, entry_points
from pathlib import Path

import pytest

import vfy
from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_ENVIRONMENT = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}


def _datadir_project(root: Path) -> VfyProject:
    project = VfyProject(root)
    project.write("test_data/greeting.txt", "hello\n")
    project.write(
        "test_data.py",
        "def test_reads_shared_data(shared_datadir):\n"
        "    assert (shared_datadir / 'greeting.txt').read_text() == 'hello\\n'\n"
        "\n"
        "def test_copies_module_data(datadir):\n"
        "    assert (datadir / 'greeting.txt').exists()\n",
    )
    project.write("data/greeting.txt", "hello\n")
    return project


def _blocked_except(*names: str) -> list[str]:
    return [
        argument
        for plugin in entry_points(group="pytest11")
        if plugin.name not in names
        for argument in ("-p", f"no:{plugin.name}")
    ]


@pytest.mark.parametrize("route", ["serial", "workers"])
def test_datadir_fixtures_match_stock_pytest(tmp_path: Path, route: str) -> None:
    blocked = _blocked_except("pytest-datadir")
    for engine in ("stock", "pytest-native"):
        project = _datadir_project(tmp_path / engine)
        completed = (
            project.run_python("-m", "pytest", *blocked, "-q", env=_ENVIRONMENT)
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--all",
                *(["--workers=2"] if route == "workers" else []),
                "--no-cov",
                *blocked,
                "-q",
                env=_ENVIRONMENT,
            )
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "2 passed" in completed.stdout


@pytest.mark.parametrize("blocked", ["pytest-datadir", "pytest_datadir.plugin"])
def test_blocking_compatible_plugin_matches_stock(tmp_path: Path, blocked: str) -> None:
    outcomes = []
    for engine in ("stock", "pytest-native"):
        project = _datadir_project(tmp_path / engine)
        arguments = ("-p", f"no:{blocked}", "-q")
        completed = (
            project.run_python("-m", "pytest", *arguments, env=_ENVIRONMENT)
            if engine == "stock"
            else project.run_vfy(
                "test", "--engine=pytest-native", "--all", "--no-cov", *arguments, env=_ENVIRONMENT
            )
        )
        outcomes.append(
            (completed.returncode == 0, "fixture 'shared_datadir' not found" in completed.stdout)
        )
    assert outcomes[0] == outcomes[1]


def test_explicit_compatible_plugin_load_is_accepted(tmp_path: Path) -> None:
    project = _datadir_project(tmp_path)
    completed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        "--no-cov",
        "-p",
        "pytest_datadir.plugin",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    completed.expect(0)
    assert "2 passed" in completed.stdout


def test_diagnostics_reports_compatible_plugin_as_loaded(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    result = project.run_vfy("diagnostics", "--engine=pytest-native", "--json", env=_ENVIRONMENT)
    result.expect(0)
    inventory = {row["name"]: row for row in json.loads(result.stdout)["pytest_plugin_inventory"]}
    datadir = inventory["pytest-datadir"]
    assert datadir["native_status"] == "loaded"
    assert datadir["differs_from_pytest"] is False
    assert "its hooks run unchanged under native execution" in datadir["reason"]
    assert inventory["hypothesispytest"]["native_status"] == "omitted"


def _editable_plugin_environment(
    project: VfyProject, source: str, *, environment: Path | None = None
) -> Path:
    project.write(
        "src/editableplug/__init__.py", "from pathlib import Path\nPath('imported').touch()\n"
    )
    project.write("src/editableplug/plugin.py", source)
    if environment is None:
        environment = project.root / "environment"
    venv.EnvBuilder(symlinks=sys.platform != "win32", with_pip=False).create(environment)
    site = (
        environment / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    metadata = site / "editableplug-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: editableplug\nVersion: 1.0\n")
    (metadata / "entry_points.txt").write_text("[pytest11]\neditableplug = editableplug.plugin\n")
    # Editable installs record only a path entry, not the package's files.
    (metadata / "RECORD").write_text("editableplug.pth,,\n")
    (site / "editableplug.pth").write_text(f"{project.root / 'src'}\n")
    (site / "dependencies.pth").write_text(
        f"{Path(vfy.__file__).parents[1]}\n{Path(pytest.__file__).parents[1]}\n"
    )
    return environment / "bin/python"


def test_watch_discovers_plugins_installed_and_removed_between_runs(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    interpreter = _editable_plugin_environment(
        project,
        "import pytest\n\n@pytest.fixture\ndef editable_value():\n    return 42\n",
        environment=tmp_path / "environment",
    )
    site = next(interpreter.parent.parent.glob("lib/python*/site-packages"))
    installed = site / "editableplug-1.0.dist-info"
    removed = tmp_path / "removed-dist-info"
    shutil.move(installed, removed)

    def test_source(phase: str, present: bool) -> str:
        return (
            "from pathlib import Path\n"
            "def test_plugin(request):\n"
            "    manager = request.config.pluginmanager\n"
            "    loaded = (manager.hasplugin('editableplug')\n"
            "              or manager.hasplugin('editableplug.plugin'))\n"
            f"    assert loaded is {present!r}\n"
            f"    Path('.vfy/observed-{phase}').touch()\n"
        )

    project.write("test_plugin.py", test_source("initial", False))
    project.git_init()
    project = VfyProject(project.root, interpreter=interpreter)
    command = project.start_vfy(
        "test",
        "--engine=pytest-native",
        "--watch",
        "--workers=1",
        "--no-replay",
        "--no-cov",
        "-q",
        env=_ENVIRONMENT,
    )
    try:
        project.wait_for_path(".vfy/observed-initial", timeout=20)
        shutil.move(removed, installed)
        project.write("test_plugin.py", test_source("installed", True))
        project.wait_for_path(".vfy/observed-installed", timeout=20)
        shutil.move(installed, removed)
        project.write("test_plugin.py", test_source("removed", False))
        project.wait_for_path(".vfy/observed-removed", timeout=20)
    except TimeoutError as error:
        pytest.fail(f"{error}\n{command.text('stdout')}\n{command.text()}")
    finally:
        command.send_signal(signal.SIGINT)
        completed = command.wait(timeout=15)
    completed.expect(2)


def test_editable_plugin_is_admitted_without_importing_its_package(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    interpreter = _editable_plugin_environment(
        project, "import pytest\n\n@pytest.fixture\ndef editable_value():\n    return 42\n"
    )
    project.write(
        "test_value.py", "def test_value(editable_value):\n    assert editable_value == 42\n"
    )
    diagnostic = project.run(
        str(interpreter),
        "-m",
        "vfy",
        "diagnostics",
        "--engine=pytest-native",
        "--json",
        env=_ENVIRONMENT,
    )
    diagnostic.expect(0)
    inventory = {
        row["name"]: row for row in json.loads(diagnostic.stdout)["pytest_plugin_inventory"]
    }
    assert inventory["editableplug"]["native_status"] == "loaded"
    assert not (project.root / "imported").exists()
    completed = project.run(
        str(interpreter),
        "-m",
        "vfy",
        "test",
        "--engine=pytest-native",
        "--no-cov",
        "-q",
        env=_ENVIRONMENT,
    )
    completed.expect(0)
    assert "1 passed" in completed.stdout


def test_unrelated_distribution_manifest_does_not_block_plugin_admission(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    interpreter = _editable_plugin_environment(
        project, "import pytest\n\n@pytest.fixture\ndef editable_value():\n    return 42\n"
    )
    site = interpreter.parent.parent / (
        f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    metadata = site / "unrelated-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: unrelated\nVersion: 1.0\n")
    (metadata / "entry_points.txt").write_text("[console_scripts]\nunrelated = unrelated:main\n")
    (metadata / "RECORD").write_bytes(b"\xff\n")
    project.write(
        "test_value.py", "def test_value(editable_value):\n    assert editable_value == 42\n"
    )
    completed = project.run(
        str(interpreter),
        "-m",
        "vfy",
        "test",
        "--engine=pytest-native",
        "--no-cov",
        "-q",
        env=_ENVIRONMENT,
    )
    completed.expect(0)
    assert "1 passed" in completed.stdout


def test_reexported_options_load_and_runtime_hooks_remain_validated(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    interpreter = _editable_plugin_environment(
        project, "from .options import pytest_addoption, pytest_configure\n"
    )
    project.write(
        "src/editableplug/options.py",
        "def pytest_addoption(parser):\n"
        "    parser.addoption('--plugin-value')\n"
        "    parser.addoption('--plugin-runtime', action='store_true')\n\n"
        "def pytest_configure(config):\n"
        "    if config.getoption('--plugin-runtime'):\n"
        "        from .runtime import Hooks\n"
        "        config.pluginmanager.register(Hooks(), 'editable-runtime')\n",
    )
    project.write(
        "src/editableplug/runtime.py",
        "from pathlib import Path\n\n"
        "class Hooks:\n"
        "    def pytest_runtest_call(self, item):\n"
        "        Path('hook-called').touch()\n",
    )
    project.write("pytest.ini", "[pytest]\naddopts = --plugin-value=present\n")
    project.write(
        "test_value.py",
        "def test_value(pytestconfig):\n"
        "    assert pytestconfig.getoption('--plugin-value') == 'present'\n",
    )
    diagnostic = project.run(
        str(interpreter),
        "-m",
        "vfy",
        "diagnostics",
        "--engine=pytest-native",
        "--json",
        env=_ENVIRONMENT,
    )
    diagnostic.expect(0)
    inventory = {
        row["name"]: row for row in json.loads(diagnostic.stdout)["pytest_plugin_inventory"]
    }
    assert inventory["editableplug"]["native_status"] == "loaded"
    assert not (project.root / "imported").exists()
    for engine in ("stock", "pytest-native"):
        arguments = (
            ("pytest",)
            if engine == "stock"
            else ("vfy", "test", "--engine=pytest-native", "--all", "--no-cov")
        )
        completed = project.run(str(interpreter), "-m", *arguments, "-q", env=_ENVIRONMENT)
        completed.expect(0)
        assert "1 passed" in completed.stdout

        enabled = project.run(
            str(interpreter), "-m", *arguments, "--plugin-runtime", "-q", env=_ENVIRONMENT
        )
        if engine == "stock":
            enabled.expect(0)
            assert (project.root / "hook-called").exists()
            (project.root / "hook-called").unlink()
        else:
            assert enabled.returncode != 0
            assert "plugin 'editable-runtime': pytest_runtest_call" in (
                enabled.stdout + enabled.stderr
            )
            assert not (project.root / "hook-called").exists()


def test_installed_protocol_hook_keeps_zero_worker_override(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    interpreter = _editable_plugin_environment(
        project,
        "from pathlib import Path\n\n"
        "def pytest_runtest_protocol(item):\n"
        "    with Path('events.txt').open('a') as stream:\n"
        "        stream.write('protocol:' + item.nodeid + '\\n')\n",
    )
    site = interpreter.parent.parent / (
        f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    xdist = distribution("pytest-xdist")
    metadata = next(
        path.parent
        for path in xdist.files or ()
        if path.name == "METADATA" and path.parent.suffix == ".dist-info"
    )
    # The isolated interpreter needs its own installed xdist installation identity.
    for relative in (Path("xdist"), metadata):
        shutil.copytree(
            str(xdist.locate_file(relative)),
            site / relative,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    project.write("pytest.ini", "[pytest]\naddopts = -p xdist -n auto --dist=worksteal\n")
    project.write(
        "test_protocol.py",
        "from pathlib import Path\n\n"
        "def test_local(worker_id):\n"
        "    assert worker_id == 'master'\n"
        "    with Path('events.txt').open('a') as stream:\n"
        "        stream.write('call\\n')\n",
    )
    for engine in ("stock", "pytest-native"):
        arguments = (
            ("pytest",)
            if engine == "stock"
            else ("vfy", "test", "--engine=pytest-native", "--all", "--no-cov")
        )
        completed = project.run(
            str(interpreter), "-m", *arguments, "--numprocesses=0", "-q", env=_ENVIRONMENT
        )
        completed.expect(0)
        assert (project.root / "events.txt").read_text().splitlines() == [
            "protocol:test_protocol.py::test_local",
            "call",
        ]
        (project.root / "events.txt").unlink()


@pytest.mark.parametrize(
    "declaration",
    [
        "for _ in (0,):\n{hook}",
        "from contextlib import nullcontext\nwith nullcontext():\n{hook}",
        "match 1:\n    case _:\n{nested_hook}",
        "target = 'pytest_runtest_call'\n@pytest.hookimpl(specname=target)\n{renamed_hook}",
        "from pytest import hookimpl as impl\n"
        "@impl(specname='pytest_runtest_call')\n{renamed_hook}",
        "class Hooks:\n{method_hook}\n"
        "def pytest_configure(config):\n"
        "    config.pluginmanager.register(Hooks(), 'editable-hooks')\n",
    ],
    ids=["for", "with", "match", "dynamic-specname", "aliased-decorator", "registered-class"],
)
def test_unsupported_editable_hooks_are_omitted_without_import(
    tmp_path: Path, declaration: str
) -> None:
    project = VfyProject(tmp_path)
    hook = "def pytest_runtest_call(item):\n    Path('hook-called').touch()\n"
    source = "import pytest\nfrom pathlib import Path\n" + declaration.format(
        hook=textwrap.indent(hook, "    "),
        nested_hook=textwrap.indent(hook, "        "),
        renamed_hook=hook.replace("pytest_runtest_call", "pytest_configure"),
        method_hook=textwrap.indent(hook.replace("(item)", "(self, item)"), "    "),
    )
    interpreter = _editable_plugin_environment(project, source)
    project.write("test_value.py", "def test_value():\n    assert True\n")
    diagnostic = project.run(
        str(interpreter),
        "-m",
        "vfy",
        "diagnostics",
        "--engine=pytest-native",
        "--json",
        env=_ENVIRONMENT,
    )
    diagnostic.expect(0)
    inventory = {
        row["name"]: row for row in json.loads(diagnostic.stdout)["pytest_plugin_inventory"]
    }
    assert inventory["editableplug"]["native_status"] == "omitted"
    assert not (project.root / "imported").exists()

    # Stock pytest confirms that each declaration installs the execution hook.
    stock = project.run(str(interpreter), "-m", "pytest", "-q", env=_ENVIRONMENT)
    stock.expect(0)
    assert (project.root / "hook-called").exists()
    (project.root / "hook-called").unlink()
    (project.root / "imported").unlink()
    native = project.run(
        str(interpreter),
        "-m",
        "vfy",
        "test",
        "--engine=pytest-native",
        "--no-cov",
        "-q",
        env=_ENVIRONMENT,
    )
    native.expect(0)
    assert "1 passed" in native.stdout
    assert not (project.root / "imported").exists()
    assert not (project.root / "hook-called").exists()


@pytest.mark.parametrize("workers", [1, 2])
def test_editable_configuration_and_collection_hooks_match_stock(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    interpreter = _editable_plugin_environment(
        project,
        "import pytest\nfrom pathlib import Path\n\n"
        "def pytest_addoption(parser):\n"
        "    parser.addoption('--plugin-value', default='missing')\n\n"
        "def pytest_configure(config):\n"
        "    config.addinivalue_line('markers', 'plugin_value: installed plugin marker')\n\n"
        "@pytest.hookimpl(wrapper=True)\n"
        "def pytest_collection_modifyitems(items):\n"
        "    result = yield\n"
        "    for item in items:\n"
        "        value = item.config.getoption('--plugin-value')\n"
        "        item.add_marker(pytest.mark.plugin_value(value))\n"
        "    return result\n\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    Path('plugin-finished').write_text(str(exitstatus))\n",
    )
    project.write(
        "test_value.py",
        "import pytest\n"
        "@pytest.mark.parametrize('number', [1, 2, 3])\n"
        "def test_value(request, number):\n"
        "    assert request.node.get_closest_marker('plugin_value').args == ('present',)\n",
    )
    for engine in ("stock", "pytest-native"):
        arguments = (
            ("pytest",)
            if engine == "stock"
            else ("vfy", "test", "--engine=pytest-native", f"--workers={workers}", "--no-cov")
        )
        completed = project.run(
            str(interpreter),
            "-m",
            *arguments,
            "--plugin-value=present",
            "-q",
            env={**_ENVIRONMENT, "VFY_WORKER_START": "exec"},
        )
        completed.expect(0)
        assert "3 passed" in completed.stdout
        assert (project.root / "plugin-finished").read_text() == "0"
        (project.root / "plugin-finished").unlink()


@pytest.mark.parametrize("engine", ["stock", "pytest-native"])
def test_installed_protocol_stop_preserves_session_fixture_cleanup_order(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    interpreter = _editable_plugin_environment(
        project,
        "import json\nfrom pathlib import Path\nimport pytest\n\n"
        "def record(event):\n"
        "    with Path('events.jsonl').open('a') as stream:\n"
        "        stream.write(json.dumps(event) + '\\n')\n\n"
        "@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
        "def pytest_runtest_protocol(item):\n"
        "    record('before')\n"
        "    item.session.shouldstop = 'installed plugin stop'\n"
        "    result = yield\n"
        "    record('after')\n"
        "    return result\n\n"
        "@pytest.fixture(scope='session')\n"
        "def plugin_answer():\n"
        "    record('setup')\n"
        "    yield 42\n"
        "    record('cleanup')\n",
    )
    project.write(
        "test_protocol.py",
        "from editableplug.plugin import record\n\n"
        "def test_first(plugin_answer):\n"
        "    assert plugin_answer == 42\n"
        "    record('call-first')\n\n"
        "def test_second(plugin_answer):\n"
        "    assert plugin_answer == 42\n"
        "    record('call-second')\n",
    )
    arguments = (
        ("pytest",)
        if engine == "stock"
        else ("vfy", "test", "--engine=pytest-native", "--all", "--workers=1", "--no-cov")
    )
    completed = project.run(
        str(interpreter),
        "-m",
        *arguments,
        *_blocked_except("editableplug"),
        "-q",
        env=_ENVIRONMENT,
    )
    transcript = completed.stdout + completed.stderr
    assert completed.returncode == pytest.ExitCode.INTERRUPTED, transcript
    assert "1 passed" in completed.stdout, transcript
    events_file = project.root / "events.jsonl"
    assert events_file.is_file(), transcript
    assert [json.loads(line) for line in events_file.read_text().splitlines()] == [
        "before",
        "setup",
        "call-first",
        "cleanup",
        "after",
    ], transcript
