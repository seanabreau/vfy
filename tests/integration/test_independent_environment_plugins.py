"""Explicit environment plug-ins load even when entry-point autoload is disabled."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("blocked", [False, True])
def test_environment_plugins_preserve_explicit_blocking_and_fixture_outcomes(
    tmp_path: Path, blocked: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    for name, value in (("first", 1), ("second", 2)):
        project.write(
            f"plugin_{name}.py",
            "from pathlib import Path\nimport pytest\n"
            f"Path('imported-{name}').touch()\n"
            f"@pytest.fixture\ndef {name}():\n    return {value}\n",
        )
    project.write(
        "test_plugins.py",
        "def test_plugins(first, second):\n    assert first + second == 3\n",
    )
    arguments = ("-p", "no:plugin_second") if blocked else ()
    environment = {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_PLUGINS": "plugin_first,plugin_second",
    }
    oracle = project.run_pytest("-q", *arguments, load_xdist=False, env=environment)
    assert oracle.returncode == (1 if blocked else 0), oracle.stdout + oracle.stderr
    expected_imports = {
        name: (tmp_path / f"imported-{name}").exists() for name in ("first", "second")
    }
    assert expected_imports == {"first": True, "second": not blocked}
    for name in ("first", "second"):
        (tmp_path / f"imported-{name}").unlink(missing_ok=True)

    actual = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-q", *arguments, env=environment
    )

    assert actual.returncode == oracle.returncode, actual.stdout + actual.stderr
    assert {
        name: (tmp_path / f"imported-{name}").exists() for name in ("first", "second")
    } == expected_imports
    if blocked:
        assert "fixture 'second' not found" in actual.stdout + actual.stderr
    else:
        assert "1 passed" in actual.stdout
