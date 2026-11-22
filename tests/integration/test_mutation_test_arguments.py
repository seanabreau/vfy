"""Mutation locations and pytest selectors retain their separate command scopes."""

from __future__ import annotations

import json
import sys
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
@pytest.mark.parametrize("selection", ["files", "node", "directory", "location"])
def test_mutation_forwards_the_first_pytest_selector(
    tmp_path: Path, engine: str, selection: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "selected/test_exact.py",
        "from app import value\n\ndef test_exact():\n    assert value() == 1\n",
    )
    project.write(
        "selected/test_range.py",
        "from app import value\n\ndef test_range():\n    assert value() in (1, 2)\n",
    )
    project.write("test_outside.py", "raise AssertionError('outside the requested scope')\n")
    selectors = (
        ["selected"]
        if selection == "directory"
        else [
            "selected/test_exact.py::test_exact"
            if selection == "node"
            else "selected/test_exact.py",
            "selected/test_range.py",
        ]
    )
    stock = project.run_python("-m", "pytest", "-q", *selectors)
    stock.expect(0)
    assert "2 passed" in stock.stdout

    result = project.run_vfy(
        "mutate",
        f"--engine={engine}",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--format=json",
        "--test-isolation=serial",
        *(["app.py:2"] if selection == "location" else []),
        *selectors,
    )
    result.expect(0)
    report = json.loads(result.stdout)
    assert report["summary"] == {"protected": 1}, report
    assert report["mutations"][0]["killer"] == "selected/test_exact.py::test_exact", report
    baseline = json.loads((tmp_path / ".vfy/mutation-baseline.json").read_text())
    assert baseline["tests"] == [
        "selected/test_exact.py::test_exact",
        "selected/test_range.py::test_range",
    ], baseline
