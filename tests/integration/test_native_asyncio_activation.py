"""Keep built-in asyncio support separate from requested plugin warnings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

pytestmark = requires_native_pytest

_SOURCE = "from pathlib import Path\ndef test_sync():\n    Path('ran.txt').touch()\n"


def test_builtin_asyncio_does_not_add_scope_warnings_to_synchronous_tests(tmp_path: Path) -> None:
    for engine in ("stock", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nfilterwarnings = error\n")
        project.write("test_sync.py", _SOURCE)
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "stock"
            else project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (project.root / "ran.txt").exists()


@pytest.mark.parametrize("location", ["argument", "environment"])
@pytest.mark.parametrize("warning_policy", ["configured", "interpreter"])
def test_explicit_asyncio_adapter_keeps_its_unset_scope_warning(
    tmp_path: Path, location: str, warning_policy: str
) -> None:
    observations: dict[str, tuple[int, bool, bool]] = {}
    for engine in ("stock", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nfilterwarnings = error\n")
        project.write("test_sync.py", _SOURCE)
        arguments = ["-p", "vfy.asyncio_plugin"] if location == "argument" else []
        environment = {"PYTEST_PLUGINS": "vfy.asyncio_plugin"} if location == "environment" else {}
        if warning_policy == "interpreter":
            environment["PYTHONWARNINGS"] = "error"
        result = (
            project.run_pytest("-q", *arguments, load_xdist=False, env=environment)
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=1",
                "--no-cov",
                "-q",
                *arguments,
                env=environment,
            )
        )
        assert result.returncode in {0, 3}, result.stdout + result.stderr
        observations[engine] = (
            result.returncode,
            "Set asyncio_default_fixture_loop_scope explicitly" in result.stdout + result.stderr,
            (project.root / "ran.txt").exists(),
        )
    assert observations["pytest-native"] == observations["stock"], observations
    if warning_policy == "interpreter":
        status, warned, ran = observations["stock"]
        assert warned, observations
        assert ran == (status == 0), observations


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("warning_policy", ["configured", "interpreter"])
def test_upstream_asyncio_autoload_keeps_its_configuration_warning(
    tmp_path: Path, blocked: bool, warning_policy: str
) -> None:
    pytest.importorskip("pytest_asyncio", reason="requires the upstream asyncio plugin")
    observations: dict[str, tuple[int, bool, bool]] = {}
    for engine in ("stock", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nfilterwarnings = error\n")
        project.write("test_sync.py", _SOURCE)
        arguments = ["-p", "no:asyncio"] if blocked else []
        environment: dict[str, str | None] = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}
        if warning_policy == "interpreter":
            environment["PYTHONWARNINGS"] = "error"
        result = (
            project.run_pytest("-q", *arguments, load_xdist=False, env=environment)
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=1",
                "--no-cov",
                "-q",
                *arguments,
                env=environment,
            )
        )
        assert result.returncode in {0, 3}, result.stdout + result.stderr
        observations[engine] = (
            result.returncode,
            "asyncio_default_fixture_loop_scope" in result.stdout + result.stderr,
            (project.root / "ran.txt").exists(),
        )
    assert observations["pytest-native"] == observations["stock"], observations
    if blocked:
        assert observations["stock"] == (0, False, True), observations
    elif warning_policy == "interpreter":
        assert observations["stock"] == (3, True, False), observations


def test_native_mutation_baseline_uses_builtin_asyncio_without_configuration_warnings(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nfilterwarnings = error\n")
    source = "def value():\n    return 1\n"
    project.write("app.py", source)
    project.write(
        "test_sync.py", "from app import value\ndef test_sync():\n    assert value() == 1\n"
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    result = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--max-mutations",
        "1",
        "--no-cache",
        "--format",
        "json",
    )
    result.expect(0)
    assert json.loads(result.stdout)["summary"] == {"protected": 1}
    assert (project.root / "app.py").read_text() == source
