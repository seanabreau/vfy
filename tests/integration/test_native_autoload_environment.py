"""Test bodies see the plug-in autoload setting of the environment vfy started in."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_RECORDING_TEST = (
    "import os\n"
    "from pathlib import Path\n\n"
    "def test_records_autoload():\n"
    "    value = os.environ.get('PYTEST_DISABLE_PLUGIN_AUTOLOAD')\n"
    "    Path('autoload').write_text(repr(value))\n"
    "    assert 'VFY_USER_PYTEST_DISABLE_PLUGIN_AUTOLOAD' not in os.environ\n"
)


@pytest.mark.parametrize(
    "route",
    [
        ("--engine=native",),
        ("--engine=native", "--workers=2"),
        ("--engine=pytest-native", "--numprocesses", "2"),
    ],
    ids=["serial", "workers", "xdist"],
)
@pytest.mark.parametrize("setting", [None, "1"], ids=["unset", "disabled"])
def test_test_bodies_see_the_user_autoload_setting(
    tmp_path: Path, route: tuple[str, ...], setting: str | None
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_autoload.py", _RECORDING_TEST)
    # A second file gives each worker route more than one scheduling unit.
    project.write("test_other.py", "def test_other():\n    assert True\n")

    completed = project.run_vfy(
        "test",
        "--no-cov",
        "-q",
        # The benchmark-only selector requires installed pytest internals.
        "-p",
        "no:pytest-testmon",
        *route,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": setting},
    )

    completed.expect(0)
    assert "2 passed" in completed.stdout
    assert (tmp_path / "autoload").read_text() == repr(setting)
