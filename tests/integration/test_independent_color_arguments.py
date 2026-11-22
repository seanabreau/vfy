"""Public vfy color aliases reach the actual pytest Config and terminal writer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_PLUGIN = """import json
from pathlib import Path

def pytest_sessionfinish(session):
    terminal=session.config.pluginmanager.getplugin('terminalreporter')
    Path('color.json').write_text(json.dumps({
        'option':session.config.option.color,
        'getoption':session.config.getoption('color'),
        'markup':terminal._tw.hasmarkup,
        'text':terminal._tw.markup('actual text',green=True),
    }))
"""

_ENVIRONMENT: dict[str, str | None] = {
    "PYTEST_ADDOPTS": None,
    "PY_COLORS": None,
    "NO_COLOR": None,
    "FORCE_COLOR": None,
    "TERM": None,
}


def _run(
    project: VfyProject,
    route: str,
    before: list[str],
    after: list[str],
    expected: str,
    *,
    ini: str = "",
    environment: dict[str, str | None] | None = None,
) -> None:
    project.write("pytest.ini", "[pytest]\n" + ini)
    project.write("conftest.py", _PLUGIN)
    project.write("test_color.py", "def test_color():pass\n")
    env = {**_ENVIRONMENT, **(environment or {})}
    stock_color = [f"--color={expected}"] if before or after else []
    result = (
        project.run_python("-B", "-m", "pytest", "-q", "-s", *stock_color, env=env)
        if route == "stock"
        else project.run_vfy(
            *before, "test", "--engine=native", "--no-cov", "-q", "-s", *after, env=env
        )
    )
    project.write(f"{route}.stdout", result.stdout)
    project.write(f"{route}.stderr", result.stderr)
    project.write(f"{route}.status", str(result.returncode))
    result.expect(0)
    observed = json.loads((project.root / "color.json").read_text())
    markup = expected == "yes"
    assert observed == {
        "option": expected,
        "getoption": expected,
        "markup": markup,
        "text": "\x1b[32mactual text\x1b[0m" if markup else "actual text",
    }


@requires_native_pytest
@pytest.mark.parametrize("route", ["stock", "independent"])
@pytest.mark.parametrize("position", ["global", "local", "target"])
@pytest.mark.parametrize(
    ("choice", "expected"), [("auto", "auto"), ("always", "yes"), ("never", "no")]
)
def test_public_color_aliases_reach_canonical_config_and_writer(
    tmp_path: Path, route: str, position: str, choice: str, expected: str
) -> None:
    option = f"--color={choice}"
    before = [option] if position == "global" else []
    after = ["test_color.py", option] if position == "target" else [option]
    if position == "global":
        after = []
    _run(VfyProject(tmp_path), route, before, after, expected)


@requires_native_pytest
@pytest.mark.parametrize("route", ["stock", "independent"])
@pytest.mark.parametrize(
    ("before", "after", "expected", "environment"),
    [
        (["--color"], [], "yes", {"NO_COLOR": "1"}),
        ([], ["--color"], "yes", {"NO_COLOR": "1"}),
        (["--no-color"], [], "no", {"FORCE_COLOR": "1"}),
        ([], ["--no-color"], "no", {"FORCE_COLOR": "1"}),
        ([], ["--color=yes"], "yes", {"NO_COLOR": "1"}),
        ([], ["--color=no"], "no", {"FORCE_COLOR": "1"}),
        (["--color=always"], ["--color=no"], "no", {}),
        (["--color=never"], ["--color=yes"], "yes", {}),
    ],
)
def test_explicit_color_flags_override_environment_and_earlier_choices(
    tmp_path: Path,
    route: str,
    before: list[str],
    after: list[str],
    expected: str,
    environment: dict[str, str | None],
) -> None:
    _run(VfyProject(tmp_path), route, before, after, expected, environment=environment)


@requires_native_pytest
@pytest.mark.parametrize("route", ["stock", "independent"])
@pytest.mark.parametrize(
    ("ini", "environment", "expected"),
    [
        ("addopts = --color=yes\n", {}, "yes"),
        ("addopts = --color=no\n", {}, "no"),
        ("", {"PYTEST_ADDOPTS": "--color=yes"}, "yes"),
        ("", {"PYTEST_ADDOPTS": "--color=no"}, "no"),
    ],
)
def test_absent_vfy_color_preserves_upstream_addopts(
    tmp_path: Path, route: str, ini: str, environment: dict[str, str | None], expected: str
) -> None:
    _run(VfyProject(tmp_path), route, [], [], expected, ini=ini, environment=environment)


@requires_native_pytest
@pytest.mark.parametrize("engine", ["pytest-native", "pytest", "native"])
@pytest.mark.parametrize("choice", ["yes", "no", "auto"])
def test_color_forwarding_reaches_every_test_engine(
    tmp_path: Path, engine: str, choice: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", _PLUGIN)
    project.write("test_color.py", "def test_color():pass\n")

    result = project.run_vfy(
        "test", f"--engine={engine}", "--no-cov", "-q", "-s", f"--color={choice}", env=_ENVIRONMENT
    )

    result.expect(0)
    assert json.loads((tmp_path / "color.json").read_text())["option"] == choice


@pytest.mark.parametrize("arguments", [["--color=invalid", "test"], ["test", "--color=invalid"]])
def test_vfy_color_rejects_invalid_choices(tmp_path: Path, arguments: list[str]) -> None:
    result = VfyProject(tmp_path).run_vfy(*arguments)

    assert result.returncode == 2
    assert "invalid choice" in result.stderr


@requires_native_pytest
@pytest.mark.parametrize("route", ["stock", "independent"])
@pytest.mark.parametrize("arguments", [["--color"], ["--color=invalid"], ["--color=never"]])
def test_direct_color_usage_errors_precede_collection(
    tmp_path: Path, route: str, arguments: list[str]
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_color.py",
        "from pathlib import Path\nPath('imported').touch()\ndef test_value():pass\n",
    )
    result = project.run_python(
        "-B",
        "-m",
        "pytest" if route == "stock" else "vfy.independent.entry",
        *arguments,
        env=_ENVIRONMENT,
    )
    project.write(f"{route}.stdout", result.stdout)
    project.write(f"{route}.stderr", result.stderr)
    project.write(f"{route}.status", str(result.returncode))
    result.expect(4)
    assert not (project.root / "imported").exists()
    assert (
        "expected one argument" if arguments == ["--color"] else "invalid choice"
    ) in result.stderr


@requires_native_pytest
@pytest.mark.parametrize("route", ["stock", "independent"])
@pytest.mark.parametrize("explicit_color", [False, True])
def test_blocked_terminal_provider_does_not_declare_color(
    tmp_path: Path, route: str, explicit_color: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_color.py", "def test_color():pass\n")
    project.write(
        "conftest.py",
        "import json\nfrom pathlib import Path\n"
        "def pytest_sessionfinish(session):\n"
        "    Path('blocked.json').write_text(json.dumps({\n"
        "        'terminal':session.config.pluginmanager.getplugin('terminal'),\n"
        "        'reporter':session.config.pluginmanager.getplugin('terminalreporter'),\n"
        "        'has_color':hasattr(session.config.option,'color')}))\n",
    )
    color = ["--color=no"] if explicit_color else []
    result = (
        project.run_python("-B", "-m", "pytest", "-p", "no:terminal", *color, env=_ENVIRONMENT)
        if route == "stock"
        else project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            "-p",
            "no:terminal",
            *color,
            env=_ENVIRONMENT,
        )
    )
    project.write(f"{route}.stdout", result.stdout)
    project.write(f"{route}.stderr", result.stderr)
    project.write(f"{route}.status", str(result.returncode))
    assert result.returncode == (4 if explicit_color else 0), result.stdout + result.stderr
    if explicit_color:
        assert "unrecognized arguments: --color=no" in result.stderr
        assert not (project.root / "blocked.json").exists()
    else:
        assert json.loads((project.root / "blocked.json").read_text()) == {
            "terminal": None,
            "reporter": None,
            "has_color": False,
        }
