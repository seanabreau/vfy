"""Plugin exclusions occur at the real CLI and configured-argument phases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict, cast

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


class Observation(TypedDict):
    terminal: bool
    reporter: bool
    color: bool
    blocked: list[str]
    cache: bool
    history: list[str]
    namespace: list[str]


_ENVIRONMENT: dict[str, str | None] = {
    "PYTEST_ADDOPTS": None,
    "PYTEST_PLUGINS": None,
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}
_OBSERVER = """import json,os
from pathlib import Path
seen=[]
def pytest_plugin_registered(plugin_name):
    seen.append(plugin_name)
def pytest_sessionfinish(session):
    manager=session.config.pluginmanager
    names=('terminal','pytest_terminal','_pytest.terminal','runner','pytest_runner',
           'cacheprovider','pytest_cacheprovider','stepwise','pytest_stepwise')
    Path(f'observed-{os.getpid()}.json').write_text(json.dumps({
        'terminal':manager.hasplugin('terminal'),
        'reporter':manager.hasplugin('terminalreporter'),
        'color':hasattr(session.config.option,'color'),
        'blocked':[name for name in names if manager.is_blocked(name)],
        'cache':hasattr(session.config,'cache'),
        'history':seen,
        'namespace':session.config.option.plugins,
    }))
"""
_CASE = """def test_case(tmp_path, monkeypatch, doctest_namespace, record_property):
    assert tmp_path.is_dir()
    monkeypatch.setenv('REAL_FIXTURE', 'active')
    assert isinstance(doctest_namespace,dict)
    record_property('actual', 'value')
"""


def _run(
    project: VfyProject,
    engine: str,
    arguments: list[str],
    *,
    environment: str = "",
    ini: str = "",
    body: str = _CASE,
) -> tuple[int, str, list[Observation]]:
    project.write("pytest.ini", "[pytest]\naddopts = " + ini + "\n")
    project.write("conftest.py", _OBSERVER)
    project.write("test_case.py", body)
    env = {**_ENVIRONMENT, "PYTEST_ADDOPTS": environment}
    if engine == "stock":
        result = project.run_python("-B", "-m", "pytest", *arguments, env=env)
    else:
        workers = ["--workers=2"] if engine == "independent-workers" else []
        result = project.run_vfy(
            "test", "--engine=native", "--no-cov", *workers, *arguments, env=env
        )
    project.write("stdout", result.stdout)
    project.write("stderr", result.stderr)
    project.write("status", str(result.returncode))
    observed = [
        cast("Observation", json.loads(path.read_text()))
        for path in sorted(project.root.glob("observed-*.json"))
    ]
    return result.returncode, result.stdout + result.stderr, observed


def _exclusions(observation: Observation) -> list[str]:
    # vfy adds its own report plug-ins to the same namespace.
    return [name for name in observation["namespace"] if name.startswith("no:")]


_SCENARIOS = [
    pytest.param(
        ["-p", "no:terminal"], "", "", False, False, ["terminal", "pytest_terminal"], id="cli"
    ),
    pytest.param(
        ["-pno:terminal"], "", "", False, False, ["terminal", "pytest_terminal"], id="compact"
    ),
    pytest.param(
        ["-p", " no:terminal "],
        "",
        "",
        False,
        False,
        ["terminal", "pytest_terminal"],
        id="operand-whitespace",
    ),
    pytest.param(
        [], "-p no:terminal", "", False, True, ["terminal", "pytest_terminal"], id="environment"
    ),
    pytest.param([], "", "-p no:terminal", False, True, ["terminal", "pytest_terminal"], id="ini"),
    pytest.param(
        ["-spno:terminal"], "", "", True, True, [], id="argparse-group-ignored-for-activation"
    ),
    pytest.param(
        ["-p", "no:pytest_terminal"],
        "",
        "",
        True,
        True,
        ["pytest_terminal"],
        id="pytest-prefix-is-distinct",
    ),
    pytest.param(
        ["-p", "no:_pytest.terminal"],
        "",
        "",
        True,
        True,
        ["_pytest.terminal"],
        id="module-name-is-distinct",
    ),
    pytest.param(["-p", "no:terminal", "-p", "terminal"], "", "", True, True, [], id="reenable"),
    pytest.param(
        ["-p", "terminal", "-p", "no:terminal"],
        "",
        "",
        False,
        True,
        ["terminal", "pytest_terminal"],
        id="positive-before-exclusion",
    ),
]


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize(
    ("arguments", "environment", "ini", "terminal", "color", "blocked"), _SCENARIOS
)
def test_cli_preparse_preserves_later_exclusion_and_real_provider_declarations(
    tmp_path: Path,
    engine: str,
    arguments: list[str],
    environment: str,
    ini: str,
    terminal: bool,
    color: bool,
    blocked: list[str],
) -> None:
    status, output, observed = _run(
        VfyProject(tmp_path), engine, arguments, environment=environment, ini=ini
    )
    assert status == 0, output
    assert len(observed) == 1
    assert {
        name: observed[0][name] for name in ("terminal", "reporter", "color", "blocked", "cache")
    } == {
        "terminal": terminal,
        "reporter": terminal,
        "color": color,
        "blocked": blocked,
        "cache": True,
    }
    if arguments == ["-spno:terminal"]:
        assert _exclusions(observed[0]) == ["no:terminal"]


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("phase", ["cli", "environment", "ini"])
@pytest.mark.parametrize(
    "provider", ["capture", "cacheprovider", "monkeypatch", "doctest", "junitxml", "subtests"]
)
def test_exclusion_history_and_unrelated_fixture_availability(
    tmp_path: Path, engine: str, phase: str, provider: str
) -> None:
    option = f"-p no:{provider}"
    body = "def test_case(tmp_path):\n    assert tmp_path.is_dir()\n"
    status, output, observed = _run(
        VfyProject(tmp_path),
        engine,
        ["-p", f"no:{provider}"] if phase == "cli" else [],
        environment=option if phase == "environment" else "",
        ini=option if phase == "ini" else "",
        body=body,
    )
    assert status == 0, output
    assert len(observed) == 1
    assert (provider in observed[0]["history"]) is (phase != "cli")
    assert observed[0]["color"] is True
    if provider == "cacheprovider":
        assert observed[0]["cache"] is False
        assert observed[0]["blocked"] == [
            "cacheprovider",
            "pytest_cacheprovider",
            "stepwise",
            "pytest_stepwise",
        ]


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("phase", ["cli", "environment", "ini"])
@pytest.mark.parametrize("provider", ["mark", "main", "runner", "fixtures", "helpconfig"])
def test_essential_plugin_exclusions_are_usage_errors_before_test_import(
    tmp_path: Path, engine: str, phase: str, provider: str
) -> None:
    option = f"-p no:{provider}"
    status, output, observed = _run(
        VfyProject(tmp_path),
        engine,
        ["-p", f"no:{provider}"] if phase == "cli" else [],
        environment=option if phase == "environment" else "",
        ini=option if phase == "ini" else "",
        body="from pathlib import Path\nPath('imported').touch()\ndef test_case():pass\n",
    )
    assert status == 4, output
    assert f"plugin {provider} cannot be disabled" in output
    assert observed == []
    assert not (tmp_path / "imported").exists()


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("provider", ["terminal", "runner"])
def test_argument_file_plugin_operands_do_not_activate_plugins(
    tmp_path: Path, engine: str, provider: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("options.txt", f"-p\nno:{provider}\n")
    status, output, observed = _run(project, engine, ["@options.txt"])
    assert status == 0, output
    assert len(observed) == 1
    assert observed[0]["terminal"] is True
    assert observed[0]["color"] is True
    assert observed[0]["blocked"] == []
    assert _exclusions(observed[0]) == [f"no:{provider}"]


@pytest.mark.parametrize(
    "engine", ["stock", pytest.param("independent-workers", marks=requires_native_pytest)]
)
@pytest.mark.parametrize("phase", ["cli", "environment", "argument-file"])
def test_worker_bootstrap_preserves_original_plugin_token_provenance(
    tmp_path: Path, engine: str, phase: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_second.py", "def test_second(tmp_path):assert tmp_path.is_dir()\n")
    arguments = ["-pno:terminal"] if phase == "cli" else []
    if phase == "argument-file":
        project.write("options.txt", "-p\nno:terminal\n")
        arguments = ["@options.txt"]
    status, output, observed = _run(
        project,
        engine,
        arguments,
        environment="-p no:terminal" if phase == "environment" else "",
        body="def test_first(tmp_path):assert tmp_path.is_dir()\n",
    )
    assert status == 0, output
    expected = {
        "terminal": phase == "argument-file",
        "color": phase != "cli",
        "blocked": [] if phase == "argument-file" else ["terminal", "pytest_terminal"],
    }
    # The controller and each worker evaluate the original plug-in tokens.
    assert len(observed) == (3 if engine == "independent-workers" else 1)
    for child in observed:
        assert {
            "terminal": child["terminal"],
            "color": child["color"],
            "blocked": child["blocked"],
        } == expected
