"""Real serial xdist plugins coexist with the independent native worker owner."""

from __future__ import annotations

import json
from importlib.metadata import distributions
from pathlib import Path
from typing import Literal

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

Engine = Literal["stock", "independent"]
_ENV = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}


def _other_plugins() -> list[str]:
    names = {
        entry.name
        for distribution in distributions()
        for entry in distribution.entry_points
        if entry.group == "pytest11" and entry.name not in {"xdist", "xdist.looponfail"}
    }
    return [argument for name in sorted(names) for argument in ("-p", f"no:{name}")]


def _run(project: VfyProject, engine: Engine, *arguments: str, workers: int = 1) -> CommandResult:
    if engine == "stock":
        return project.run_pytest(*arguments, load_xdist=False, env=_ENV)
    return project.run_python(
        "-m",
        "vfy.independent.entry",
        f"--workers={workers}",
        *arguments,
        env=_ENV,
    )


# The profiler counts calls of the unmodified installed plugin code objects.
# It observes actual calls, including registration before configure, in every
# fresh process; no wrapper replaces a distribution's implementation.
_OBSERVER = """import hashlib
import json
import os
from pathlib import Path
import sys
import pytest
from xdist import plugin, looponfail

modules = (plugin, looponfail)
codes = {value.__code__: module.__name__ + '.' + name
         for module in modules for name, value in vars(module).items()
         if name.startswith('pytest_') and callable(value) and hasattr(value, '__code__')}
counts = {name: 0 for name in codes.values()}
previous = sys.getprofile()
def observe(frame, event, arg):
    if event == 'call' and frame.f_code in codes:
        counts[codes[frame.f_code]] += 1
    if previous is not None:
        previous(frame, event, arg)
sys.setprofile(observe)

@pytest.hookimpl(tryfirst=True)
def pytest_xdist_auto_num_workers(config):
    return 7

def pytest_unconfigure(config):
    sys.setprofile(previous)
    manager = config.pluginmanager
    Path('activation-' + str(os.getpid()) + '.json').write_text(json.dumps({
        'pid': os.getpid(), 'counts': counts,
        'modules': {m.__name__: hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
                    for m in modules},
        'plugins': {name: module.__name__ for name, module in manager.list_name_plugin()
                    if getattr(module, '__name__', '') in ('xdist.plugin', 'xdist.looponfail')},
        'options': {name: config.getoption(name)
                    for name in ('dist', 'numprocesses', 'tx', 'looponfail')},
        'ini': {name: config.getini(name)
                for name in ('rsyncdirs', 'rsyncignore', 'looponfailroots')},
        'configured': any(m.startswith('xdist_group:') for m in config.getini('markers')),
        'distributed': manager.hasplugin('dsession'),
    }))
"""

_TESTS = """import os
from pathlib import Path
import pytest

@pytest.mark.parametrize('value', [0, 1, 2, 3])
def test_real_plugins(request, worker_id, testrun_uid, value):
    config = request.config
    # Like pytest-xdist, workers report gwN through workerinput; serial runs report master.
    workerinput = getattr(config, 'workerinput', None)
    assert worker_id == (workerinput['workerid'] if workerinput else 'master')
    assert len(testrun_uid) == 32 and all(c in '0123456789abcdef' for c in testrun_uid)
    assert config.hook.pytest_xdist_auto_num_workers(config=config) == 7
    with Path('ran-' + str(os.getpid())).open('a') as stream:
        stream.write(str(value) + '\\n')
"""


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("numprocesses", [None, 0])
def test_installed_xdist_serial_hooks_and_native_workers(
    tmp_path: Path, engine: Engine, workers: int, numprocesses: int | None
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("activation_observer.py", _OBSERVER)
    project.write("test_plugins_a.py", _TESTS.replace("[0, 1, 2, 3]", "[0, 1]"))
    project.write("test_plugins_b.py", _TESTS.replace("[0, 1, 2, 3]", "[2, 3]"))
    arguments = [
        "-q",
        "-p",
        "activation_observer",
        "-p",
        "xdist.plugin",
        "-p",
        "xdist.looponfail",
    ]
    if numprocesses is not None:
        arguments.append(f"-n{numprocesses}")
    if engine == "native":
        arguments.append("--native-result-json=actual.json")
    result = _run(project, engine, *arguments, workers=workers)
    result.expect(0)
    assert "4 passed" in result.stdout
    if engine == "native":
        record = json.loads((tmp_path / "actual.json").read_text())
        assert record["collected"] == 4
        assert record["counts"]["passed"] == 4
        assert record["execution_topology"]["requested_workers"] == workers
        assert record["execution_topology"]["active_workers"] == workers
        assert len(set(record["execution_topology"]["worker_pids"])) == workers
    rows = [json.loads(path.read_text()) for path in tmp_path.glob("activation-*.json")]
    assert len(rows) == (3 if engine == "native" and workers == 2 else 1)
    sources = {tuple(sorted(row["modules"].items())) for row in rows}
    assert len(sources) == 1
    for row in rows:
        assert row["plugins"] == {
            "xdist.plugin": "xdist.plugin",
            "xdist.looponfail": "xdist.looponfail",
        }
        assert row["options"] == {
            "dist": "no",
            "numprocesses": numprocesses,
            "tx": [],
            "looponfail": False,
        }
        assert row["ini"] == {"rsyncdirs": [], "rsyncignore": [], "looponfailroots": []}
        assert row["configured"] is True
        assert row["distributed"] is False
        assert row["counts"] == {
            "xdist.plugin.pytest_addhooks": 1,
            "xdist.plugin.pytest_addoption": 1,
            "xdist.plugin.pytest_cmdline_main": 1,
            "xdist.plugin.pytest_configure": 1,
            "xdist.plugin.pytest_xdist_auto_num_workers": 0,
            "xdist.looponfail.pytest_addoption": 1,
            "xdist.looponfail.pytest_cmdline_main": 1,
        }
    executions = [line for path in tmp_path.glob("ran-*") for line in path.read_text().splitlines()]
    assert sorted(executions) == ["0", "1", "2", "3"]
    assert len(list(tmp_path.glob("ran-*"))) == (2 if engine == "native" and workers == 2 else 1)


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
def test_autoload_xdist_registers_real_entrypoint(tmp_path: Path, engine: Engine) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_alias.py",
        """def test_alias(pytestconfig, worker_id):
    import xdist.plugin
    assert worker_id == 'master'
    records = [(plugin, dist) for plugin, dist in pytestconfig.pluginmanager.list_plugin_distinfo()
               if getattr(plugin, '__name__', None) == 'xdist.plugin']
    assert len(records) == 1
    assert records[0][0] is xdist.plugin
    assert records[0][1].version == '3.8.0'
""",
    )
    arguments = (*_other_plugins(), "-q")
    if engine == "stock":
        result = project.run_pytest(
            *arguments, load_xdist=False, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}
        )
    else:
        result = project.run_python(
            "-m",
            "vfy.independent.entry",
            *arguments,
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None},
        )
    result.expect(0)
    assert "1 passed" in result.stdout


@requires_native_pytest
@pytest.mark.parametrize("arguments", [("-n2",), ("--dist=load", "--tx=popen"), ("-f",)])
def test_xdist_execution_requests_reject_before_test_import(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_never.py", "raise RuntimeError('test module must not import')\n")
    result = _run(
        project,
        "independent",
        "-p",
        "xdist.plugin",
        "-p",
        "xdist.looponfail",
        *arguments,
    )
    result.expect(4)
    assert "independent pytest" in result.stderr
    assert "xdist" in result.stderr
    assert "test module must not import" not in result.stdout + result.stderr


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
def test_ini_omitted_defaults_and_explicit_none_are_distinct(
    tmp_path: Path, engine: Engine
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """def pytest_addoption(parser):
    for kind in ('paths', 'pathlist', 'args', 'linelist', 'bool', 'int', 'float', 'string'):
        parser.addini('implicit_' + kind, '', type=kind)
        parser.addini('explicit_' + kind, '', type=kind, default=None)
""",
    )
    project.write(
        "test_ini.py",
        """def test_ini(pytestconfig):
    for kind, expected in [('paths', []), ('pathlist', []), ('args', []), ('linelist', []),
                           ('bool', False), ('int', 0), ('float', 0.0), ('string', '')]:
        assert pytestconfig.getini('implicit_' + kind) == expected
        assert pytestconfig.getini('explicit_' + kind) is None
    first = pytestconfig.getini('implicit_paths')
    first.append('changed')
    assert pytestconfig.getini('implicit_paths') is first
    assert pytestconfig.getini('implicit_args') == []
""",
    )
    result = _run(project, engine, "-q")
    result.expect(0)
    assert "1 passed" in result.stdout


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("empty", [False, True])
def test_command_wrapper_encloses_session_and_cleanup(
    tmp_path: Path, engine: Engine, empty: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "command_plugin.py",
        """from pathlib import Path
import pytest

def event(value):
    with Path('events').open('a') as stream: stream.write(value + '\\n')

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_cmdline_main(config):
    event('wrapper before')
    code = yield
    event('wrapper after ' + str(int(code)))
    return code

class First:
    @pytest.hookimpl(tryfirst=True)
    def pytest_cmdline_main(self, config): event('first None')
class Ordinary:
    def pytest_cmdline_main(self, config): event('ordinary None')
class Late:
    @pytest.hookimpl(trylast=True)
    def pytest_cmdline_main(self, config): event('late must be bypassed')
def pytest_addhooks(pluginmanager):
    pluginmanager.register(First(), 'first')
    pluginmanager.register(Ordinary(), 'ordinary')
    pluginmanager.register(Late(), 'late')
def pytest_configure(config):
    event('configure')
    config.add_cleanup(lambda: event('cleanup'))
def pytest_sessionstart(session): event('sessionstart')
def pytest_sessionfinish(session, exitstatus): event('sessionfinish ' + str(int(exitstatus)))
def pytest_unconfigure(config): event('unconfigure')
""",
    )
    if not empty:
        project.write(
            "test_body.py",
            "from command_plugin import event\ndef test_body(): event('body')\n",
        )
    result = _run(project, engine, "-q", "-p", "command_plugin")
    status = 5 if empty else 0
    assert result.returncode == status, result.stdout + result.stderr
    expected = [
        "wrapper before",
        "first None",
        "ordinary None",
        "configure",
        "sessionstart",
    ]
    if not empty:
        expected.append("body")
    expected += [
        f"sessionfinish {status}",
        "unconfigure",
        "cleanup",
        f"wrapper after {status}",
    ]
    assert (tmp_path / "events").read_text().splitlines() == expected


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("status", [0, 4])
def test_command_firstresult_bypasses_session_without_test_evidence(
    tmp_path: Path, engine: Engine, status: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "early_plugin.py",
        f"""from pathlib import Path
import pytest

@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    config.add_cleanup(lambda: Path('cleaned').write_text('yes'))
    return {status}
def pytest_configure(config): raise RuntimeError('must not configure')
""",
    )
    project.write("test_never.py", "raise RuntimeError('must not import')\n")
    # The direct independent CLI reports command completion; the cached-suite
    # controller requires a completed suite for its own work.
    arguments = ["-q", "-p", "early_plugin"]
    if engine == "stock":
        result = project.run_pytest(*arguments, load_xdist=False, env=_ENV)
    else:
        result = project.run_python(
            "-m",
            "vfy.independent.entry",
            "--native-result-json=never.json",
            *arguments,
            env=_ENV,
        )
    assert result.returncode == status, result.stdout + result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert (tmp_path / "cleaned").read_text() == "yes"
    assert not (tmp_path / "never.json").exists()


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("where", ["before", "configure", "after"])
def test_command_errors_preserve_original_exception_and_cleanup(
    tmp_path: Path, engine: Engine, where: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "error_plugin.py",
        f"""from pathlib import Path
import pytest

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_cmdline_main(config):
    config.add_cleanup(lambda: Path('cleaned').write_text('yes'))
    if {where!r} == 'before': raise RuntimeError('original command failure')
    result = yield
    if {where!r} == 'after': raise RuntimeError('original command failure')
    return result
def pytest_configure(config):
    if {where!r} == 'configure': raise RuntimeError('original command failure')
""",
    )
    project.write("test_body.py", "def test_body(): pass\n")
    result = _run(project, engine, "-q", "-p", "error_plugin")
    assert result.returncode != 0
    assert "RuntimeError: original command failure" in result.stdout + result.stderr
    assert (tmp_path / "cleaned").read_text() == "yes"


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
def test_explicit_xdist_module_and_autoload_preserve_duplicate_error(
    tmp_path: Path, engine: Engine
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_never.py", "raise RuntimeError('must not import')\n")
    arguments = (*_other_plugins(), "-p", "xdist.plugin")
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}
    if engine == "stock":
        result = project.run_pytest(*arguments, load_xdist=False, env=environment)
    else:
        result = project.run_python("-m", "vfy.independent.entry", *arguments, env=environment)
    result.expect(1)
    assert "Plugin already registered under a different name: xdist=" in result.stderr
    assert "must not import" not in result.stdout + result.stderr


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
def test_command_wrapper_exit_status_does_not_rewrite_session_result(
    tmp_path: Path, engine: Engine
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "status_plugin.py",
        """from pathlib import Path
import pytest
@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_cmdline_main(config):
    status = yield
    assert int(status) == 0
    return 4
def pytest_sessionfinish(session, exitstatus):
    Path('session-status').write_text(str(int(exitstatus)))
""",
    )
    project.write("test_body.py", "def test_body(): pass\n")
    args = ("-q", "-p", "status_plugin")
    if engine == "stock":
        result = project.run_pytest(*args, load_xdist=False, env=_ENV)
    else:
        result = project.run_python(
            "-m", "vfy.independent.entry", "--native-result-json=actual.json", *args, env=_ENV
        )
        assert json.loads((tmp_path / "actual.json").read_text())["exitstatus"] == 0
    result.expect(4)
    assert (tmp_path / "session-status").read_text() == "0"
    assert "1 passed" in result.stdout


@requires_native_pytest
@pytest.mark.parametrize("workers", [None, 2])
def test_public_vfy_cli_native_workers_with_installed_xdist_serial(
    tmp_path: Path, workers: int | None
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    body = """import os
from pathlib import Path

def test_body(pytestconfig, worker_id):
    # Like pytest-xdist, workers report gwN through workerinput; serial runs report master.
    workerinput = getattr(pytestconfig, 'workerinput', None)
    assert worker_id == (workerinput['workerid'] if workerinput else 'master')
    assert pytestconfig.getoption('numprocesses') is None
    assert pytestconfig.getoption('dist') == 'no'
    Path('body-' + str(os.getpid()) + '-' + __name__).write_text('ran')
"""
    project.write("test_a.py", body)
    project.write("test_b.py", body)
    arguments = ["test", "--engine=native", "--no-cov"]
    if workers is not None:
        arguments.append(f"-n{workers}")
    result = project.run_vfy(
        *arguments, "--native-result-json=actual.json", "-p", "xdist.plugin", env=_ENV
    )
    result.expect(0)
    assert "2 passed" in result.stdout
    record = json.loads((tmp_path / "actual.json").read_text())
    expected = 1 if workers is None else workers
    assert record["collected"] == 2
    assert record["counts"]["passed"] == 2
    assert record["execution_topology"]["requested_workers"] == expected
    assert record["execution_topology"]["active_workers"] == expected
    assert len(set(record["execution_topology"]["worker_pids"])) == expected
    assert len(list(tmp_path.glob("body-*"))) == 2


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
def test_installed_xdist_collect_only_keeps_hooks_without_running_bodies(
    tmp_path: Path, engine: Engine
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("activation_observer.py", _OBSERVER)
    project.write("test_plugins.py", _TESTS)
    result = _run(
        project,
        engine,
        "-q",
        "--collect-only",
        "-n0",
        "-p",
        "activation_observer",
        "-p",
        "xdist.plugin",
        "-p",
        "xdist.looponfail",
    )
    result.expect(0)
    assert all(f"test_real_plugins[{value}]" in result.stdout for value in range(4))
    assert "4 tests collected" in result.stdout
    assert not list(tmp_path.glob("ran-*"))
    rows = [json.loads(path.read_text()) for path in tmp_path.glob("activation-*.json")]
    assert len(rows) == 1
    row = rows[0]
    assert row["options"] == {
        "dist": "no",
        "numprocesses": 0,
        "tx": [],
        "looponfail": False,
    }
    assert row["ini"] == {"rsyncdirs": [], "rsyncignore": [], "looponfailroots": []}
    assert row["configured"] is True
    assert row["distributed"] is False
    assert row["counts"] == {
        "xdist.plugin.pytest_addhooks": 1,
        "xdist.plugin.pytest_addoption": 1,
        "xdist.plugin.pytest_cmdline_main": 1,
        "xdist.plugin.pytest_configure": 1,
        "xdist.plugin.pytest_xdist_auto_num_workers": 0,
        "xdist.looponfail.pytest_addoption": 1,
        "xdist.looponfail.pytest_cmdline_main": 1,
    }


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("serial", [("-n", "0"), ("-n0",), ("--numprocesses=auto", "-n0")])
def test_cli_serial_xdist_overrides_ini_worker_count(
    tmp_path: Path, engine: Engine, serial: tuple[str, ...]
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = --numprocesses=auto --strict-markers\n")
    project.write(
        "test_options.py",
        "def test_options(pytestconfig):\n"
        "    assert pytestconfig.getoption('numprocesses') == 0\n"
        "    assert pytestconfig.getoption('strict_markers') is True\n",
    )
    arguments = ("-q", "-p", "xdist.plugin", *serial)
    result = (
        project.run_pytest(*arguments, load_xdist=False, env=_ENV)
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", *arguments, env=_ENV)
    )
    result.expect(0)
    assert "1 passed" in result.stdout


@requires_native_pytest
def test_cli_later_xdist_execution_request_is_rejected(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = --numprocesses=auto\n")
    project.write("test_body.py", "def test_body():\n    assert False\n")
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "xdist.plugin",
        "-n",
        "0",
        "--numprocesses=auto",
        env=_ENV,
    )
    result.expect(4)
    assert "unsupported xdist execution" in result.stderr


@requires_native_pytest
def test_cli_serial_workers_without_xdist_plugin(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_body.py", "def test_body():\n    pass\n")
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q", "-n", "0", env=_ENV)
    result.expect(0)
    assert "1 passed" in result.stdout
