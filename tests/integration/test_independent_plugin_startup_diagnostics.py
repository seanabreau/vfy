"""Identify installed plug-ins that prevent the independent engine from starting."""

from __future__ import annotations

import json
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _install_entry_point(
    project: VfyProject, distribution: str, version: str, source: str
) -> tuple[str, str]:
    name = distribution.removeprefix("pytest-")
    module = distribution.replace("-", "_")
    metadata = f"{module}-{version}.dist-info"
    project.write(
        f"{metadata}/METADATA",
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n",
    )
    project.write(f"{metadata}/entry_points.txt", f"[pytest11]\n{name} = {module}\n")
    project.write(f"{module}.py", source)
    return name, module


@pytest.mark.parametrize(
    ("distribution", "version", "source", "operation", "cause"),
    [
        pytest.param(
            "pytest-sugar",
            "1.1.1",
            "from _pytest.main import Session\n",
            "import",
            "ModuleNotFoundError: No module named '_pytest.main'",
            id="sugar-import",
        ),
        pytest.param(
            "pytest-enabler",
            "3.4.0",
            "def pytest_load_initial_conftests(early_config, parser, args):\n"
            "    parser.parse_known_and_unknown_args(args, early_config.known_args_namespace)\n",
            "pytest_load_initial_conftests",
            "AttributeError: 'Parser' object has no attribute 'parse_known_and_unknown_args'",
            id="enabler-initial-conftests",
        ),
        pytest.param(
            "pytest-option-probe",
            "1.0",
            "from argparse import Namespace\n\n"
            "def pytest_addoption(parser):\n"
            "    parser.parse_known_and_unknown_args([], Namespace())\n",
            "pytest_addoption",
            "AttributeError: 'Parser' object has no attribute 'parse_known_and_unknown_args'",
            id="historic-option-hook",
        ),
        pytest.param(
            "pytest-wrapper-probe",
            "1.0",
            "import pytest\n\n"
            "@pytest.hookimpl(wrapper=True)\n"
            "def pytest_load_initial_conftests(early_config, parser, args):\n"
            "    result = yield\n"
            "    parser.parse_known_and_unknown_args(args, early_config.known_args_namespace)\n"
            "    return result\n",
            "pytest_load_initial_conftests",
            "AttributeError: 'Parser' object has no attribute 'parse_known_and_unknown_args'",
            id="startup-wrapper-after-yield",
        ),
        pytest.param(
            "pytest-command-probe",
            "1.0",
            "def pytest_cmdline_main(config):\n    config.pluginmanager.consider_env()\n",
            "pytest_cmdline_main",
            "AttributeError: 'PluginManager' object has no attribute 'consider_env'",
            id="command-hook",
        ),
        pytest.param(
            "pytest-registration-observer",
            "1.0",
            "def pytest_plugin_registered(plugin_name, manager):\n"
            "    if plugin_name == 'registration-subject':\n"
            "        manager.consider_env()\n",
            "pytest_plugin_registered",
            "AttributeError: 'PluginManager' object has no attribute 'consider_env'",
            id="observer-of-another-distribution",
        ),
        pytest.param(
            "pytest-unsupported-probe",
            "1.0",
            "def pytest_cmdline_parse(pluginmanager, args):\n    pass\n",
            "registration",
            "independent pytest does not support hook 'pytest_cmdline_parse'",
            id="unsupported-hook-registration",
        ),
    ],
)
def test_pytest_entry_point_startup_failure_and_coverage_recovery(
    tmp_path: Path, distribution: str, version: str, source: str, operation: str, cause: str
) -> None:
    project = VfyProject(tmp_path)
    name, module = _install_entry_point(project, distribution, version, source)
    project.write(".coveragerc", "[run]\nsource = measured\n")
    project.write("measured.py", "value = 5\n")
    project.write(
        "test_activation.py",
        "from pathlib import Path\n\n"
        "from measured import value\n\n"
        "def test_plugin_is_active(pytestconfig):\n"
        f"    assert pytestconfig.pluginmanager.has_plugin({name!r})\n"
        "    assert value == 5\n"
        "    Path('executed').touch()\n",
    )
    disabled = [
        argument
        for plugin in sorted(
            {point.name for point in entry_points(group="pytest11")} - {name, "pytest_cov"}
        )
        for argument in ("-p", f"no:{plugin}")
    ]
    explicit: tuple[str, ...] = ()
    if operation == "pytest_plugin_registered":
        subject, _ = _install_entry_point(project, "pytest-registration-subject", "1.0", "")
        explicit = ("-p", name, "-p", subject)
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}
    arguments = ("test_activation.py", *disabled, *explicit)
    identity = f"{distribution} {version} (pytest11 {name} = {module}) during {operation}"
    recovery = "pytest -p no:pytest_cov -p vfy.pytest_plugin --cov"

    native = project.run_engine("native", *arguments, env=environment)
    native.expect(4 if operation == "registration" else 1)
    if native.returncode == 1:
        assert "Traceback (most recent call last)" in native.stderr
    assert cause in native.stderr
    assert identity in native.stderr
    assert "pytest-registration-subject 1.0" not in native.stderr
    assert recovery in native.stderr
    assert "Test result evidence could not be recorded" not in native.stderr
    assert not (tmp_path / "executed").exists()

    diagnostics = project.run_vfy("diagnostics", "--collect", "--", *arguments, env=environment)
    diagnostics.expect(0)
    assert "Collection: failed" in diagnostics.stdout
    assert identity in diagnostics.stdout
    assert cause in diagnostics.stdout
    assert recovery in diagnostics.stdout

    structured = project.run_vfy(
        "diagnostics", "--collect", "--json", "--", *arguments, env=environment
    )
    structured.expect(0)
    readiness = json.loads(structured.stdout)["test_readiness"]
    assert readiness["status"] == "blocked"
    collection = readiness["collection"]
    assert collection["returncode"] == native.returncode
    assert identity in collection["diagnostic"]
    assert cause in collection["diagnostic"]
    assert recovery in collection["diagnostic"]

    stock = project.run_python(
        "-m",
        "pytest",
        "-p",
        "no:pytest_cov",
        "-p",
        "vfy.pytest_plugin",
        *arguments,
        "--cov",
        env=environment,
    )
    stock.expect(0)
    assert (tmp_path / "executed").is_file()
    assert project.lines("measured.py") == [1]
