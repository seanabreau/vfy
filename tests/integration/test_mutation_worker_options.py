"""Mutation workers replace inherited pytest concurrency without nested workers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


@pytest.mark.parametrize("workers", ["0", "257"])
def test_mutation_rejects_worker_counts_outside_its_range(tmp_path: Path, workers: str) -> None:
    result = VfyProject(tmp_path).run_vfy("mutate", f"--workers={workers}")

    result.expect(2)
    assert "workers must be auto, logical, or an integer from 1 to 256" in result.stderr


@pytest.mark.parametrize(
    "engine",
    [
        pytest.param("pytest-native", marks=requires_native_pytest),
        pytest.param(
            "native",
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="independent mutation execution requires Linux"
            ),
        ),
    ],
)
@pytest.mark.parametrize("origin", ["config", "environment", "explicit-serial"])
def test_mutations_override_inherited_pytest_workers(
    tmp_path: Path, engine: str, origin: str
) -> None:
    _check_worker_mutation(tmp_path, engine, origin)


@pytest.mark.skipif(sys.platform != "linux", reason="independent mutation execution requires Linux")
def test_independent_mutation_worker_plugin_keeps_its_autoload_registration(tmp_path: Path) -> None:
    _check_worker_mutation(tmp_path, "native", "config", autoload=True)


def _check_worker_mutation(
    tmp_path: Path, engine: str, origin: str, *, autoload: bool = False
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_app.py",
        "from app import value\n\n"
        "def test_value(pytestconfig):\n"
        "    assert pytestconfig.getoption('numprocesses') == 0\n"
        "    assert not hasattr(pytestconfig, 'workerinput')\n"
        "    assert value() == 1\n",
    )
    project.write("pytest.ini", "[pytest]\naddopts = --strict-markers\n")
    environment: dict[str, str | None] = {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": None if autoload else "1"
    }
    arguments: tuple[str, ...] = ()
    if origin == "environment":
        environment["PYTEST_ADDOPTS"] = "--numprocesses=auto --dist=worksteal"
    else:
        project.write(
            "pytest.ini", "[pytest]\naddopts = -n auto --dist=worksteal --strict-markers\n"
        )
        if origin == "explicit-serial":
            arguments = ("-p", "xdist.plugin", "--numprocesses=0")
    result = project.run_vfy(
        "mutate",
        f"--engine={engine}",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--format=json",
        "--test-isolation=serial",
        "-j2" if engine == "pytest-native" else "-j1",
        "-q",
        *arguments,
        "tests",
        env=environment,
    )
    result.expect(0)
    report = json.loads(result.stdout)
    assert report["summary"] == {"protected": 1}
    mutation = report["mutations"][0]
    assert (mutation["phase"], mutation["killer"]) == ("call", "tests/test_app.py::test_value")


@requires_native_pytest
def test_explicit_parallel_pytest_workers_fail_before_mutation_evidence(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = -n auto\n")
    project.write("app.py", "def value():\n    return 1\n")
    project.write("tests/test_app.py", "raise AssertionError('must not collect')\n")
    result = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source=app.py",
        "--numprocesses=2",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(1)
    assert "controls mutation workers" in result.stderr
    assert "must not collect" not in result.stdout + result.stderr
    assert not (tmp_path / ".vfy").exists()
