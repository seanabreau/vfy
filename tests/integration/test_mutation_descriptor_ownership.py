"""Descriptor decorators can retain callable state outside the descriptor."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


@pytest.mark.parametrize(
    "engine",
    [
        pytest.param("pytest-native", marks=requires_native_pytest),
        pytest.param(
            "native",
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="independent mutations require Linux"
            ),
        ),
    ],
)
def test_body_mutation_replays_callable_state_retained_by_descriptor_decorator(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    source = (
        "import builtins\n"
        "from types import FunctionType\n\n"
        "def retain(function):\n"
        "    global alias\n"
        "    alias = FunctionType(function.__code__, function.__globals__)\n"
        "    return getattr(builtins, 'staticmethod')(function)\n\n"
        "globals()['staticmethod'] = retain\n\n"
        "class Service:\n"
        "    @staticmethod\n"
        "    def value():\n"
        "        return 1\n\n"
        "globals().pop('staticmethod')\n"
        "del retain\n"
    )
    path = project.write("app.py", source)
    project.write(
        "test_app.py",
        "import app\n\ndef test_retained_alias():\n    assert app.alias() == 1\n",
    )

    clean = project.run_python("-B", "-m", "pytest", "-q")
    clean.expect(0)
    path.write_text(source.replace("return 1", "return 2"))
    changed = project.run_python("-B", "-m", "pytest", "-q", "--junitxml=stock-mutant.xml")
    changed.expect(1)
    cases = ET.parse(tmp_path / "stock-mutant.xml").getroot().findall(".//testcase")
    assert [case.get("name") for case in cases if case.find("failure") is not None] == [
        "test_retained_alias"
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
        env={"VFY_MUTATION_ENGINE": "auto"},
    )
    assert completed.stdout, completed
    project.write("mutation-result.json", completed.stdout)
    report = json.loads(completed.stdout)
    assert completed.returncode == 0, (completed.stderr, report)
    assert report["summary"] == {"protected": 1}, report
    mutation = report["mutations"][0]
    assert (mutation["killer"], mutation["phase"]) == (
        "test_app.py::test_retained_alias",
        "call",
    ), report
    assert report["metrics"]["fast_results"] == 0, report
    assert report["metrics"]["portable_fallbacks"] == 1, report
