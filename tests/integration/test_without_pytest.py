"""vfy commands in an environment where pytest and its dependencies are absent."""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import CommandResult, VfyProject

# Make pytest, its plug-ins, pluggy, and packaging unimportable in vfy and in
# every child process it starts, as in an installation without the pytest extra.
_SITECUSTOMIZE = """\
import importlib.abc
import importlib.metadata
import sys

ABSENT = {"pytest", "_pytest", "pluggy", "packaging", "iniconfig", "xdist"}


def _absent(name):
    name = name.lower().replace("_", "-")
    return name in ABSENT or name.startswith("pytest-")


class _WithoutAbsent(importlib.abc.MetaPathFinder):
    def __init__(self, finder):
        self.finder = finder

    def find_spec(self, name, path=None, target=None):
        top = name.partition(".")[0]
        if top in ABSENT or top.startswith("pytest_"):
            return None
        return self.finder.find_spec(name, path, target)

    def invalidate_caches(self):
        getattr(self.finder, "invalidate_caches", lambda: None)()

    def find_distributions(self, *args, **kwargs):
        return getattr(self.finder, "find_distributions", lambda *args, **kwargs: ())(
            *args, **kwargs
        )


_discover = importlib.metadata.Distribution.discover.__func__


def _discover_present(cls, **kwargs):
    for distribution in _discover(cls, **kwargs):
        if not _absent(distribution.metadata["Name"]):
            yield distribution


sys.meta_path[:] = [_WithoutAbsent(finder) for finder in sys.meta_path]
importlib.metadata.Distribution.discover = classmethod(_discover_present)
"""
# Like VfyProject, use the active installation and the checkout only without one.
_SOURCE = [] if importlib.util.find_spec("vfy") else [Path(__file__).parents[2] / "python"]


def _run_vfy_without_pytest(
    project: VfyProject, *arguments: str, env: dict[str, str | None] | None = None
) -> CommandResult:
    site = project.root.parent / f".{project.root.name}-without-pytest"
    site.mkdir(exist_ok=True)
    (site / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    path = os.pathsep.join(str(entry) for entry in (site, *_SOURCE))
    return project.run_vfy(*arguments, env={**(env or {}), "PYTHONPATH": path})


# Installed plug-ins load as they do for a user, so the replacements stand in for them.
_AUTOLOAD: dict[str, str | None] = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}


def test_coverage_commands_run_without_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def branch(flag):\n    if flag:\n        return 1\n    return 2\n")
    project.write("main.py", "import app\n\napp.branch(True)\n")

    run = _run_vfy_without_pytest(project, "coverage", "run", "--source=.", "main.py")
    report = _run_vfy_without_pytest(
        project, "coverage", "report", "--include=app.py", "--show-missing"
    )

    run.expect(0)
    report.expect(0)
    assert project.lines("app.py") == [1, 2, 3]
    assert report.stdout.splitlines()[2].split() == ["app.py", "4", "1", "75%", "4"]


@requires_native_pytest
def test_test_command_runs_the_independent_engine_without_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_example.py",
        "import pytest\n\n\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_value(value):\n    assert value in (1, 2)\n\n\n"
        "def test_failure():\n    assert 1 == 2\n",
    )

    result = _run_vfy_without_pytest(project, "test", "--no-cov", "-q")

    result.expect(1)
    assert "FAILED tests/test_example.py::test_failure - assert 1 == 2" in result.stdout
    assert result.stdout.splitlines()[-1].startswith("1 failed, 2 passed")


def test_pytest_engines_name_missing_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("tests/test_example.py", "def test_example():\n    assert True\n")

    result = _run_vfy_without_pytest(project, "test", "--engine=pytest-native", "--no-cov")

    assert result.returncode == 1
    expected = (
        "native pytest is supported only on Linux and macOS"
        if sys.platform == "win32"
        else (
            "pytest is not installed; install vfy[pytest] "
            "to use --engine=pytest or --engine=pytest-native"
        )
    )
    assert result.stderr == f"vfy: error: native pytest cannot run this workflow: {expected}\n"


def _final_line(result: CommandResult) -> str:
    return re.sub(r" in [0-9.]+s.*$", "", result.stdout.splitlines()[-1])


@requires_native_pytest
def test_plugin_replacements_match_pytest_with_the_plugins(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini",
        "[pytest]\nasyncio_mode = auto\nasyncio_default_fixture_loop_scope = function\n"
        "timeout = 30\n",
    )
    project.write(
        "test_plugins.py",
        """import os
import pytest
from pytest_mock import MockerFixture


def test_patch(mocker: MockerFixture):
    mocker.patch("os.getcwd", return_value="/patched")
    assert os.getcwd() == "/patched"


def test_patch_is_undone():
    assert os.getcwd() != "/patched"


def test_spy(mocker):
    spy = mocker.spy(os.path, "join")
    assert os.path.join("a", "b") == os.path.join("a", "b")
    assert spy.call_count == 2
    assert spy.spy_return == os.path.join("a", "b")


def test_benchmark(benchmark):
    assert benchmark(sum, [1, 2, 3]) == 6


@pytest.mark.timeout(5)
def test_timeout_marker():
    pass


async def test_asyncio_auto():
    assert True


def test_failure():
    assert 1 == 2
""",
    )

    stock = project.run_pytest(
        "-q", "-p", "no:cacheprovider", "--durations=2", env=_AUTOLOAD, load_xdist=False
    )
    vfy = _run_vfy_without_pytest(project, "test", "--no-cov", "-q", "--durations=2", env=_AUTOLOAD)

    assert vfy.returncode == stock.returncode == 1, vfy.stdout + vfy.stderr
    assert _final_line(vfy) == _final_line(stock) == "1 failed, 6 passed"
    assert "slowest 2 durations" in vfy.stdout


@requires_native_pytest
def test_anyio_plugin_runs_marked_tests_without_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_async.py",
        """import anyio
import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_sleep():
    await anyio.sleep(0)


@pytest.mark.anyio
async def test_backend(anyio_backend_name):
    assert anyio_backend_name == "asyncio"
""",
    )

    stock = project.run_pytest("-q", "-p", "no:cacheprovider", env=_AUTOLOAD, load_xdist=False)
    vfy = _run_vfy_without_pytest(project, "test", "--no-cov", "-q", env=_AUTOLOAD)

    assert vfy.returncode == stock.returncode == 0, vfy.stdout + vfy.stderr
    assert _final_line(vfy) == _final_line(stock) == "2 passed"


@requires_native_pytest
def test_traceback_styles_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_fail.py",
        "def helper():\n    raise ValueError('bad value')\n\n\ndef test_fail():\n    helper()\n",
    )

    for style in ("native", "line", "no"):
        stock = project.run_pytest(
            "-q", "-p", "no:cacheprovider", f"--tb={style}", load_xdist=False
        )
        vfy = _run_vfy_without_pytest(project, "test", "--no-cov", "-q", f"--tb={style}")

        assert vfy.returncode == stock.returncode == 1, vfy.stdout + vfy.stderr
        assert _without_runner_frames(vfy, project) == _without_runner_frames(stock, project), style


def _without_runner_frames(result: CommandResult, project: VfyProject) -> list[str]:
    # pytest's native traceback starts in pytest's own runner frames, which a
    # run without pytest does not have; keep the project's frames and messages.
    lines = []
    for line in result.stdout.splitlines()[:-1]:
        if line.startswith("  File ") and str(project.root) not in line:
            continue
        if line.startswith("    "):
            continue
        lines.append(line)
    return lines
