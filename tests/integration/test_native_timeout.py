"""Installed pytest-timeout keeps pytest's timeouts under native execution."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="native execution and SIGALRM require Unix"
)

_AUTOLOAD = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}

_TESTS = """\
import time
import pytest


@pytest.fixture
def slow_fixture():
    time.sleep(0.6)


@pytest.fixture
def slow_teardown():
    yield
    time.sleep(3)


@pytest.mark.timeout(0.2)
def test_marker_call():
    time.sleep(3)


@pytest.mark.timeout(0.2)
def test_marker_setup(slow_fixture):
    pass


@pytest.mark.timeout(0.2)
def test_marker_teardown(slow_teardown):
    pass


@pytest.mark.timeout(0.2, func_only=True)
def test_func_only_call():
    time.sleep(3)


@pytest.mark.timeout(0.2, func_only=True)
def test_func_only_excludes_fixtures(slow_fixture):
    pass


def test_configured_timeout():
    time.sleep(3)


@pytest.mark.timeout(0)
def test_disabled_by_marker():
    time.sleep(0.6)


def test_fast():
    pass
"""


def _run(project: VfyProject, route: str, *arguments: str) -> tuple[int, str]:
    result = (
        project.run_python("-m", "pytest", "-p", "no:cacheprovider", *arguments, env=_AUTOLOAD)
        if route == "stock"
        else project.run_vfy(
            "test", "--engine=pytest-native", "--no-cov", *arguments, env=_AUTOLOAD
        )
    )
    return result.returncode, result.stdout + result.stderr


def _outcomes(project: VfyProject) -> list[tuple[str | None, list[tuple[str, str | None]]]]:
    return sorted(
        (
            case.get("name"),
            [(node.tag, node.get("message")) for node in case if node.tag in {"failure", "error"}],
        )
        for case in ET.parse(project.root / "result.xml").iter("testcase")
    )


@pytest.mark.parametrize("workers", ["1", "2", "xdist"])
def test_marker_ini_and_func_only_timeouts_match_stock(tmp_path: Path, workers: str) -> None:
    observed = []
    for route in ("stock", "native"):
        project = VfyProject(tmp_path / route)
        # Strict configuration rejects `timeout` unless the plug-in registered it.
        project.write(
            "pytest.ini",
            "[pytest]\naddopts = --strict-markers --strict-config\ntimeout = 0.3\n",
        )
        project.write("test_timeouts.py", _TESTS)
        arguments = ("--junitxml=result.xml", "-q")
        if route == "native":
            worker_option = "--numprocesses=2" if workers == "xdist" else f"--workers={workers}"
            arguments = (worker_option, *arguments)
        status, output = _run(project, route, *arguments)
        assert status == 1, output
        observed.append(_outcomes(project))
    assert observed[0] == observed[1]
    failed = {name for name, problems in observed[1] if problems}
    assert failed == {
        "test_marker_call",
        "test_marker_setup",
        "test_marker_teardown",
        "test_func_only_call",
        "test_configured_timeout",
    }
    assert all(
        "Timeout (>" in (message or "") for _, problems in observed[1] for _, message in problems
    )


@pytest.mark.parametrize("source", ["option", "environment"])
def test_command_line_and_environment_timeouts_match_stock(tmp_path: Path, source: str) -> None:
    observed = []
    for route in ("stock", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_cli.py", "import time\n\ndef test_slow():\n    time.sleep(3)\n")
        arguments = ("--timeout=0.2", "-q") if source == "option" else ("-q",)
        environment = {**_AUTOLOAD, "PYTEST_TIMEOUT": "0.2" if source == "environment" else None}
        result = (
            project.run_python(
                "-m", "pytest", "-p", "no:cacheprovider", *arguments, env=environment
            )
            if route == "stock"
            else project.run_vfy(
                "test", "--engine=pytest-native", "--no-cov", *arguments, env=environment
            )
        )
        status, output = result.returncode, result.stdout + result.stderr
        observed.append((status, "Timeout (>0.2s) from pytest-timeout." in output))
    assert observed[0] == observed[1] == (1, True)


def test_session_timeout_stops_after_the_current_test_like_stock(tmp_path: Path) -> None:
    observed = []
    for route in ("stock", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_session.py",
            "import time\n\n"
            "def test_first():\n    time.sleep(0.4)\n\n"
            "def test_second():\n    pass\n\n"
            "def test_third():\n    pass\n",
        )
        arguments = [
            "--session-timeout=0.2",
            "-q",
            "--junitxml=result.xml",
            "-p",
            "no:randomly",
        ]
        if route == "native":
            arguments.insert(0, "--workers=1")
        status, output = _run(project, route, *arguments)
        assert "session-timeout: 0.2 sec exceeded" in output, output
        observed.append((status, _outcomes(project)))
    assert observed[0] == observed[1]
    assert [name for name, _ in observed[1][1]] == ["test_first"]


@pytest.mark.parametrize(("arguments", "autoload"), [(("-p", "no:timeout"), None), ((), "1")])
def test_disabled_plugin_does_not_enforce_timeouts_like_stock(
    tmp_path: Path, arguments: tuple[str, ...], autoload: str | None
) -> None:
    for route in ("stock", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_disabled.py",
            "import time\nimport pytest\n\n"
            "@pytest.mark.timeout(0.1)\n"
            "def test_not_enforced():\n    time.sleep(0.3)\n",
        )
        environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": autoload}
        result = (
            project.run_python("-m", "pytest", *arguments, "-q", env=environment)
            if route == "stock"
            else project.run_vfy(
                "test", "--engine=pytest-native", "--no-cov", *arguments, "-q", env=environment
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout


def test_blocking_the_module_alias_keeps_the_entry_point_like_stock(tmp_path: Path) -> None:
    for route in ("stock", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_alias.py",
            "import time\nimport pytest\n\n"
            "@pytest.mark.timeout(0.1)\n"
            "def test_alias():\n    time.sleep(0.3)\n",
        )
        status, output = _run(project, route, "-p", "no:pytest_timeout", "-q")
        assert status == 1, output
        assert "Timeout (>0.1s) from pytest-timeout." in output
        if route == "native":
            diagnostic = project.run_vfy(
                "diagnostics",
                "--engine=pytest-native",
                "--json",
                "--",
                "-p",
                "no:pytest_timeout",
                env=_AUTOLOAD,
            )
            diagnostic.expect(0)
            timeout = next(
                row
                for row in json.loads(diagnostic.stdout)["pytest_plugin_inventory"]
                if row["name"] == "timeout"
            )
            assert timeout["native_status"] == "loaded"


@pytest.mark.parametrize("source", ["option", "marker"])
def test_thread_method_is_rejected_before_tests_run(tmp_path: Path, source: str) -> None:
    project = VfyProject(tmp_path)
    method = ", method='thread'" if source == "marker" else ""
    project.write(
        "test_thread.py",
        "from pathlib import Path\nimport pytest\n\n"
        f"@pytest.mark.timeout(1{method})\n"
        "def test_thread():\n    Path('ran').touch()\n",
    )
    arguments = ("--timeout-method=thread",) if source == "option" else ()
    result = project.run_vfy(
        "test", "--engine=pytest-native", "--no-cov", *arguments, "-q", env=_AUTOLOAD
    )
    assert result.returncode != 0
    assert "pytest-timeout's signal method, not the thread method" in result.stdout + result.stderr
    assert not (project.root / "ran").exists()


@pytest.mark.parametrize("source", ["parameter", "fixture"])
def test_runtime_thread_methods_are_rejected_before_starting_timers(
    tmp_path: Path, source: str
) -> None:
    project = VfyProject(tmp_path)
    parameter = (
        "@pytest.mark.parametrize('value', [\n"
        "    pytest.param(0, marks=pytest.mark.timeout(0.1, method='thread')), 1])\n"
        if source == "parameter"
        else ""
    )
    arguments = "value" if source == "parameter" else "runtime_timeout"
    project.write(
        "test_runtime.py",
        "import time\nfrom pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture\n"
        "def runtime_timeout(request):\n"
        "    request.node.add_marker(pytest.mark.timeout(0.1, method='thread', func_only=True))\n\n"
        f"{parameter}"
        f"def test_runtime({arguments}):\n"
        "    Path('ran').touch()\n    time.sleep(0.4)\n",
    )
    result = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q", env=_AUTOLOAD
    )
    assert result.returncode != 0
    assert "pytest-timeout's signal method, not the thread method" in result.stdout + result.stderr
    assert not (project.root / "ran").exists()


def test_signal_timer_does_not_fall_back_to_a_thread_in_an_embedded_run(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_embedded.py",
        "import time\nfrom pathlib import Path\n\n"
        "def test_embedded():\n    Path('ran').touch()\n    time.sleep(0.4)\n",
    )
    project.write(
        "embedded.py",
        "from pathlib import Path\nfrom threading import Thread\nimport pytest\n\n"
        "statuses = []\n"
        "def run():\n"
        "    statuses.append(pytest.main(['-p', 'no:cacheprovider', '-p', "
        "'vfy._pytest.native_pytest', '-p', 'pytest_timeout', '--timeout=0.1', '-q']))\n"
        "thread = Thread(target=run)\nthread.start()\nthread.join()\n"
        "assert statuses and statuses[0] != 0, statuses\nPath('survived').touch()\n",
    )
    result = project.run_python("embedded.py")
    result.expect(0)
    assert "signal timers to run in the main thread" in result.stdout + result.stderr
    assert (project.root / "survived").exists()
    assert not (project.root / "ran").exists()


@pytest.mark.parametrize("source", ["option", "ini"])
def test_session_timeout_rejects_native_file_workers_before_collection(
    tmp_path: Path, source: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_session.py",
        "from pathlib import Path\nPath('collected').touch()\ndef test_session():\n    pass\n",
    )
    if source == "ini":
        project.write("pytest.ini", "[pytest]\nsession_timeout = 0.1\n")
    arguments = ("--session-timeout=0.1",) if source == "option" else ()
    result = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=2", "--no-cov", *arguments, "-q", env=_AUTOLOAD
    )
    assert result.returncode != 0
    assert (
        "requires --workers=1 with pytest-timeout session timeouts" in result.stdout + result.stderr
    )
    assert not (project.root / "collected").exists()


def test_failed_protocol_acquisition_cancels_timer_before_reusing_pytest(tmp_path: Path) -> None:
    observed = []
    for route in ("stock", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_case.py", "def test_case():\n    pass\n")
        project.write(
            "reuse.py",
            """import faulthandler
import json
import signal
import sys
from pathlib import Path
import pytest

arguments = ['-p', 'no:cacheprovider', '-p', 'pytest_timeout', '--timeout=0.2',
             '-o', 'faulthandler_timeout=0.1', '-q']
if sys.argv[1] == 'native':
    arguments += ['-p', 'vfy._pytest.native_pytest']
original = faulthandler.dump_traceback_later
def failed_acquisition(*args, **kwargs):
    raise RuntimeError('faulthandler acquisition sentinel')
faulthandler.dump_traceback_later = failed_acquisition
try:
    failed = pytest.main(arguments)
finally:
    faulthandler.dump_traceback_later = original
cancelled = signal.getitimer(signal.ITIMER_REAL)[0] == 0
recovered = pytest.main(arguments)
Path('observed.json').write_text(json.dumps([int(failed), cancelled, int(recovered)]))
""",
        )
        result = project.run_python("reuse.py", route)
        result.expect(0)
        assert "faulthandler acquisition sentinel" in result.stdout + result.stderr
        observed.append(json.loads((project.root / "observed.json").read_text()))
    assert observed[0] == observed[1] == [3, True, 0]


@pytest.mark.parametrize("explicit", [False, True])
def test_project_timeout_module_is_rejected_before_its_hooks_run(
    tmp_path: Path, explicit: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest_timeout.py",
        "from pathlib import Path\ndef pytest_configure(config):\n    Path('configured').touch()\n",
    )
    project.write("test_timeout.py", "def test_timeout():\n    pass\n")
    arguments = ("-p", "pytest_timeout") if explicit else ()
    result = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=1", "--no-cov", *arguments, "-q", env=_AUTOLOAD
    )
    assert result.returncode != 0
    assert "requires the installed upstream pytest-timeout plugin" in result.stdout + result.stderr
    assert not (project.root / "configured").exists()


@pytest.mark.parametrize("installed", ["2.3.1", "2.5.0", "3.0.0", "invalid"])
@pytest.mark.parametrize("explicit", [False, True])
def test_unsupported_timeout_release_fails_before_collection(
    tmp_path: Path, installed: str, explicit: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", "from pathlib import Path\nPath('collected').touch()\n")
    project.write("test_timeout.py", "def test_timeout():\n    pass\n")
    arguments = ["test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q"]
    if explicit:
        arguments.extend(("-p", "pytest_timeout"))
    project.write(
        "run_timeout.py",
        "from importlib.metadata import Distribution\n"
        "from vfy.cli import main\n"
        "original = Distribution.version.fget\n"
        "def installed_version(distribution):\n"
        "    if distribution.metadata['Name'].lower() == 'pytest-timeout':\n"
        f"        return {installed!r}\n"
        "    return original(distribution)\n"
        "Distribution.version = property(installed_version)\n"
        f"raise SystemExit(main({arguments!r}))\n",
    )
    result = project.run_python("run_timeout.py", env=_AUTOLOAD)
    result.expect(1)
    assert "native pytest engine requires pytest-timeout>=2.4,<2.5" in result.stderr
    assert not (project.root / "collected").exists()
