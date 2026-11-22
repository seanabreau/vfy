"""Source mutations retain registered helper assertion rewriting."""

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
        pytest.param(
            "native",
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="independent mutations use Linux"
            ),
        ),
        pytest.param("pytest-native", marks=requires_native_pytest),
    ],
)
@pytest.mark.parametrize("import_mode", ["prepend", "importlib"])
def test_fresh_mutations_rewrite_helper_assertions_without_reusing_clean_code(
    tmp_path: Path, engine: str, import_mode: str
) -> None:
    project = VfyProject(tmp_path)
    source = (
        "from pathlib import Path\n"
        "def decorate(function):\n"
        "    def wrapper():\n"
        "        return function()\n"
        "    return wrapper\n"
        "@decorate\n"
        "def value():\n"
        "    result = 1\n"
        "    try:\n"
        "        assert result == 1\n"
        "    except AssertionError as error:\n"
        "        Path('assertion-message').write_text(str(error))\n"
        "        raise\n"
        "    return result\n"
    )
    project.write("app.py", source)
    project.write("conftest.py", 'import pytest\npytest.register_assert_rewrite("app")\n')
    project.write(
        "tests/test_app.py", "from app import value\ndef test_value():\n    assert value() == 1\n"
    )
    arguments = (f"--import-mode={import_mode}", "-q")
    clean = project.run_pytest(*arguments, load_xdist=False)
    clean.expect(0)

    result = project.run_vfy(
        "mutate",
        f"--engine={engine}",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--max-mutations=1",
        "--no-cache",
        "--format=json",
        *arguments,
    )
    result.expect(0)
    report = json.loads(result.stdout)
    assert report["summary"] == {"protected": 1}, report
    mutation = report["mutations"][0]
    assert (mutation["phase"], mutation["killer"]) == ("call", "tests/test_app.py::test_value")
    assert report["metrics"]["portable_fallbacks"] == 1, report
    assert (tmp_path / "assertion-message").read_text() == "assert 2 == 1"
    assert (tmp_path / "app.py").read_text() == source
    after = project.run_pytest(*arguments, load_xdist=False)
    after.expect(0)
