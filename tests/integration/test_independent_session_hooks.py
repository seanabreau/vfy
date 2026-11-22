"""Native session hooks surround execution and preserve live session mutations."""

from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()


def test_session_loop_counts_and_terminal_summary_order_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """from pathlib import Path
import pytest

def event(text):
    with Path('events').open('a') as output:
        output.write(text + '\\n')

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtestloop(session):
    event(f'loop before {session.testsfailed}')
    result = yield
    event(f'loop after {result!r} {session.testsfailed}')
    session.testsfailed += 1
    return result

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    event(f'finish outer before {exitstatus} {session.testsfailed}')
    result = yield
    event('finish outer after')
    return result

@pytest.hookimpl(wrapper=True, specname='pytest_sessionfinish')
def pytest_finish_inner(session, exitstatus):
    event('finish inner before')
    result = yield
    event('finish inner after')
    return result

def pytest_terminal_summary(terminalreporter, exitstatus, config):
    assert terminalreporter.config is config
    event(f'terminal {exitstatus}')
    terminalreporter.write_line('custom coverage summary')

def pytest_unconfigure(config):
    event('unconfigure')
""",
    )
    project.write(
        "test_cases.py",
        """def test_pass():
    pass

def test_fail():
    raise ValueError('failure counted before loop wrappers resume')
""",
    )
    oracle = project.run_python("-m", "pytest", "-q")
    expected = (project.root / "events").read_text()
    (project.root / "events").unlink()
    actual = project.run_python("-m", "vfy.independent.entry", env={"PYTHONPATH": _SOURCE})
    assert oracle.returncode == actual.returncode == 1, (
        oracle.stdout + oracle.stderr + actual.stdout + actual.stderr
    )
    assert "custom coverage summary" in oracle.stdout
    assert "custom coverage summary" in actual.stdout
    assert (project.root / "events").read_text() == expected
    assert "loop after True 1\n" in expected
    assert "finish outer before 1 2\n" in expected
    assert expected.index("finish inner after") < expected.index("terminal 1")
    assert expected.index("terminal 1") < expected.index("finish outer after")


def test_loop_wrapper_failure_mutation_preserves_passing_case_results(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest
@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session):
    result = yield
    assert result is True
    assert session.testsfailed == 0
    session.testsfailed += 1
    return result
""",
    )
    project.write("test_pass.py", "def test_pass():\n    pass\n")
    oracle = project.run_python("-m", "pytest", "-q")
    actual = project.run_python("-m", "vfy.independent.entry", env={"PYTHONPATH": _SOURCE})
    assert oracle.returncode == actual.returncode == 1, (
        oracle.stdout + oracle.stderr + actual.stdout + actual.stderr
    )
    assert "1 passed" in oracle.stdout
    assert "1 passed" in actual.stdout


def test_initial_conftest_hook_runs_after_plugin_options_and_before_conftest_imports(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "early_plugin.py",
        """import os
from pathlib import Path

def pytest_addoption(parser):
    parser.addoption('--probe-active', action='store_true')

def pytest_load_initial_conftests(early_config, parser, args):
    assert early_config.known_args_namespace.probe_active is True
    assert '--probe-active' in args
    os.environ['EARLY_PLUGIN_ACTIVE'] = 'yes'
    with Path('events').open('a') as output:
        output.write('early hook\\n')
""",
    )
    project.write(
        "conftest.py",
        """import os
from pathlib import Path
assert os.environ.get('EARLY_PLUGIN_ACTIVE') == 'yes'
with Path('events').open('a') as output:
    output.write('conftest import\\n')

def pytest_load_initial_conftests(early_config):
    raise AssertionError('conftest hooks are registered after the early hook')

def pytest_configure(config):
    assert config.getoption('probe_active') is True
    with Path('events').open('a') as output:
        output.write('configured\\n')
""",
    )
    project.write("test_pass.py", "def test_pass():\n    pass\n")
    arguments = ["-p", "early_plugin", "--probe-active"]
    oracle = project.run_python("-m", "pytest", "-q", *arguments)
    expected = (project.root / "events").read_text()
    (project.root / "events").unlink()
    actual = project.run_python(
        "-m", "vfy.independent.entry", *arguments, env={"PYTHONPATH": _SOURCE}
    )
    assert oracle.returncode == actual.returncode == 0, (
        oracle.stdout + oracle.stderr + actual.stdout + actual.stderr
    )
    assert (project.root / "events").read_text() == expected
    assert expected == "early hook\nconftest import\nconfigured\n"


def test_collect_only_dispatches_the_session_loop_without_running_bodies(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """from pathlib import Path
import pytest
@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session):
    with Path('events').open('a') as output:
        output.write(f'loop before {session.testsfailed}\\n')
    result = yield
    with Path('events').open('a') as output:
        output.write(f'loop after {result!r} {session.testsfailed}\\n')
    return result
""",
    )
    project.write("test_pass.py", "def test_pass():\n    raise AssertionError('must not run')\n")
    oracle = project.run_python("-m", "pytest", "-q", "--collect-only")
    expected = (project.root / "events").read_text()
    (project.root / "events").unlink()
    actual = project.run_python(
        "-m", "vfy.independent.entry", "--collect-only", env={"PYTHONPATH": _SOURCE}
    )
    assert oracle.returncode == actual.returncode == 0, (
        oracle.stdout + oracle.stderr + actual.stdout + actual.stderr
    )
    assert (project.root / "events").read_text() == expected == "loop before 0\nloop after True 0\n"


def test_failed_collection_loop_raises_the_typed_interruption_to_wrappers(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """from pathlib import Path
import pytest

def event(text):
    with Path('events').open('a') as output:
        output.write(text + '\\n')

@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session):
    event(f'loop before {session.testsfailed}')
    try:
        result = yield
    except session.Interrupted as error:
        event(f'loop interrupted {error}')
        raise
    else:
        event(f'loop after {result!r}')
        return result

def pytest_sessionfinish(session, exitstatus):
    event(f'finished {exitstatus}')
""",
    )
    project.write("test_bad.py", "raise ValueError('collection error')\n")
    oracle = project.run_python("-m", "pytest", "-q")
    expected = (project.root / "events").read_text()
    (project.root / "events").unlink()
    actual = project.run_python("-m", "vfy.independent.entry", env={"PYTHONPATH": _SOURCE})
    assert oracle.returncode == actual.returncode == 2, (
        oracle.stdout + oracle.stderr + actual.stdout + actual.stderr
    )
    assert (project.root / "events").read_text() == expected
    assert expected == "loop before 1\nloop interrupted 1 error during collection\nfinished 2\n"


def test_terminal_summary_reuses_reporter_registered_at_normal_configure_priority(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """from pathlib import Path
import pytest

def event(value):
    with Path('events').open('a') as output:
        output.write(value + '\\n')

def pytest_configure(config):
    event(f'normal {config.pluginmanager.get_plugin("terminalreporter") is None}')

@pytest.hookimpl(trylast=True, specname='pytest_configure')
def pytest_late_configure(config):
    global reporter
    reporter = config.pluginmanager.get_plugin('terminalreporter')
    assert reporter is not None
    reporter.write_line('write during configure')
    event('late configured')

def pytest_terminal_summary(terminalreporter):
    assert terminalreporter is reporter
    terminalreporter.write_line('write during summary')
    event('summary same reporter')
""",
    )
    project.write("test_pass.py", "def test_pass():\n    pass\n")
    oracle = project.run_python("-m", "pytest", "-q")
    expected = (project.root / "events").read_text()
    (project.root / "events").unlink()
    actual = project.run_python("-m", "vfy.independent.entry", env={"PYTHONPATH": _SOURCE})
    assert oracle.returncode == actual.returncode == 0, (
        oracle.stdout + oracle.stderr + actual.stdout + actual.stderr
    )
    assert (project.root / "events").read_text() == expected
    assert expected == "normal True\nlate configured\nsummary same reporter\n"
    for result in (oracle, actual):
        assert "write during configure" in result.stdout
        assert "write during summary" in result.stdout
        assert result.stdout.index("write during configure") < result.stdout.index(
            "write during summary"
        )


def test_loop_wrapper_error_after_execution_keeps_completed_cases(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """from pathlib import Path
import pytest

@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session):
    yield
    raise RuntimeError('loop failed after body')

def pytest_sessionfinish(session, exitstatus):
    Path('finished').write_text(f'{int(exitstatus)} {session.testsfailed}')
""",
    )
    project.write("test_pass.py", "def test_pass():\n    pass\n")
    oracle = project.run_python("-m", "pytest", "-q")
    expected = (project.root / "finished").read_text()
    actual = project.run_python("-m", "vfy.independent.entry", env={"PYTHONPATH": _SOURCE})
    assert oracle.returncode == actual.returncode == 3, (
        oracle.stdout + oracle.stderr + actual.stdout + actual.stderr
    )
    assert (project.root / "finished").read_text() == expected == "3 0"
    for result in (oracle, actual):
        assert "1 passed" in result.stdout
        assert "loop failed after body" in result.stdout + result.stderr


def test_early_known_arguments_include_interleaved_paths_in_order(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "early_plugin.py",
        """import json
from pathlib import Path

def pytest_load_initial_conftests(early_config):
    namespace = early_config.known_args_namespace
    paths = vars(namespace).get('file_or_dir', vars(namespace).get('paths'))
    Path('early-paths.json').write_text(json.dumps(paths))

def pytest_configure(config):
    loaded = Path('loaded-conftests')
    Path('configure-conftests').write_text(loaded.read_text() if loaded.exists() else '')
""",
    )
    for name in ("a", "b", "c"):
        project.write(f"{name}/test_{name}.py", f"def test_{name}(): pass\n")
        project.write(
            f"{name}/conftest.py",
            "from pathlib import Path\n"
            f"with Path('loaded-conftests').open('a') as stream: stream.write('{name}\\n')\n",
        )
    arguments = ["-p", "early_plugin", "a/test_a.py", "-q", "b/test_b.py", "-v", "c/test_c.py"]
    env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""}
    oracle = project.run_python("-m", "pytest", *arguments, env=env)
    expected_paths = (tmp_path / "early-paths.json").read_text()
    expected_conftests = (tmp_path / "configure-conftests").read_text()
    for name in ("early-paths.json", "configure-conftests", "loaded-conftests"):
        (tmp_path / name).unlink()
    actual = project.run_python("-m", "vfy.independent.entry", *arguments, env=env)
    assert oracle.returncode == actual.returncode == 0, (
        oracle.stdout + oracle.stderr + actual.stdout + actual.stderr
    )
    assert expected_paths == '["a/test_a.py", "b/test_b.py", "c/test_c.py"]'
    assert expected_conftests == "a\nb\nc\n"
    assert (tmp_path / "early-paths.json").read_text() == expected_paths
    assert (tmp_path / "configure-conftests").read_text() == expected_conftests
