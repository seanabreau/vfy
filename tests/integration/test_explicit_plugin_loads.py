"""Explicit pytest-cov and built-in plug-in loads that vfy runs or replaces."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Unix")

_SOURCE = "def double(value):\n    return value * 2\n"
_TEST = "from sample import double\n\ndef test_double():\n    assert double(2) == 4\n"


@pytest.mark.parametrize("engine", ["pytest-native", "pytest"])
@pytest.mark.parametrize("coverage", [True, False])
def test_configured_pytest_cov_load_is_replaced_by_vfy_coverage(
    tmp_path: Path, engine: str, coverage: bool
) -> None:
    project = VfyProject(tmp_path)
    # aio-libs projects force-load pytest-cov this way.
    project.write("pytest.ini", "[pytest]\naddopts = -p pytest_cov --cov=sample -ra\n")
    project.write("sample.py", _SOURCE)
    project.write("test_sample.py", _TEST)
    result = project.run_vfy(
        "test",
        "--all",
        f"--engine={engine}",
        *([] if coverage else ["--no-cov"]),
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None},
    )
    result.expect(0)
    assert "1 passed" in result.stdout
    assert "already added" not in result.stdout + result.stderr
    if coverage:
        assert project.lines("sample.py", ".coverage") == [1, 2]
    else:
        assert not (project.root / ".coverage").exists()


@pytest.mark.parametrize("name", ["warnings", " warnings "])
def test_builtin_plugin_short_name_runs_natively(tmp_path: Path, name: str) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", f"[pytest]\naddopts = -p {shlex.quote(name)}\n")
    project.write("sample.py", _SOURCE)
    project.write("test_sample.py", _TEST)
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    result = project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
    result.expect(0)
    assert "1 passed" in result.stdout


@pytest.mark.parametrize("engine", ["pytest-native", "pytest"])
def test_pytest_cov_load_from_environment_is_reported(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write("sample.py", _SOURCE)
    project.write("test_sample.py", _TEST)
    result = project.run_vfy(
        "test", f"--engine={engine}", "--no-cov", "-q", env={"PYTEST_ADDOPTS": "-p pytest_cov"}
    )
    assert result.returncode != 0
    assert "remove -p pytest_cov from PYTEST_ADDOPTS" in result.stderr


def test_explicit_pytest_cov_request_is_reported_as_replaced(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("sample.py", _SOURCE)
    project.write("test_sample.py", _TEST)
    diagnostic = project.run_vfy(
        "diagnostics", "--engine=pytest-native", "--json", "--", "-p", "pytest_cov"
    )
    diagnostic.expect(0)
    inventory = {
        row["name"]: row for row in json.loads(diagnostic.stdout)["pytest_plugin_inventory"]
    }
    assert inventory["pytest_cov"]["native_status"] == "replaced"
    result = project.run_vfy("test", "--all", "--engine=pytest-native", "-q", "-p", "pytest_cov")
    result.expect(0)
    assert project.lines("sample.py", ".coverage") == [1, 2]


@pytest.mark.parametrize("engine", ["pytest-native", "pytest"])
@pytest.mark.parametrize(
    "plugin_arguments", [("-ppytest_cov",), ("-p", "pytest_cov.plugin"), ("-p", " pytest_cov ")]
)
def test_explicit_pytest_cov_forms_match_upstream_collection(
    tmp_path: Path, engine: str, plugin_arguments: tuple[str, ...]
) -> None:
    observations = []
    for runner in ("upstream", "vfy"):
        project = VfyProject(tmp_path / runner)
        project.write("sample.py", _SOURCE)
        project.write("test_sample.py", _TEST)
        arguments = ["-q", "--cov=sample", *plugin_arguments]
        result = (
            project.run_pytest(*arguments, load_xdist=False)
            if runner == "upstream"
            else project.run_vfy("test", "--all", f"--engine={engine}", *arguments)
        )
        result.expect(0)
        assert "1 passed" in result.stdout
        observations.append(project.lines("sample.py"))
    assert observations == [[1, 2], [1, 2]]


@pytest.mark.parametrize("engine", ["pytest-native", "pytest"])
@pytest.mark.parametrize(
    ("plugin_arguments", "diagnostic"),
    [
        pytest.param(
            ("--plugin", "pytest_cov"), "unrecognized arguments: --plugin", id="long-pair"
        ),
        pytest.param(
            ("--plugin=pytest_cov",),
            "unrecognized arguments: --plugin=pytest_cov",
            id="long-equals",
        ),
        pytest.param(("-p=pytest_cov",), 'Error importing plugin "=pytest_cov"', id="short-equals"),
    ],
)
def test_invalid_pytest_cov_spellings_are_rejected_like_pytest(
    tmp_path: Path, engine: str, plugin_arguments: tuple[str, ...], diagnostic: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_sample.py",
        "from pathlib import Path\n\ndef test_sample():\n    Path('executed').touch()\n",
    )
    stock = project.run_pytest("-q", *plugin_arguments, load_xdist=False)
    assert stock.returncode != 0, stock.stdout + stock.stderr
    assert diagnostic in stock.stderr
    result = project.run_vfy("test", f"--engine={engine}", "--no-cov", "-q", *plugin_arguments)
    assert result.returncode == stock.returncode, result.stdout + result.stderr
    assert diagnostic in result.stderr
    assert not (project.root / "executed").exists()


@pytest.mark.parametrize("source", ["config", "environment", "cli"])
def test_sanitized_addopts_preserve_overrides_and_quoted_values(
    tmp_path: Path, source: str
) -> None:
    addopts = shlex.join(
        [
            "-p",
            " pytest_cov ",
            "--cov=sample",
            "-k",
            "double or missing",
            "-o",
            "cache_dir=my cache",
        ]
    )
    configured_cache = []
    for runner in ("upstream", "vfy"):
        project = VfyProject(tmp_path / runner)
        project.write("sample.py", _SOURCE)
        project.write("test_sample.py", _TEST + "\ndef test_unselected():\n    assert False\n")
        project.write(
            "pytest.ini", "[pytest]\naddopts = " + (addopts if source == "config" else "--no-cov")
        )
        arguments = ["-q"]
        environment = {}
        if source == "environment":
            environment["PYTEST_ADDOPTS"] = shlex.join(["-o", f"addopts={addopts}"])
        elif source == "cli":
            # The final CLI override wins over both config and environment.
            environment["PYTEST_ADDOPTS"] = "-o addopts=--no-cov"
            arguments.extend(["-o", "addopts=-k unselected", "-o", f"addopts={addopts}"])
        result = (
            project.run_pytest(*arguments, env=environment, load_xdist=False)
            if runner == "upstream"
            else project.run_vfy("test", *arguments, env=environment)
        )
        result.expect(0)
        assert "1 passed, 1 deselected" in result.stdout
        assert project.lines("sample.py") == [1, 2]
        configured_cache.append((project.root / "my cache").is_dir())
    assert configured_cache[0] == configured_cache[1]
