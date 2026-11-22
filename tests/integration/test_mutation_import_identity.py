"""Portable mutations preserve filesystem identity across module aliases."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support.platforms import requires_independent_mutation, requires_native_pytest
from tests.support.vfy_project import VfyProject


@requires_independent_mutation
@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_portable_definition_mutation_applies_through_source_alias(
    tmp_path: Path, alias_kind: str
) -> None:
    project = VfyProject(tmp_path)
    source = project.write("app.py", "def value(argument=1):\n    return argument\n")
    alias = tmp_path / "alias.py"
    if alias_kind == "symlink":
        alias.symlink_to(source.name)
    else:
        os.link(source, alias)
    project.write(
        "tests/test_app.py",
        "import alias\n\ndef test_default():\n    assert alias.value.__defaults__ == (1,)\n",
    )

    completed = project.run_vfy(
        "mutate",
        "--engine=native",
        "--all",
        "--source",
        ".",
        "--operator",
        "integer_plus_one",
        "--max-mutations",
        "1",
        "--no-cache",
        "--format",
        "json",
        "app.py:1",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "VFY_MUTATION_ENGINE": "portable"},
    )

    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}
    mutation = report["mutations"][0]
    assert mutation["owner"]["phase"] == "definition"
    assert mutation["killer"] == "tests/test_app.py::test_default"
    assert source.read_bytes() == b"def value(argument=1):\n    return argument\n"


@pytest.mark.skipif(os.name != "posix", reason="source aliases require POSIX links")
@requires_native_pytest
@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_collected_mutation_preserves_shared_source_aliases(
    tmp_path: Path, alias_kind: str
) -> None:
    project = VfyProject(tmp_path)
    source = project.write("app.py", "def value():\n    return 1\n")
    alias = tmp_path / "alias.py"
    if alias_kind == "symlink":
        alias.symlink_to(source.name)
    else:
        os.link(source, alias)
    project.write(
        "tests/test_app.py",
        "import app, alias\n\ndef test_value():\n    assert app.value() == alias.value()\n",
    )
    completed = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--no-cache",
        "--test-isolation",
        "serial",
        "--format",
        "json",
        "-j",
        "1",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"survived": 1}, report
    assert source.read_bytes() == b"def value():\n    return 1\n"
    assert alias.read_bytes() == source.read_bytes()
