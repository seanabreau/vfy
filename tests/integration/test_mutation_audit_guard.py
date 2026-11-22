"""Mutation admission preserves outcomes when audit hooks observe registration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


def _project(
    path: Path, *, static_value: int = 1, plain_value: int = 1, guarded_only: bool = False
) -> VfyProject:
    project = VfyProject(path)
    project.write(
        "sitecustomize.py",
        "import builtins, sys\n"
        "builtins._vfy_registration_count = 0\n"
        "def observe(event, args):\n"
        "    if event == 'sys.addaudithook':\n"
        "        builtins._vfy_registration_count += 1\n"
        "sys.addaudithook(observe)\n",
    )
    project.write(
        "app.py",
        "class App:\n"
        "    @staticmethod\n"
        "    def value():\n"
        f"        return {static_value}\n"
        "def other():\n"
        f"    return {plain_value}\n",
    )
    audit_check = (
        "    assert App.value() == 1 if builtins._vfy_registration_count else App.value() >= 0\n"
        if guarded_only
        else "    assert builtins._vfy_registration_count == 0\n"
    )
    project.write(
        "test_app.py",
        "import builtins\n"
        "from app import App, other\n"
        "def test_values():\n" + audit_check + "    assert App.value() >= 0\n"
        "    assert other() >= 0\n",
    )
    return project


@requires_native_pytest
@pytest.mark.parametrize("guarded_only", [False, True])
def test_mutation_results_match_stock_when_audit_hook_tracks_registrations(
    tmp_path: Path, guarded_only: bool
) -> None:
    for name, static_value, plain_value in (
        ("clean", 1, 1),
        ("static-mutant", 2, 1),
        ("plain-mutant", 1, 2),
    ):
        oracle = _project(
            tmp_path / name,
            static_value=static_value,
            plain_value=plain_value,
            guarded_only=guarded_only,
        )
        stock = oracle.run_pytest(
            "-q",
            load_xdist=False,
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONPATH": str(oracle.root)},
        )
        stock.expect(0)

    project = _project(tmp_path / "mutation", guarded_only=guarded_only)
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
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONPATH": str(project.root)},
    )
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"survived": 2}, report
