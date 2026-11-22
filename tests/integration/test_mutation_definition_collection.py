"""Definition mutations affect source imports and pytest collection metadata."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native mutation execution")
_MUTATION_ENGINES = (
    pytest.param(
        "default",
        marks=pytest.mark.skipif(
            sys.platform != "linux", reason="independent mutations require Linux"
        ),
    ),
    pytest.param("auto", id="native-auto"),
    pytest.param("portable", id="native-portable"),
)


def _run_mutation(project: VfyProject, engine: str) -> dict[str, Any]:
    engine_options = () if engine == "default" else ("--engine=pytest-native",)
    completed = project.run_vfy(
        "mutate",
        *engine_options,
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--max-mutations",
        "1",
        "--no-cache",
        "--workers",
        "1" if engine == "default" else "2",
        "--format",
        "json",
        env={
            "VFY_MUTATION_ENGINE": "auto" if engine == "default" else engine,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
    )
    assert completed.stdout, completed
    report = json.loads(completed.stdout)
    assert completed.returncode == 0, (completed.stderr, report)
    result = report["mutations"][0]
    assert result["owner"]["phase"] == "definition", report
    assert result["tests"] == [], report
    return result


@pytest.mark.parametrize("engine", _MUTATION_ENGINES)
def test_mutation_recomputes_values_derived_during_source_import(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "app.py",
        "def value(argument=1):\n    return argument\n\nIMPORTED_DEFAULT = value.__defaults__[0]\n",
    )
    project.write(
        "tests/test_app.py",
        "import app\n\n"
        "def test_imported_default():\n"
        "    assert app.IMPORTED_DEFAULT == 1\n"
        "    assert app.value(1) == 1\n",
    )

    result = _run_mutation(project, engine)

    assert (result["status"], result["killer"], result["phase"]) == (
        "protected",
        "tests/test_app.py::test_imported_default",
        "call",
    ), result


@pytest.mark.parametrize("engine", _MUTATION_ENGINES)
def test_mutation_recollects_parameters_derived_from_default_metadata(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value(argument=1):\n    return argument\n")
    project.write(
        "tests/test_app.py",
        "import pytest\nimport app\n\n"
        "@pytest.mark.parametrize('argument', app.value.__defaults__, ids=['default'])\n"
        "def test_collected_default(argument):\n"
        "    assert argument == 1\n"
        "    assert app.value(1) == 1\n",
    )

    result = _run_mutation(project, engine)

    assert (result["status"], result["killer"], result["phase"]) == (
        "protected",
        "tests/test_app.py::test_collected_default[default]",
        "call",
    ), result


@pytest.mark.parametrize("engine", _MUTATION_ENGINES)
def test_mutation_recomputes_collection_skip_markers(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value(argument=1):\n    return argument\n")
    project.write(
        "conftest.py",
        "from pathlib import Path\nimport pytest\nimport app\n\n"
        "def pytest_collection_modifyitems(items):\n"
        "    if app.value.__defaults__ != (1,):\n"
        "        Path('mutated-collection').touch()\n"
        "        for item in items:\n"
        "            item.add_marker(pytest.mark.skip(reason='default unsupported'))\n",
    )
    project.write(
        "tests/test_app.py",
        "import app\n\ndef test_value():\n    assert app.value() == 1\n",
    )

    result = _run_mutation(project, engine)

    assert (tmp_path / "mutated-collection").exists(), result
    assert (result["status"], result["killer"]) == ("survived", None), result
