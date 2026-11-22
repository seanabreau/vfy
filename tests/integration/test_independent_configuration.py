"""Independent configuration values match the pinned pytest getini contract."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PROBE = """\
import json
from pathlib import Path


def pytest_addoption(parser):
    for declaration in json.loads(Path("declarations.json").read_text()):
        parser.addini(**declaration)


def pytest_configure(config):
    results = []
    for name, default in json.loads(Path("queries.json").read_text()):
        try:
            value = config.getini(name)
            results.append(["returned", repr(value), type(value).__name__])
        except (ValueError, TypeError) as error:
            results.append([type(error).__name__, str(error)])
    Path("observed.json").write_text(json.dumps(results))
"""


def _observe(
    tmp_path: Path,
    files: dict[str, str],
    declarations: list[dict[str, object]],
    queries: list[str],
    arguments: tuple[str, ...] = (),
) -> list[list[list[str]]]:
    observed: list[list[list[str]]] = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        for name, contents in files.items():
            project.write(name, contents)
        project.write("conftest.py", _PROBE)
        project.write("declarations.json", json.dumps(declarations))
        project.write("queries.json", json.dumps([[name, None] for name in queries]))
        project.write("test_body.py", "def test_body():\n    pass\n")
        result = project.run_engine(engine, "-q", *arguments, env={"PYTEST_ADDOPTS": None})
        result.expect(0)
        text = (project.root / "observed.json").read_text().replace(str(project.root), "<root>")
        observed.append(json.loads(text))
    return observed


@pytest.mark.parametrize(
    ("filename", "contents", "kind"),
    [
        ("pytest.ini", "[pytest]\nsetting = one 'two words'\n", "args"),
        ("pytest.ini", '[pytest]\nsetting = "a\\\n', "args"),
        ("pytest.ini", "[pytest]\nsetting = 'a\\\n", "args"),
        ("pytest.ini", "[pytest]\nsetting =\n    first\n    second\n", "linelist"),
        ("pytest.ini", "[pytest]\nsetting = YES\n", "bool"),
        ("pytest.ini", "[pytest]\nsetting = invalid\n", "bool"),
        ("pytest.ini", "[pytest]\nsetting = 1_000\n", "int"),
        ("pytest.ini", "[pytest]\nsetting = 1.25e2\n", "float"),
        ("pytest.ini", "[pytest]\nsetting = one 'two words'\n", "paths"),
        ("pyproject.toml", "[tool.pytest.ini_options]\nsetting = true\n", "bool"),
        ("pyproject.toml", "[tool.pytest.ini_options]\nsetting = ['first', 'second']\n", "args"),
        ("pyproject.toml", "[tool.pytest.ini_options]\nsetting = [1, 2]\n", "int"),
        ("pytest.toml", "[pytest]\nsetting = true\n", "bool"),
        ("pytest.toml", "[pytest]\nsetting = 3\n", "float"),
        ("pytest.toml", "[pytest]\nsetting = ['first', 'second']\n", "paths"),
        ("pytest.toml", "[pytest]\nsetting = 'bad'\n", "args"),
        ("pytest.toml", "[pytest]\nsetting = ['first', 2]\n", "args"),
        ("pytest.toml", "[pytest]\nsetting = true\n", "int"),
    ],
)
def test_independent_declared_ini_types_match_pytest(
    tmp_path: Path, filename: str, contents: str, kind: str
) -> None:
    stock, independent = _observe(
        tmp_path,
        {filename: contents},
        [{"name": "setting", "help": "differential", "type": kind}],
        ["setting"],
    )
    assert independent == stock


def test_independent_overrides_and_mutable_defaults_match_pytest(tmp_path: Path) -> None:
    stock, independent = _observe(
        tmp_path,
        {"pytest.toml": "[pytest]\nsetting = 7\n"},
        [
            {"name": "setting", "help": "differential", "type": "int"},
            {"name": "absent", "help": "default", "type": "linelist", "default": ["x"]},
        ],
        ["setting", "absent"],
        ("-o", "setting=9"),
    )
    assert independent == stock
    assert stock[0] == ["returned", "9", "int"]


@pytest.mark.parametrize(
    ("settings", "arguments"),
    [
        ("setting = canonical\nlegacy = alias\n", []),
        ("setting = canonical\nlegacy = alias\n", ["-o", "legacy=override"]),
        ("setting = canonical\nlegacy = alias\n", ["-o", "setting=override"]),
        ("legacy = alias\n", []),
    ],
)
def test_independent_ini_alias_precedence_matches_pytest(
    tmp_path: Path, settings: str, arguments: list[str]
) -> None:
    stock, independent = _observe(
        tmp_path,
        {"pytest.ini": f"[pytest]\n{settings}"},
        [{"name": "setting", "help": "alias differential", "aliases": ["legacy"]}],
        ["setting"],
        tuple(arguments),
    )
    assert independent == stock


@pytest.mark.parametrize(
    ("filename", "contents", "overrides", "expected"),
    [
        (
            "pytest.ini",
            "[pytest]\nz_unknown = file\na_unknown = file\nlegacy_setting = alias\n",
            ["-o", "cli_unknown=value", "-o", "z_unknown=override"],
            ["a_unknown", "cli_unknown", "z_unknown"],
        ),
        (
            "pytest.toml",
            "[pytest]\nz_unknown = 'file'\na_unknown = 'file'\nlegacy_setting = 'alias'\n",
            ["-o", "cli_unknown=value", "-o", "z_unknown=override"],
            ["a_unknown", "cli_unknown", "z_unknown"],
        ),
        (
            "pytest.ini",
            "[pytest]\naddopts = -o addopts_unknown=value\nlegacy_setting = alias\n",
            [],
            ["addopts_unknown"],
        ),
    ],
)
def test_independent_unknown_ini_names_match_pytest_after_registration(
    tmp_path: Path, filename: str, contents: str, overrides: list[str], expected: list[str]
) -> None:
    names = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(filename, contents)
        project.write(
            "conftest.py",
            "def pytest_addoption(parser):\n"
            "    parser.addini('registered', 'unknown setting differential',"
            " aliases=['legacy_setting'])\n",
        )
        project.write("test_body.py", "def test_body():\n    pass\n")
        result = project.run_engine(engine, "-q", "-c", filename, *overrides)
        result.expect(0)
        names.append(sorted(re.findall(r"Unknown config option: (\w+)", result.stdout)))
    assert names[0] == expected
    assert names[1] == expected
