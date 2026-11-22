"""Plugins register real arguments before the independent parser validates them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()


@pytest.mark.parametrize("arguments", [["--flavour=cli"], ["--flavour", "cli"]])
def test_registered_plugin_arguments_follow_ini_env_cli_precedence(
    tmp_path: Path, arguments: list[str]
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = --flavour=ini\n")
    project.write(
        "conftest.py",
        """def pytest_addoption(parser):
    parser.addoption('--flavour', choices=['ini', 'env', 'cli'], default='default')
""",
    )
    project.write(
        "test_options.py",
        """import json
from pathlib import Path
def test_value(request):
    Path('value.json').write_text(json.dumps(request.config.getoption('--flavour')))
""",
    )
    for module in ["pytest", "vfy.independent.entry"]:
        result = project.run_python(
            "-m",
            module,
            *arguments,
            env={"PYTHONPATH": _SOURCE, "PYTEST_ADDOPTS": "--flavour=env"},
        )
        result.expect(0)
        assert json.loads((project.root / "value.json").read_text()) == "cli"


@pytest.mark.parametrize("arguments", [["--flavour=bad"], ["--unknown"]])
def test_invalid_plugin_arguments_are_usage_errors(tmp_path: Path, arguments: list[str]) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "def pytest_addoption(parser):\n    parser.addoption('--flavour', choices=['valid'])\n",
    )
    project.write("test_options.py", "def test_body():\n    raise AssertionError('not run')\n")
    for module in ["pytest", "vfy.independent.entry"]:
        result = project.run_python("-m", module, *arguments, env={"PYTHONPATH": _SOURCE})
        result.expect(4)


def test_duplicate_plugin_options_remain_errors_within_one_invocation(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    for name in ["first", "second"]:
        project.write(
            f"{name}.py",
            "def pytest_addoption(parser):\n"
            f"    parser.getgroup('{name}').addoption('--flavour')\n",
        )
    project.write("test_options.py", "def test_body(): raise AssertionError('not run')\n")
    for module in ["pytest", "vfy.independent.entry"]:
        result = project.run_python(
            "-m", module, "-p", "first", "-p", "second", env={"PYTHONPATH": _SOURCE}
        )
        assert result.returncode != 0, (result.stdout, result.stderr)
        assert "argument --flavour: conflicting option string: --flavour" in (
            result.stdout + result.stderr
        )


@pytest.mark.parametrize("configure_fails", [False, True])
def test_configuration_cleanup_drains_after_unconfigure_even_on_failure(
    tmp_path: Path, configure_fails: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"""from pathlib import Path

def event(text):
    with Path('events').open('a') as output:
        output.write(text + '\\n')

def pytest_configure(config):
    config.add_cleanup(lambda: event('first'))
    config.add_cleanup(lambda: event('second'))
    if {configure_fails!r}:
        raise RuntimeError('configure failed')

def pytest_unconfigure(config):
    event('unconfigure')
""",
    )
    project.write("test_cleanup.py", "def test_pass(): pass\n")
    oracle = project.run_python("-m", "pytest", env={"PYTHONPATH": _SOURCE})
    expected = (project.root / "events").read_text()
    (project.root / "events").unlink()
    actual = project.run_python("-m", "vfy.independent.entry", env={"PYTHONPATH": _SOURCE})
    if not configure_fails:
        assert oracle.returncode == actual.returncode == 0, actual.stdout + actual.stderr
    else:
        assert oracle.returncode != 0
        assert actual.returncode != 0
    assert (project.root / "events").read_text() == expected == "unconfigure\nsecond\nfirst\n"
