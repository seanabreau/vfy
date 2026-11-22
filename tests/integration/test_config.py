from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any

import coverage
import pytest

import vfy
from tests.support import VfyProject
from vfy.errors import ConfigError


def _options(tool: ModuleType, config: Path, names: list[str]) -> dict[str, object]:
    """Read resolved options from the coverage or vfy module's `Coverage` class."""

    collector = tool.Coverage(config_file=str(config))
    return {name: collector.get_option(name) for name in names}


def _shown(root: Path, env: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Return the JSON that ``vfy config show`` prints for the project in ``root``."""

    result = VfyProject(root).run_vfy("config", "show", env=env)
    result.expect(0)
    return json.loads(result.stdout)


def _same_as_coverage_py(config: Path, names: list[str]) -> dict[str, object]:
    actual = _options(vfy, config, names)
    assert actual == _options(coverage, config, names)
    return actual


def test_ini_resolution_matches_coverage_py_for_defaults_environment_and_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPORT_ROOT", "build/coverage")
    config = tmp_path / "setup.cfg"
    config.write_text(
        """
[DEFAULT]
source =
    package
    helpers

[coverage:run]
data_file = ${REPORT_ROOT}/coverage$$data
""".strip(),
        encoding="utf-8",
    )

    reference = coverage.Coverage(config_file=str(config)).config
    run = _shown(tmp_path)["settings"]["run"]

    assert run == {"data_file": reference.data_file, "source": reference.source}
    assert run["data_file"] == "build/coverage/coverage$data"


def test_project_toml_resolution_preserves_typed_values(tmp_path: Path) -> None:
    config = tmp_path / "pyproject.toml"
    config.write_text(
        """
[tool.coverage.run]
branch = true
relative_files = true
source = ["package", "helpers"]
source_pkgs = ["package", "helpers"]

[tool.coverage.report]
precision = 2
fail_under = 99.5

[tool.coverage.html]
show_contexts = true
""".strip(),
        encoding="utf-8",
    )

    reference = coverage.Coverage(config_file=str(config)).config
    settings = _shown(tmp_path)["settings"]

    assert settings == {
        "html": {"show_contexts": True},
        "report": {"fail_under": 99.5, "precision": 2},
        "run": {
            "branch": True,
            "relative_files": True,
            "source": ["package", "helpers"],
            "source_pkgs": ["package", "helpers"],
        },
    }
    assert settings["run"]["branch"] == reference.branch
    assert settings["run"]["source"] == reference.source
    assert settings["run"]["source_pkgs"] == reference.source_pkgs
    assert settings["report"]["precision"] == reference.precision
    assert settings["report"]["fail_under"] == reference.fail_under


def test_core_and_warning_settings_resolve_like_coverage_py(tmp_path: Path) -> None:
    config = tmp_path / "pyproject.toml"
    config.write_text(
        """
[tool.coverage.run]
core = "sysmon"
disable_warnings = ["no-data-collected", "module-not-imported"]
""".strip(),
        encoding="utf-8",
    )

    actual = _same_as_coverage_py(config, ["run:core", "run:disable_warnings"])

    assert actual["run:core"] == "sysmon"


def test_explicit_custom_toml_uses_coverage_tool_settings(tmp_path: Path) -> None:
    config = tmp_path / "coverage.toml"
    config.write_text(
        '[tool.coverage]\nrun.patch = ["subprocess"]\nrun.parallel = true\n'
        'run.source = ["app"]\nreport.fail_under = 100\n',
        encoding="utf-8",
    )
    _same_as_coverage_py(config, ["run:patch", "run:parallel", "run:source", "report:fail_under"])


_TOML_OPTIONS = [
    "run:branch",
    "run:source",
    "run:parallel",
    "report:fail_under",
    "report:precision",
]


@pytest.mark.parametrize(
    ("name", "contents"),
    [
        ("coverage.toml", '[run]\nbranch = true\nsource = ["app"]\n[report]\nfail_under = 90\n'),
        (
            "coverage.toml",
            '[tool.coverage.run]\nbranch = true\n[run]\nparallel = true\nsource = ["bare"]\n'
            "[report]\nprecision = 2\n",
        ),
        ("coverage.toml", "[coverage.run]\nbranch = true\n[tool.coverage.report]\nprecision = 1\n"),
        ("pyproject.toml", "[run]\nbranch = true\n[tool.coverage.report]\nprecision = 1\n"),
    ],
)
def test_explicit_toml_bare_sections_match_coverage_py(
    tmp_path: Path, name: str, contents: str
) -> None:
    config = tmp_path / name
    config.write_text(contents, encoding="utf-8")
    _same_as_coverage_py(config, _TOML_OPTIONS)


@pytest.mark.parametrize(
    ("name", "contents", "bare_is_read"),
    [
        (".coveragerc.toml", "[run]\nbranch = true\n[tool.coverage.report]\nprecision = 1\n", True),
        ("pyproject.toml", "[run]\nbranch = true\n[tool.coverage.report]\nprecision = 1\n", False),
    ],
)
def test_discovered_toml_bare_sections_match_coverage_py(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    contents: str,
    bare_is_read: bool,
) -> None:
    (tmp_path / name).write_text(contents, encoding="utf-8")
    monkeypatch.delenv("COVERAGE_RCFILE", raising=False)
    monkeypatch.chdir(tmp_path)

    actual = {
        tool.__name__: {n: tool.Coverage(config_file=True).get_option(n) for n in _TOML_OPTIONS}
        for tool in (coverage, vfy)
    }

    assert actual["vfy"] == actual["coverage"]
    assert actual["vfy"]["run:branch"] is bare_is_read
    assert actual["vfy"]["report:precision"] == 1


def test_configured_source_pkgs_collects_the_same_package_as_coverage_py(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    source = project.write(
        "sample_pkg/__init__.py",
        "def answer(value):\n    if value:\n        return 42\n    return 0\n",
    )
    project.write(
        "pyproject.toml",
        '[tool.coverage.run]\nsource_pkgs = ["sample_pkg"]\nbranch = true\n',
    )
    project.write(
        "collect.py",
        "import importlib\n"
        "import sys\n"
        "from sample_pkg import answer\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "coverage = Coverage(data_file=sys.argv[2], config_file='pyproject.toml')\n"
        "coverage.start()\n"
        "try:\n"
        "    assert answer(True) == 42\n"
        "finally:\n"
        "    coverage.stop()\n"
        "coverage.save()\n",
    )

    for tool, data_file in (("coverage", ".coverage-reference"), ("vfy", ".coverage-vfy")):
        completed = project.run_python("collect.py", tool, data_file)
        completed.expect(0)

    expected = project.data(".coverage-reference")
    actual = project.data(".coverage-vfy")
    filename = str(source.resolve())
    assert actual.measured_files() == expected.measured_files() == {filename}
    assert actual.lines(filename) == expected.lines(filename)
    assert set(actual.arcs(filename) or ()) == set(expected.arcs(filename) or ())


def test_path_alias_groups_match_coverage_py(tmp_path: Path) -> None:
    config = tmp_path / "pyproject.toml"
    config.write_text(
        """
[tool.coverage.paths]
generated = ["generated", "/build/generated"]
source = ["src", "/workers/*/checkout/src"]
""".strip(),
        encoding="utf-8",
    )

    reference = coverage.Coverage(config_file=str(config)).config
    paths = _shown(tmp_path)["settings"]["paths"]

    assert paths == reference.paths
    assert list(paths) == list(reference.paths) == ["generated", "source"]


def test_plugin_sections_follow_coverage_py_names_and_preserve_values(tmp_path: Path) -> None:
    ini = tmp_path / ".coveragerc"
    ini.write_text(
        "[run]\nplugins = sample_plugin\n[coverage:sample_plugin]\nanswer = 42\n",
        encoding="utf-8",
    )
    assert _shown(tmp_path)["settings"] == {
        "run": {"plugins": ["sample_plugin"]},
        "plugin:sample_plugin": {"answer": "42"},
    }

    ini.unlink()
    toml = tmp_path / "pyproject.toml"
    toml.write_text(
        "[tool.coverage.run]\nplugins = ['sample_plugin']\n"
        "[tool.coverage.sample_plugin]\nanswer = 42\n",
        encoding="utf-8",
    )
    assert _shown(tmp_path)["settings"] == {
        "run": {"plugins": ["sample_plugin"]},
        "plugin:sample_plugin": {"answer": 42},
    }
    assert coverage.Coverage(config_file=str(toml)).get_option("sample_plugin:answer") == 42


def test_report_additive_patterns_sort_and_format_match_coverage_py(tmp_path: Path) -> None:
    config = tmp_path / ".coveragerc"
    config.write_text(
        "[report]\n"
        "exclude_also =\n    raise NotImplementedError\n"
        "partial_also =\n    if debug:\n"
        "sort = miss\n"
        "format = markdown\n",
        encoding="utf-8",
    )

    reference = coverage.Coverage(config_file=str(config)).config

    assert _shown(tmp_path)["settings"]["report"] == {
        "exclude_also": reference.exclude_also,
        "format": reference.format,
        "partial_also": reference.partial_also,
        "sort": reference.sort,
    }


def test_discovery_matches_coverage_py_working_directory_and_rcfile_rules(tmp_path: Path) -> None:
    project = tmp_path / "project"
    nested = project / "package" / "tests"
    nested.mkdir(parents=True)
    config = project / ".coveragerc"
    config.write_text("[run]\nbranch = true\n", encoding="utf-8")

    unconfigured = _shown(nested, env={"COVERAGE_RCFILE": None})
    assert unconfigured["settings"] == {}
    assert unconfigured["source"] is None
    assert _shown(project, env={"COVERAGE_RCFILE": None})["source"] == str(config)

    override = tmp_path / "override.ini"
    override.write_text("[run]\nparallel = true\n", encoding="utf-8")
    overridden = _shown(nested, env={"COVERAGE_RCFILE": str(override)})
    assert overridden["source"] == str(override)
    assert overridden["settings"]["run"]["parallel"] is True


def test_discovery_ignores_unrelated_tox_sections_and_reaches_pyproject(tmp_path: Path) -> None:
    (tmp_path / "tox.ini").write_text(
        "[tox]\nenvlist = py312\n\n[testenv]\ncommands = pytest\n",
        encoding="utf-8",
    )
    config = tmp_path / "pyproject.toml"
    config.write_text("[tool.coverage.run]\nbranch = true\n", encoding="utf-8")

    shown = _shown(tmp_path, env={"COVERAGE_RCFILE": None})
    assert shown["source"] == str(config)
    assert shown["settings"]["run"]["branch"] is True


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("[run]\nbranch = perhaps\n", "invalid value"),
        ("[report]\nprecision = 1.5\n", "invalid value"),
        ("[run]\ndata_file = ${VFY_MISSING?}\n", "environment variable"),
    ],
)
def test_rust_configuration_rejects_invalid_values(
    tmp_path: Path, contents: str, message: str
) -> None:
    config = tmp_path / ".coveragerc"
    config.write_text(contents, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        vfy.Coverage(config_file=str(config))


def test_project_toml_configuration_rejects_wrong_types(tmp_path: Path) -> None:
    config = tmp_path / "pyproject.toml"
    config.write_text("[tool.coverage.run]\nsource = 42\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid value"):
        vfy.Coverage(config_file=str(config))


def test_vfy_settings_expand_environment(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.vfy.test]\nselection = "safe"\nhistory = "${VFY_TEST_HISTORY}"\n',
        encoding="utf-8",
    )
    shown = _shown(tmp_path, env={"VFY_TEST_HISTORY": "cache/history.json"})
    assert shown["settings"]["vfy"] == {
        "test": {"selection": "safe", "history": "cache/history.json"}
    }


@pytest.mark.parametrize(
    ("filename", "contents", "message"),
    [
        ("pyproject.toml", "[tool.coverage.run\n", "failed to read TOML"),
        (".coveragerc", "[run]\nbranch = true\nbranch = false\n", "duplicate option"),
        (
            "pyproject.toml",
            "[tool.vfy.unknown]\nvalue = 1\n",
            "unsupported configuration section",
        ),
        ("pyproject.toml", "[tool.vfy.test]\nunknown = 1\n", "unsupported option"),
        ("pyproject.toml", "[tool.vfy.test]\nselection = 'unsafe'\n", "invalid value"),
        (
            "pyproject.toml",
            "[tool.coverage.run]\nconcurrency = ['thread', 'gevent']\n",
            "unsupported [run] concurrency value(s): gevent",
        ),
        (
            "pyproject.toml",
            "[tool.coverage.run]\ncore = 'sysmonitor'\n",
            "unknown [run] core value: sysmonitor",
        ),
    ],
)
def test_invalid_configuration_prevents_execution(
    tmp_path: Path, filename: str, contents: str, message: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(filename, contents)
    project.write("program.py", "from pathlib import Path\nPath('executed').touch()\n")
    result = project.run_vfy("coverage", "run", f"--rcfile={filename}", "program.py")
    assert result.returncode == 1
    assert result.stdout == ""
    assert message in result.stderr
    assert not (tmp_path / "executed").exists()
    assert not (tmp_path / ".coverage").exists()


@pytest.mark.parametrize("cover_pylib", ["true", "false"])
def test_configured_cover_pylib_matches_coverage_py(tmp_path: Path, cover_pylib: str) -> None:
    project = VfyProject(tmp_path)
    project.write(".coveragerc", f"[run]\ncover_pylib = {cover_pylib}\n")
    project.write("sample.py", "from fractions import Fraction\nvalue = Fraction(2, 3) + 1\n")
    stock = project.run_coverage_py("run", "--data-file=.coverage-stock", "sample.py")
    stock.expect(0)
    native = project.run_vfy("coverage", "run", "--data-file=.coverage-vfy", "sample.py")
    native.expect(0)
    measured = {
        name: sorted(Path(path).name for path in project.data(name).measured_files())
        for name in (".coverage-stock", ".coverage-vfy")
    }
    assert ("fractions.py" in measured[".coverage-vfy"]) is (cover_pylib == "true")
    assert ("fractions.py" in measured[".coverage-stock"]) is (cover_pylib == "true")
    assert "sample.py" in measured[".coverage-vfy"]


@pytest.mark.parametrize(("configured", "override"), [(True, False), (False, True)])
def test_cover_pylib_constructor_overrides_configuration(
    tmp_path: Path, configured: bool, override: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(".coveragerc", f"[run]\ncover_pylib = {configured}\n")
    project.write("sample.py", "from fractions import Fraction\nvalue = Fraction(2, 3) + 1\n")
    project.write(
        "collect.py",
        "import importlib, runpy, sys\n"
        "cov = importlib.import_module(sys.argv[1]).Coverage(\n"
        f"    cover_pylib={override}, data_file=sys.argv[2])\n"
        f"assert cov.get_option('run:cover_pylib') is {override}\n"
        "cov.start()\n"
        "runpy.run_path('sample.py')\n"
        "cov.stop()\n"
        "cov.save()\n",
    )
    for package in ("coverage", "vfy"):
        result = project.run_python("collect.py", package, f".{package}")
        result.expect(0)
        measured = project.data(f".{package}").measured_files()
        assert any(Path(path).name == "fractions.py" for path in measured) is override
    assert project.lines("sample.py", ".vfy") == project.lines("sample.py", ".coverage") == [1, 2]


@pytest.mark.parametrize(
    ("option", "value"), [("cover_pylib", True), ("core", "ctrace"), ("core", "sysmon")]
)
def test_collection_options_set_through_api_match_coverage_py(
    tmp_path: Path, option: str, value: bool | str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "sample.py",
        "from fractions import Fraction\n"
        "def values():\n"
        "    for value in range(3):\n"
        "        if value:\n"
        "            yield Fraction(value, 3)\n"
        "result = list(values())\n",
    )
    project.write(
        "collect.py",
        "import importlib, runpy, sys\n"
        "cov = importlib.import_module(sys.argv[1]).Coverage(\n"
        "    config_file=False, branch=True, data_file=sys.argv[2])\n"
        f"cov.set_option('run:{option}', {value!r})\n"
        f"assert cov.get_option('run:{option}') == {value!r}\n"
        "cov.start()\n"
        "runpy.run_path('sample.py')\n"
        "cov.stop()\n"
        "cov.save()\n",
    )
    for package in ("coverage", "vfy"):
        result = project.run_python("collect.py", package, f".{package}")
        result.expect(0)
        measured = project.data(f".{package}").measured_files()
        assert any(Path(path).name == "fractions.py" for path in measured) is (
            option == "cover_pylib"
        )
    assert project.arcs("sample.py", ".vfy") == project.arcs("sample.py", ".coverage")


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize("environment_core", [None, "ctrace"])
def test_invalid_core_set_through_api_leaves_monitoring_available_for_followup(
    tmp_path: Path, branch: bool, environment_core: str | None
) -> None:
    project = VfyProject(tmp_path)
    project.write("sample.py", "value = 1\n")
    project.write(
        "collect.py",
        "import importlib, runpy, sys\n"
        "coverage_type = importlib.import_module(sys.argv[1]).Coverage\n"
        "cov = coverage_type(\n"
        f"    config_file=False, branch={branch}, data_file=sys.argv[2])\n"
        "cov.set_option('run:core', 'unknown')\n"
        "try:\n"
        "    cov.start()\n"
        "except Exception as error:\n"
        "    print(type(error).__name__, 'unknown' in str(error))\n"
        "else:\n"
        "    raise AssertionError('invalid core was accepted')\n"
        "cov = coverage_type(\n"
        f"    config_file=False, branch={branch}, data_file=sys.argv[2])\n"
        "cov.set_option('run:core', 'ctrace')\n"
        "cov.start()\n"
        "runpy.run_path('sample.py')\n"
        "cov.stop()\n"
        "cov.save()\n",
    )
    for package in ("coverage", "vfy"):
        result = project.run_python(
            "collect.py", package, f".{package}", env={"COVERAGE_CORE": environment_core}
        )
        result.expect(0)
        assert result.stdout == "ConfigError True\n"
    assert project.lines("sample.py", ".vfy") == project.lines("sample.py", ".coverage") == [1]
    assert project.arcs("sample.py", ".vfy") == project.arcs("sample.py", ".coverage")


@pytest.mark.parametrize(
    ("change", "initial_core"),
    [("environment", None), ("environment", "ctrace"), ("set_option", "ctrace")],
)
def test_core_environment_is_read_at_construction_before_api_overrides(
    tmp_path: Path, change: str, initial_core: str | None
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "sample.py",
        "def values():\n"
        "    for value in range(3):\n"
        "        if value:\n"
        "            yield value\n"
        "result = list(values())\n",
    )
    project.write(
        "collect.py",
        "import importlib, os, runpy, sys\n"
        "cov = importlib.import_module(sys.argv[1]).Coverage(\n"
        "    config_file=False, branch=True, data_file=sys.argv[2])\n"
        f"assert cov.get_option('run:core') == {initial_core!r}\n"
        f"if {change!r} == 'environment':\n"
        f"    os.environ['COVERAGE_CORE'] = {'ctrace' if initial_core is None else 'sysmon'!r}\n"
        "else:\n"
        "    cov.set_option('run:core', 'sysmon')\n"
        "cov.start()\n"
        "runpy.run_path('sample.py')\n"
        "cov.stop()\n"
        "cov.save()\n",
    )
    for package in ("coverage", "vfy"):
        result = project.run_python(
            "collect.py", package, f".{package}", env={"COVERAGE_CORE": initial_core}
        )
        result.expect(0)
    assert project.arcs("sample.py", ".vfy") == project.arcs("sample.py", ".coverage")
