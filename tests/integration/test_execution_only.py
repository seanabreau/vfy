"""Execution-only test runs preserve coverage data and pytest option precedence."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from coverage import CoverageData

from tests.support import VfyProject
from tests.support.history import read_history


@pytest.mark.parametrize(
    ("coverage_data", "no_cov_source", "arguments"),
    [
        ("invalid", "command", ()),
        ("large", "config", ()),
        ("invalid", "environment", ("--engine=pytest",)),
        ("invalid", "command", ("--all",)),
    ],
)
def test_execution_only_preserves_prior_coverage_without_reading_replay_inputs(
    tmp_path: Path,
    coverage_data: str,
    no_cov_source: str,
    arguments: tuple[str, ...],
) -> None:
    if not sys.platform.startswith(("linux", "darwin")):
        arguments = ("--engine=pytest", *arguments)
    project = VfyProject(tmp_path)
    project.write("test_example.py", "def test_example():\n    assert True\n")
    data_path = tmp_path / ".coverage"
    if coverage_data == "large":
        data = CoverageData(basename=str(data_path))
        try:
            data.add_lines({f"old/source_{index}.py": set(range(1, 101)) for index in range(5_000)})
            data.write()
        finally:
            data.close(force=True)
    else:
        data_path.write_bytes(b"invalid coverage database\n")
    before = data_path.read_bytes()
    probe = project.write(
        "probe/sitecustomize.py",
        "from pathlib import Path\n"
        "import vfy.native as native\n"
        "import vfy.dependency_fingerprint as dependencies\n"
        "from vfy.data import CoverageStore\n"
        "def forbidden(*args, **kwargs):\n"
        "    Path('unexpected-replay-input').write_text('replay or coverage input read')\n"
        "    raise AssertionError('execution-only run read a replay input')\n"
        "native.git_replay_state = forbidden\n"
        "dependencies._compute = forbidden\n"
        "CoverageStore.read = classmethod(forbidden)\n"
        "Path('probe-installed').touch()\n",
    )
    environment = {"PYTHONPATH": str(probe.parent), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    pytest_arguments = ["-q"]
    if no_cov_source == "config":
        project.write("pytest.ini", "[pytest]\naddopts = --no-cov\n")
    elif no_cov_source == "environment":
        environment["PYTEST_ADDOPTS"] = "--no-cov"
    else:
        pytest_arguments.append("--no-cov")

    result = project.run_vfy("test", "-j1", *arguments, *pytest_arguments, env=environment)

    result.expect(0)
    assert "1 passed" in result.stdout
    assert (tmp_path / "probe-installed").is_file()
    assert not (tmp_path / "unexpected-replay-input").exists(), result.stdout + result.stderr
    assert data_path.read_bytes() == before
    history = read_history(tmp_path / ".vfy/test-history.sqlite3")
    assert (
        history["tests"]["test_example.py::test_example"]["observations"][-1]["outcome"] == "passed"
    )


@pytest.mark.parametrize(
    ("config_addopts", "environment_addopts", "arguments", "coverage_enabled"),
    [
        ("--no-cov", "", ("--cov",), False),
        ("--cov", "--no-cov", (), False),
        ("--no-cov", "", ("-o", "addopts="), True),
        ("", "", ("--override-ini=addopts=--no-cov",), False),
    ],
)
def test_coverage_enablement_matches_pytest_cov_option_precedence(
    tmp_path: Path,
    config_addopts: str,
    environment_addopts: str,
    arguments: tuple[str, ...],
    coverage_enabled: bool,
) -> None:
    if not sys.platform.startswith(("linux", "darwin")):
        engine_arguments = ("--engine=pytest",)
    else:
        engine_arguments = ()
    project = VfyProject(tmp_path)
    project.write("pytest.ini", f"[pytest]\naddopts = {config_addopts}\n")
    project.write("test_example.py", "def test_example():\n    assert True\n")
    environment = {
        "PYTEST_ADDOPTS": environment_addopts,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "COVERAGE_FILE": str(tmp_path / "reference.coverage"),
    }
    reference = project.run_python(
        "-m", "pytest", "-p", "pytest_cov.plugin", "--cov", "-q", *arguments, env=environment
    )
    reference.expect(0)
    assert (tmp_path / "reference.coverage").exists() is coverage_enabled

    environment["COVERAGE_FILE"] = str(tmp_path / ".coverage")
    result = project.run_vfy("test", "-j1", *engine_arguments, "-q", *arguments, env=environment)
    assert result.returncode == reference.returncode, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert (tmp_path / ".coverage").exists() is coverage_enabled
