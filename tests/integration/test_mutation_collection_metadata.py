"""Import-time calls require complete mutation replays for cached consumers."""

from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

ENGINE_CASES = (
    pytest.param("pytest-native", marks=requires_native_pytest),
    pytest.param(
        "native",
        marks=pytest.mark.skipif(
            sys.platform != "linux", reason="independent mutations require Linux"
        ),
    ),
)


@pytest.mark.parametrize("engine", ENGINE_CASES)
def test_hardlink_body_mutation_replays_cached_alias_consumers(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    source = "def value():\n    return 1\n"
    path = project.write("app.py", source)
    os.link(path, tmp_path / "alias.py")
    project.write("cache.py", "import alias\ncached = alias.value()\n")
    project.write(
        "test_app.py",
        "import app, cache\ndef test_calls():\n    assert app.value() > 0\n"
        "def test_cached():\n    assert cache.cached == 1\n",
    )
    clean = project.run_python("-B", "-m", "pytest", "-q")
    clean.expect(0)
    path.write_text(source.replace("return 1", "return 2"))
    changed = project.run_python("-B", "-m", "pytest", "-q", "--junitxml=stock-mutant.xml")
    changed.expect(1)
    cases = ET.parse(tmp_path / "stock-mutant.xml").getroot().findall(".//testcase")
    assert [case.get("name") for case in cases if case.find("failure") is not None] == [
        "test_cached"
    ]
    assert all(case.find("error") is None for case in cases)
    path.write_text(source)

    completed = project.run_vfy(
        "mutate",
        f"--engine={engine}",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--no-cache",
        "--test-isolation=serial",
        "--format=json",
    )
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}, report
    assert (
        report["mutations"][0]["tests"],
        report["mutations"][0]["killer"],
        report["mutations"][0]["phase"],
    ) == ([], "test_app.py::test_cached", "call"), report
    assert report["metrics"]["fast_results"] == 0, report
    assert report["metrics"]["portable_fallbacks"] == 1, report


@pytest.mark.parametrize("engine", ENGINE_CASES)
@pytest.mark.parametrize("direct_call", [False, True], ids=["cached-only", "also-called"])
@pytest.mark.parametrize("relative", [False, True], ids=["absolute-files", "relative-files"])
def test_imported_body_mutation_replays_cached_global_consumers(
    tmp_path: Path, engine: str, direct_call: bool, relative: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(".coveragerc", f"[run]\nrelative_files = {relative}\n")
    source = "def value():\n    return 1\n\ncached = value()\n"
    project.write("app.py", source)
    project.write(
        "test_app.py",
        "import app\n\n"
        + ("def test_calls():\n    assert app.value() > 0\n\n" if direct_call else "")
        + "def test_cached():\n    assert app.cached == 1\n",
    )
    clean = project.run_python("-B", "-m", "pytest", "-q")
    clean.expect(0)
    project.write("app.py", source.replace("return 1", "return 2"))
    changed = project.run_python("-B", "-m", "pytest", "-q", "--junitxml=stock-mutant.xml")
    changed.expect(1)
    cases = ET.parse(tmp_path / "stock-mutant.xml").getroot().findall(".//testcase")
    assert [case.get("name") for case in cases if case.find("failure") is not None] == [
        "test_cached"
    ]
    assert all(case.find("error") is None for case in cases)
    project.write("app.py", source)

    completed = project.run_vfy(
        "mutate",
        f"--engine={engine}",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--no-cache",
        "--test-isolation=serial",
        "--format=json",
        env={"PYTEST_NATIVE_RESULT": str(tmp_path / "baseline.json")},
    )
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}, report
    mutation = report["mutations"][0]
    assert (mutation["killer"], mutation["phase"], mutation["tests"]) == (
        "test_app.py::test_cached",
        "call",
        [],
    ), report
    assert report["metrics"]["fast_results"] == 0, report
    assert report["metrics"]["portable_fallbacks"] == 1, report
    baseline = json.loads((tmp_path / "baseline.json").read_text())
    assert baseline["collected"] == (2 if direct_call else 1), baseline
    assert all(
        phase["outcome"] == "passed" for case in baseline["cases"] for phase in case["phases"]
    ), baseline


@pytest.mark.parametrize("engine", ENGINE_CASES)
def test_relative_files_keep_isolated_mutations_on_the_selected_fast_path(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(".coveragerc", "[run]\nrelative_files = True\nbranch = True\n")
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "test_app.py",
        "import app\n\ndef test_calls():\n    assert app.value() == 1\n\n"
        "def test_unrelated():\n    assert True\n",
    )
    clean = project.run_python("-B", "-m", "pytest", "-q")
    clean.expect(0)
    result = project.run_vfy(
        "mutate",
        f"--engine={engine}",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--no-cache",
        "--test-isolation=serial",
        "--format=json",
    )
    result.expect(0)
    report = json.loads(result.stdout)
    assert report["summary"] == {"protected": 1}, report
    assert report["mutations"][0]["tests"] == ["test_app.py::test_calls"], report
    assert report["metrics"]["fast_results"] == 1, report
    assert report["metrics"]["portable_fallbacks"] == 0, report


@pytest.mark.parametrize("engine", ENGINE_CASES)
def test_collection_only_is_not_a_successful_mutation_baseline(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n\ncached = value()\n")
    project.write("test_app.py", "import app\n\ndef test_cached():\n    assert app.cached == 1\n")
    completed = project.run_vfy(
        "mutate",
        f"--engine={engine}",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--collect-only",
        "--format=json",
    )
    completed.expect(1)
    assert "baseline" in completed.stderr
    assert not (tmp_path / ".vfy/mutations.json").exists()
    assert not (tmp_path / ".vfy/mutation-baseline.json").exists()
