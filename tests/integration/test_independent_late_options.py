"""Options declared by lazy conftests exist during historic configure replay."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("registration", ["direct", "group"])
@pytest.mark.parametrize("selection", ["lazy", "explicit", "existing"])
def test_deep_option_defaults_precede_configure_and_preserve_existing_values(
    tmp_path: Path,
    engine: Literal["stock", "native"],
    registration: str,
    selection: str,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    existing = (
        "def pytest_configure(config): config.option.late_value = 99\n"
        if selection == "existing"
        else ""
    )
    project.write(
        "conftest.py",
        "from pathlib import Path\n"
        "def pytest_sessionstart(session):\n"
        "    with Path('events').open('a') as output: output.write('sessionstart\\n')\n" + existing,
    )
    option_target = "parser" if registration == "direct" else "parser.getgroup('late')"
    expected_value = {"lazy": 7, "explicit": 8, "existing": 99}[selection]
    project.write(
        "branch/deep/conftest.py",
        f"""from pathlib import Path
with Path('events').open('a') as output: output.write('imported\\n')

def pytest_addoption(parser, pluginmanager):
    assert all(
        implementation.plugin_name != 'pytestconfig'
        for implementation in pluginmanager.hook.pytest_addoption.get_hookimpls()
    ), 'default publication introduced an extra configuration hook'
    {option_target}.addoption('--late-value', type=int, default=7)

def pytest_configure(config):
    value = config.getoption('late_value')
    assert value == {expected_value}
    with Path('events').open('a') as output: output.write('configured ' + str(value) + '\\n')
""",
    )
    project.write(
        "branch/deep/test_value.py",
        f"""from pathlib import Path

def test_value(request):
    value = request.config.getoption('late_value')
    assert value == request.config.option.late_value == {expected_value}
    with Path('events').open('a') as output: output.write('tested ' + str(value) + '\\n')
""",
    )
    arguments = (
        ["branch/deep/test_value.py", "--late-value=8"] if selection == "explicit" else ["."]
    )
    if engine == "stock":
        result = project.run_pytest("-q", *arguments, load_xdist=False)
    else:
        result = project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    result.expect(0)
    configured = ["imported", f"configured {expected_value}"]
    expected_events = (
        [*configured, "sessionstart"] if selection == "explicit" else ["sessionstart", *configured]
    )
    assert (project.root / "events").read_text().splitlines() == [
        *expected_events,
        f"tested {expected_value}",
    ]
