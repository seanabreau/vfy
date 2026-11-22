"""Rust pytest config projection matches the installed pytest config parser."""

from __future__ import annotations

import errno
import json
import os
import shlex
import sys
import warnings
from contextlib import nullcontext
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict

import pytest
from _pytest.cacheprovider import Cache
from _pytest.config import get_config

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import CommandResult


class PytestObservation(TypedDict):
    addopts: list[str]
    rootdir: str
    config_file: str | None
    cache_dir: Path
    basetemp: Path | None
    filtered_scope: bool
    collect_only: bool


_PINNED_PYTEST = "9.1.1"
_PYTEST_TOML_ORACLE_REASON = "pytest TOML discovery differential uses pinned pytest 9.1.1"


def _pytest_observation(arguments: list[str]) -> PytestObservation:
    config = get_config(args=arguments)
    try:
        with warnings.catch_warnings():
            # pytest 9.0 on CPython 3.12 can trigger this argparse warning
            # while parsing an empty positional list; it is outside config semantics.
            warnings.filterwarnings(
                "ignore",
                message=r"Do not expect file_or_dir in Namespace\(.*",
                category=UserWarning,
                module=r"argparse",
            )
            config.parse(list(arguments))
        return {
            "collect_only": bool(config.option.collectonly),
            "filtered_scope": bool(
                config.option.keyword
                or config.option.markexpr
                or any(
                    config.getoption(name, None)
                    for name in (
                        "lf",
                        "failedfirst",
                        "newfirst",
                        # --sw-skip and --sw-reset enable stepwise in pytest_configure.
                        "stepwise",
                        "stepwise_skip",
                        "stepwise_reset",
                        "collectonly",
                        "ignore",
                        "ignore_glob",
                        "deselect",
                    )
                )
                or any("::" in target or target.endswith(".py") for target in config.args)
            ),
            "config_file": str(config.inipath) if config.inipath is not None else None,
            "rootdir": str(config.rootpath),
            "addopts": list(config.getini("addopts")),
            "cache_dir": Cache.cache_dir_from_config(config, _ispytest=True),
            "basetemp": (
                Path.cwd() / config.getoption("basetemp")
                if config.getoption("basetemp") is not None
                else None
            ),
        }
    finally:
        config._ensure_unconfigure()


_ALL_FIELDS = frozenset({"config_file", "rootdir", "addopts", "cache_dir", "scope"})
_RUN_VFY = (
    "import os, runpy, sys\n"
    "os.chdir(sys.argv[1])\n"
    "sys.argv = ['vfy', *sys.argv[2:]]\n"
    "runpy.run_module('vfy', run_name='__main__', alter_sys=True)\n"
)
_RUN_PYTEST = (
    "import os, sys\nos.chdir(sys.argv[1])\nimport pytest\nsys.exit(pytest.main(sys.argv[2:]))\n"
)
_SELECTION_REJECTED = "--all cannot be combined with pytest test-selection options"


def _assert_matches_pytest(
    tmp_path: Path,
    arguments: list[str],
    *,
    env_addopts: str | None = None,
    cwd: Path | None = None,
    environment: dict[str, str | None] | None = None,
    fields: set[str] | None = None,
    command_arguments: list[str] | None = None,
    unwritable_cache_reason: str | None = None,
) -> PytestObservation:
    """Compare the command's resolved configuration with pytest's own parser.

    ``vfy diagnostics --json`` reports the configuration file, rootdir, and
    addopts. A native run writes its cache in pytest's resolved directory, or
    matches pytest's warning and status when the fixture's home is read-only.
    ``vfy test --all`` rejects the same test-selection options pytest records in
    its parsed configuration. ``command_arguments`` replaces ``arguments`` for
    the command when pytest needs a plug-in that vfy supplies itself.
    """

    selected = _ALL_FIELDS if fields is None else fields
    directory = cwd or tmp_path
    values: dict[str, str | None] = {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": env_addopts,
        **(environment or {}),
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(directory)
        for key, value in values.items():
            if value is None:
                patch.delenv(key, raising=False)
            else:
                patch.setenv(key, value)
        expected = _pytest_observation(arguments)

    (tmp_path / "cli-home").mkdir(exist_ok=True)
    project = VfyProject(tmp_path / "cli-home")
    candidate = arguments if command_arguments is None else command_arguments

    def run(script: str, *command: str) -> CommandResult:
        return project.run_python("-c", script, str(directory), *command, env=values)

    diagnostics = run(_RUN_VFY, "diagnostics", "--json", "--engine=pytest-native", "--", *candidate)
    assert diagnostics.returncode == 0, diagnostics.stdout + diagnostics.stderr
    configuration = json.loads(diagnostics.stdout)["pytest_configuration"]
    assert configuration["error"] is None, configuration
    if "config_file" in selected:
        assert configuration["file"] == expected["config_file"]
    if "rootdir" in selected:
        assert configuration["rootdir"] == expected["rootdir"]
    if "addopts" in selected:
        assert configuration["configured_args"] == [
            *expected["addopts"],
            *(shlex.split(env_addopts) if env_addopts is not None else []),
        ]
    if "cache_dir" in selected:
        native = run(
            _RUN_VFY, "test", "--engine=pytest-native", "--workers=1", "--no-cov", *candidate
        )
        nodeids = expected["cache_dir"] / "v/cache/nodeids"
        if unwritable_cache_reason is None:
            assert nodeids.is_file(), native.stdout + native.stderr
        else:
            reference = run(_RUN_PYTEST, *candidate)
            assert native.returncode == reference.returncode == 0
            assert not nodeids.exists()
            warning_prefix = f"could not create cache path {expected['cache_dir']}"
            for result in (native, reference):
                output = result.stdout + result.stderr
                assert "PytestCacheWarning" in output, output
                assert warning_prefix in output, output
                assert unwritable_cache_reason in output, output
    if "scope" in selected:
        selection = run(
            _RUN_VFY,
            "test",
            "--all",
            "--engine=pytest-native",
            "--workers=1",
            "--no-cov",
            *candidate,
        )
        rejected = _SELECTION_REJECTED in selection.stderr
        assert rejected is expected["filtered_scope"], selection.stdout + selection.stderr
    return expected


# vfy test reads a bare --co as an ambiguous abbreviation of its own options.
_COLLECT_ONLY_CASES = [
    (flag, source)
    for flag in ("--collect-only", "--co", "--collectonly")
    for source in ("cli", "config", "environment", "argument-file", "override")
    if (flag, source) != ("--co", "cli")
]


@pytest.mark.parametrize(("flag", "source"), _COLLECT_ONLY_CASES)
@pytest.mark.skipif(version("pytest") != _PINNED_PYTEST, reason=_PYTEST_TOML_ORACLE_REASON)
def test_collect_only_projection_matches_pytest_across_argument_sources(
    tmp_path: Path, flag: str, source: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", f"[pytest]\naddopts = {flag if source == 'config' else ''}\n")
    project.write("arguments.txt", f"{flag}\n")
    arguments = (
        [flag]
        if source == "cli"
        else ["@arguments.txt"]
        if source == "argument-file"
        else ["-o", f"addopts={flag}"]
        if source == "override"
        else []
    )
    expected = _assert_matches_pytest(
        tmp_path,
        arguments,
        env_addopts=flag if source == "environment" else None,
        fields={"scope"},
    )
    assert expected["collect_only"] is True


@requires_native_pytest
@pytest.mark.parametrize("argument_source", ["cli", "environment"])
@pytest.mark.skipif(version("pytest") != _PINNED_PYTEST, reason=_PYTEST_TOML_ORACLE_REASON)
def test_recursive_argument_files_select_config_and_disable_coverage_without_loading_plugins(
    tmp_path: Path, argument_source: str
) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("upstream", "pytest-native")}
    for project in projects.values():
        project.write("test_case.py", "def test_case(): assert True\n")
        project.write("pytest.ini", "[pytest]\naddopts = -p nonexistent_plugin\n")
        project.write("chosen.ini", "[pytest]\naddopts = @flags.txt\n")
        project.write("nested/outer.txt", "@inner.txt\r\n")
        project.write("inner.txt", "-c\nchosen.ini\n")
        # pytest option parsing expands these tokens; plugin bootstrap scans raw argv.
        project.write("flags.txt", "-p\nnonexistent_plugin\n--no-cov\n-q\n")
    arguments = ["@nested/outer.txt"] if argument_source == "cli" else []
    environment = {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "@nested/outer.txt" if argument_source == "environment" else None,
    }
    stock = projects["upstream"].run_pytest(
        "-p", "vfy.pytest_plugin", *arguments, env=environment, load_xdist=False
    )
    native = projects["pytest-native"].run_vfy(
        "test", "--engine=pytest-native", "--workers=1", *arguments, env=environment
    )
    for result in (stock, native):
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
    assert "coverage is disabled" in native.stderr
    for project in projects.values():
        assert not (project.root / ".coverage").exists()
        assert not (project.root / ".vfy/replay.coverage").exists()


@pytest.mark.skipif(version("pytest") != _PINNED_PYTEST, reason=_PYTEST_TOML_ORACLE_REASON)
def test_argument_file_projection_matches_pytest_paths_and_literal_arguments(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("chosen.ini", "[pytest]\naddopts = -q\n")
    project.write("args.txt", "-c\nchosen.ini\n-o\ncache_dir=my cache\n--basetemp\nmy temp\n")
    _assert_matches_pytest(tmp_path, ["@args.txt"])


@requires_native_pytest
@pytest.mark.skipif(version("pytest") != _PINNED_PYTEST, reason=_PYTEST_TOML_ORACLE_REASON)
def test_argument_file_coverage_scope_is_not_overridden_by_controller_defaults(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    selected = project.write("src/app.py", "def value(): return 1\n")
    project.write("other/app.py", "def value(): return 2\n")
    project.write(
        "test_case.py",
        "from src.app import value as one\nfrom other.app import value as two\n"
        "def test_case(): assert one() + two() == 3\n",
    )
    project.write("args.txt", "--cov=src\n--cov-report=\n")
    result = project.run_vfy("test", "--engine=native", "--workers=1", "@args.txt")
    result.expect(0)
    assert project.data().measured_files() == {str(selected)}


@pytest.mark.skipif(version("pytest") != _PINNED_PYTEST, reason=_PYTEST_TOML_ORACLE_REASON)
def test_argument_file_test_filter_cannot_publish_an_all_tests_baseline(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_case.py", "def test_case(): assert True\n")
    project.write("args.txt", "-k\ncase\n")
    result = project.run_vfy(
        "test", "--engine=native", "--all", "--workers=1", "--no-cov", "@args.txt"
    )
    result.expect(1)
    assert "--all cannot be combined with pytest test-selection options" in result.stderr
    assert not (tmp_path / ".vfy").exists()


@requires_native_pytest
@pytest.mark.skipif(version("pytest") != _PINNED_PYTEST, reason=_PYTEST_TOML_ORACLE_REASON)
def test_external_argument_file_changes_invalidate_green_replay(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    project.write(".gitignore", ".coverage*\n.vfy/\n.pytest_cache/\n__pycache__/\n")
    project.write("app.py", "def value(): return 1\n")
    project.write(
        "tests_first/test_app.py", "from app import value\ndef test_app(): assert value() == 1\n"
    )
    project.write(
        "tests_second/test_app.py", "from app import value\ndef test_app(): assert value() == 2\n"
    )
    project.git_init()
    argument_file = tmp_path / "external.txt"
    argument_file.write_text(
        "-o\ntestpaths=tests_first\n--cov=app.py\n--cov-report=\n", encoding="utf-8"
    )
    arguments = ("test", "--workers=1", f"@{argument_file}")
    first = project.run_vfy(*arguments)
    first.expect(0)
    assert (project.root / ".vfy/replay.coverage").is_file()
    assert project.run("git", "status", "--porcelain").stdout == ""

    argument_file.write_text(
        "-o\ntestpaths=tests_second\n--cov=app.py\n--cov-report=\n", encoding="utf-8"
    )
    second = project.run_vfy(*arguments)
    second.expect(1)
    assert "FAILED tests_second/test_app.py::test_app" in second.stdout
    assert "No code changed" not in second.stderr


@pytest.mark.skipif(version("pytest") != _PINNED_PYTEST, reason=_PYTEST_TOML_ORACLE_REASON)
def test_argument_file_xdist_options_preserve_stock_and_native_workers(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_first.py", "def test_first(): pass\n")
    project.write("test_second.py", "def test_second(): pass\n")
    project.write("args.txt", "-n\n2\n--dist\nloadfile\n--no-cov\n")
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": None}
    stock = project.run_vfy(
        "test", "--engine=pytest", "--workers=auto", "@args.txt", env=environment
    )
    stock.expect(0)
    assert "2 workers [2 items]" in stock.stdout
    assert "2 passed" in stock.stdout
    conflicting = project.run_vfy(
        "test", "--engine=pytest", "--workers=2", "@args.txt", env=environment
    )
    conflicting.expect(1)
    assert "--workers cannot be combined with pytest -n/--numprocesses" in conflicting.stderr
    if sys.platform == "win32":
        pytest.skip("native pytest requires Unix")
    native = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=1", "@args.txt", env=environment
    )
    native.expect(0)
    assert "2 workers [2 items]" in native.stdout
    assert "2 passed" in native.stdout


@pytest.mark.parametrize(
    ("filename", "contents", "diagnostic"),
    [
        ("missing.txt", None, "failed to read pytest argument file"),
        ("cycle.txt", "@cycle.txt\n", "recursive pytest argument file"),
    ],
)
@pytest.mark.skipif(version("pytest") != _PINNED_PYTEST, reason=_PYTEST_TOML_ORACLE_REASON)
def test_invalid_argument_files_fail_before_project_outputs(
    tmp_path: Path, filename: str, contents: str | None, diagnostic: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_case.py", "def test_case(): assert True\n")
    if contents is not None:
        project.write(filename, contents)
    result = project.run_vfy("test", "--engine=native", "--workers=1", "--no-cov", f"@{filename}")
    result.expect(4)
    assert diagnostic in result.stderr
    assert result.stderr.startswith("ERROR: ")
    assert not (tmp_path / ".coverage").exists()
    assert not (tmp_path / ".vfy").exists()
    assert not (tmp_path / ".pytest_cache").exists()


def test_rust_projection_matches_pytest_for_nested_target_and_option_precedence(
    tmp_path: Path,
) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    root = tmp_path
    nested = root / "pkg" / "tests"
    nested.mkdir(parents=True)
    target = nested / "test_sample.py"
    target.write_text("def test_sample():\n    assert True\n", encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\naddopts = -x\n", encoding="utf-8")
    (nested / "pytest.toml").write_text(
        "[pytest]\naddopts = ['-q', '--basetemp=config-temp']\ncache_dir = 'toml-cache'\n",
        encoding="utf-8",
    )
    (nested / "pytest.ini").write_text("[pytest]\naddopts = -s\n", encoding="utf-8")
    env_addopts = "-v -o cache_dir=env-cache --basetemp env-temp"
    cwd = root
    arguments = [
        "--strict-config",
        "-k",
        "sample",
        "-s",
        "-o",
        "cache_dir=cli-cache",
        "--basetemp",
        "cli-temp",
        "pkg/tests/test_sample.py::test_sample",
    ]

    _assert_matches_pytest(
        tmp_path,
        arguments,
        env_addopts=env_addopts,
        cwd=cwd,
        environment=environment,
    )


@pytest.mark.parametrize(
    "option",
    ["--log-file", "--log-cli-format", "--doctest-glob", "--junit-prefix", "-r"],
)
def test_option_value_directory_does_not_select_nested_configuration(
    tmp_path: Path, option: str
) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = -q\n")
    project.write("a/pytest.ini", "[pytest]\naddopts = -x\n")
    cwd = tmp_path
    arguments = [option, "a"]

    _assert_matches_pytest(
        tmp_path,
        arguments,
        cwd=cwd,
        environment=environment,
        fields={"config_file", "rootdir", "addopts", "scope"},
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["-qk", "nested"],
        ["-vqk", "nested"],
        ["-qknested", "target"],
        ["-qk=nested", "target"],
        ["-kqcnested", "target"],
        ["-qc", "selected.ini", "-vo", "cache_dir=custom"],
        ["-vqcselected.ini", "-xocache_dir=custom"],
        ["-qc=selected.ini", "-qo=cache_dir=custom"],
    ],
)
def test_grouped_short_options_match_pytest_configuration(
    tmp_path: Path, arguments: list[str]
) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = -q\n")
    project.write("nested/pytest.ini", "[pytest]\naddopts = -p unrelated\n")
    project.write("selected.ini", "[pytest]\naddopts = -x\n")
    project.write("target/test_sample.py", "def test_sample(): pass\n")
    cwd = tmp_path

    _assert_matches_pytest(
        tmp_path,
        arguments,
        cwd=cwd,
        environment=environment,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["tests"],
        ["-rm"],
        ["-vk", "value"],
        ["-mfocus"],
        ["--junitxml", "report.py"],
        ["--lf"],
        ["--sw-skip"],
        ["--stepwise-reset"],
        ["--collectonly"],
        ["--ignore", "tests/other"],
        ["--ignore-glob=*_other.py"],
        ["--deselect", "tests/test_app.py::test_value"],
    ],
)
@pytest.mark.skipif(version("pytest") != _PINNED_PYTEST, reason=_PYTEST_TOML_ORACLE_REASON)
def test_filtered_scope_matches_pytest_selection_options(
    tmp_path: Path, arguments: list[str]
) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nmarkers = focus\n")
    project.write("tests/test_app.py", "def test_value(): pass\n")
    cwd = tmp_path

    _assert_matches_pytest(
        tmp_path,
        arguments,
        cwd=cwd,
        environment=environment,
        fields={"scope"},
    )


@pytest.mark.parametrize("option", ["-qk", "-vqk"])
@pytest.mark.skipif(os.name != "posix", reason="native execution requires Unix")
def test_native_grouped_keyword_ignores_unselected_directory_config(
    tmp_path: Path, option: str
) -> None:
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("nested/pytest.ini", "[pytest]\naddopts = -p unrelated\n")
        project.write("test_nested.py", "def test_nested(): pass\n")
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            option,
            "nested",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": None},
        )
        result.expect(0)
        assert "1 passed" in result.stdout


def test_config_free_external_target_uses_target_root_at_filesystem_boundary(
    tmp_path: Path,
) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    project = VfyProject(tmp_path)
    target = project.write("test_sample.py", "def test_sample(): pass\n")
    invocation_dir = Path(tmp_path.anchor)
    cwd = invocation_dir
    arguments = [str(target)]

    expected = _assert_matches_pytest(
        tmp_path,
        arguments,
        cwd=cwd,
        environment=environment,
        fields={"rootdir", "cache_dir"},
    )
    assert expected["rootdir"] == str(tmp_path)


@pytest.mark.parametrize("option", ["--debug", "--cache-show"])
def test_optional_value_does_not_consume_the_next_option(tmp_path: Path, option: str) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("selected/test_example.py", "def test_example(): pass\n")
    cwd = tmp_path
    arguments = [option, "--rootdir", "selected"]

    _assert_matches_pytest(
        tmp_path,
        arguments,
        cwd=cwd,
        environment=environment,
        fields={"rootdir"} if option == "--cache-show" else {"rootdir", "cache_dir"},
    )


@pytest.mark.parametrize(
    "following",
    [
        ["--rootdir", "selected"],
        ["--basetemp", "generated"],
        ["-c", "selected/pytest.ini"],
        ["-o", "cache_dir=generated"],
    ],
)
def test_bare_cov_preserves_pytest_configuration_options(
    tmp_path: Path, following: list[str]
) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("selected/pytest.ini", "[pytest]\naddopts = -q\n")
    cwd = tmp_path
    arguments = ["-p", "pytest_cov.plugin", "--cov", *following, "--no-cov"]

    _assert_matches_pytest(
        tmp_path,
        arguments,
        cwd=cwd,
        command_arguments=["--cov", *following, "--no-cov"],
        environment=environment,
    )


@pytest.mark.parametrize(
    ("config_addopts", "env_addopts", "arguments"),
    [
        ("-q -x", None, ["-o", "addopts=-v"]),
        ("-q -x", None, ["--override-ini=addopts="]),
        ("-q", "-o addopts=-s -v", []),
        ("-q -x", "-o addopts=-s -v", ["-o", "addopts=-v"]),
        ("-q -x", None, ["-o", "addopts='unterminated", "-o", "addopts=-v"]),
        ("'unterminated", None, ["-o", "addopts=-v"]),
    ],
)
@pytest.mark.skipif(
    version("pytest") != _PINNED_PYTEST,
    reason="addopts override differential uses pinned pytest 9.1.1",
)
def test_addopts_override_matches_pinned_pytest(
    tmp_path: Path,
    config_addopts: str,
    env_addopts: str | None,
    arguments: list[str],
) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    (tmp_path / "pytest.ini").write_text(
        f"[pytest]\naddopts = {config_addopts}\n", encoding="utf-8"
    )
    _assert_matches_pytest(
        tmp_path,
        arguments,
        env_addopts=env_addopts,
        cwd=cwd,
        environment=environment,
        fields={"addopts"},
    )


def test_rust_projection_matches_pytest_for_explicit_config_and_rootdir(tmp_path: Path) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    config_dir = tmp_path / "config"
    rootdir = tmp_path / "selected-root"
    config_dir.mkdir()
    rootdir.mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf-8")
    (config_dir / "arbitrary.ini").write_text(
        "[pytest]\naddopts = -x\ncache_dir = explicit-cache\n", encoding="utf-8"
    )
    (rootdir / "test_example.py").write_text("def test_example(): pass\n", encoding="utf-8")
    cwd = tmp_path
    arguments = [
        "-c",
        "config/arbitrary.ini",
        "--rootdir",
        "selected-root",
        "selected-root/test_example.py",
    ]

    _assert_matches_pytest(
        tmp_path,
        arguments,
        cwd=cwd,
        environment=environment,
    )


def test_explicit_rootdir_ignores_per_target_configurations_and_plugins(tmp_path: Path) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    project = VfyProject(tmp_path)
    project.write("a/pytest.ini", "[pytest]\naddopts = -p pytest_cov\ncache_dir = nested-cache\n")
    project.write("a/test_a.py", "def test_a(): pass\n")
    project.write("b/test_b.py", "def test_b(): pass\n")
    arguments = ["--rootdir=.", "a", "b"]
    cwd = tmp_path

    expected = _assert_matches_pytest(
        tmp_path,
        arguments,
        cwd=cwd,
        environment=environment,
    )
    assert expected["addopts"] == []
    assert expected["cache_dir"] == tmp_path / ".pytest_cache"


@requires_native_pytest
def test_explicit_rootdir_runs_targets_without_loading_nested_plugins(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("a/pytest.ini", "[pytest]\naddopts = -p pytest_cov\ncache_dir = nested-cache\n")
    project.write("a/test_a.py", "def test_a(): pass\n")
    project.write("b/test_b.py", "def test_b(): pass\n")
    arguments = ["--rootdir=.", "a", "b"]
    stock = project.run_pytest("-q", *arguments, load_xdist=False)
    native = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q", *arguments
    )
    for result in (stock, native):
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 passed" in result.stdout


@pytest.mark.skipif(os.name != "posix", reason="the pytest null-device config path is Unix-only")
def test_rust_projection_matches_pytest_for_null_device_config(tmp_path: Path) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    config_path = Path("/dev/null")
    if not config_path.exists():
        pytest.skip("the pytest null-device config path is unavailable")
    cwd = tmp_path
    # The null config makes /dev the rootdir; keep cache writes in the test project.
    arguments = ["-c", str(config_path), "-o", f"cache_dir={tmp_path / 'cache'}"]

    _assert_matches_pytest(
        tmp_path,
        arguments,
        cwd=cwd,
        environment=environment,
    )


def test_rust_projection_matches_pytest_tox_environment_cache_default(tmp_path: Path) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    tox_environment = tmp_path / ".tox" / "pytest-env"
    cwd = tmp_path
    environment["TOX_ENV_DIR"] = str(tox_environment)

    expected = _assert_matches_pytest(
        tmp_path,
        [],
        cwd=cwd,
        environment=environment,
        fields={"cache_dir"},
    )
    assert expected["cache_dir"] == tox_environment / ".pytest_cache"


def test_rust_projection_expands_home_before_environment_variables(tmp_path: Path) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    (tmp_path / "pytest.ini").write_text("[pytest]\ncache_dir = $VFY_CACHE\n", encoding="utf-8")
    cwd = tmp_path
    environment["VFY_CACHE"] = "~/pytest-cache"

    expected = _assert_matches_pytest(
        tmp_path,
        [],
        cwd=cwd,
        environment=environment,
        fields={"cache_dir"},
    )
    assert expected["cache_dir"] == tmp_path / "~" / "pytest-cache"


def test_rust_projection_preserves_unterminated_environment_variable(tmp_path: Path) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    expression = "${VFY_UNTERMINATED_CACHE_DIR"
    (tmp_path / "pytest.ini").write_text(f"[pytest]\ncache_dir = {expression}\n", encoding="utf-8")
    cwd = tmp_path
    environment["VFY_UNTERMINATED_CACHE_DIR"] = str(tmp_path / "expanded-cache")

    expected = _assert_matches_pytest(
        tmp_path,
        [],
        cwd=cwd,
        environment=environment,
        fields={"cache_dir"},
    )
    assert expected["cache_dir"] == tmp_path / expression


@pytest.mark.parametrize(
    ("config_path", "arguments"),
    [
        ("setup.cfg", []),
        ("config/custom.cfg", ["-c", "config/custom.cfg"]),
    ],
)
def test_rust_projection_matches_pytest_cfg_section_precedence(
    tmp_path: Path,
    config_path: str,
    arguments: list[str],
) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    path = tmp_path / config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[tool:pytest]\naddopts = -q\ncache_dir = selected-cache\n\n"
        "[pytest]\naddopts = -v\ncache_dir = ignored-cache\n",
        encoding="utf-8",
    )
    cwd = tmp_path

    expected = _assert_matches_pytest(
        tmp_path,
        arguments,
        cwd=cwd,
        environment=environment,
    )
    assert expected["addopts"] == ["-q"]


@pytest.mark.skipif(
    os.name != "posix",
    reason="native execution requires Unix",
)
def test_null_device_config_runs_through_stock_and_native_vfy_engines() -> None:
    with TemporaryDirectory(prefix="vfy-null-device-", dir="/tmp") as temporary_root:
        project_root = Path(temporary_root)
        projects = {
            name: VfyProject(project_root / name) for name in ("upstream", "stock", "pytest-native")
        }
        for project in projects.values():
            project.write("test_sample.py", "def test_sample():\n    assert True\n")

        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": None}
        upstream = projects["upstream"].run_pytest(
            "-c",
            "/dev/null",
            "--rootdir",
            str(projects["upstream"].root),
            "-q",
            env=env,
            load_xdist=False,
        )
        stock = projects["stock"].run_vfy(
            "test",
            "--engine=pytest",
            "--workers=1",
            "--no-cov",
            "-c",
            "/dev/null",
            "--rootdir",
            str(projects["stock"].root),
            "-q",
            env=env,
        )
        native = projects["pytest-native"].run_vfy(
            "test",
            "--engine=pytest-native",
            "--workers=1",
            "--no-cov",
            "-c",
            "/dev/null",
            "--rootdir",
            str(projects["pytest-native"].root),
            "-q",
            env=env,
        )

        assert upstream.returncode == stock.returncode == native.returncode == 0, (
            upstream.stdout
            + upstream.stderr
            + stock.stdout
            + stock.stderr
            + native.stdout
            + native.stderr
        )
        assert all("1 passed" in result.stdout for result in (upstream, stock, native))


@pytest.mark.skipif(
    os.name != "posix",
    reason="native execution requires Unix",
)
def test_cfg_section_precedence_runs_through_stock_and_native_engines(
    tmp_path: Path,
) -> None:
    projects = {
        name: VfyProject(tmp_path / name) for name in ("upstream", "stock", "pytest-native")
    }
    config = (
        "[tool:pytest]\naddopts = -q\ncache_dir = selected-cache\n\n"
        "[pytest]\naddopts = --not-a-pytest-option\n"
    )
    for project in projects.values():
        project.write("setup.cfg", config)
        project.write("test_sample.py", "def test_sample():\n    assert True\n")

    env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": None}
    upstream = projects["upstream"].run_pytest("test_sample.py", env=env, load_xdist=False)
    stock = projects["stock"].run_vfy(
        "test", "--engine=pytest", "--workers=1", "--no-cov", "test_sample.py", env=env
    )
    native = projects["pytest-native"].run_vfy(
        "test", "--engine=pytest-native", "--workers=1", "--no-cov", "test_sample.py", env=env
    )

    assert upstream.returncode == stock.returncode == native.returncode == 0, (
        upstream.stdout
        + upstream.stderr
        + stock.stdout
        + stock.stderr
        + native.stdout
        + native.stderr
    )


@pytest.mark.parametrize(
    ("relative_dir", "config_contents", "expected_addopts", "expected_cache"),
    [
        (
            "native-toml",
            "[tool.pytest]\naddopts = ['-q']\ncache_dir = 'native-cache'\n",
            ["-q"],
            "native-cache",
        ),
        (
            "ini-toml",
            "[tool.pytest.ini_options]\naddopts = '-x'\ncache_dir = 'ini-cache'\n",
            ["-x"],
            "ini-cache",
        ),
        (
            "native-with-empty-ini",
            "[tool.pytest]\naddopts = ['-q']\ncache_dir = 'native-cache'\n"
            "[tool.pytest.ini_options]\n",
            ["-q"],
            "native-cache",
        ),
        (
            "ini-integer-cache",
            "[tool.pytest.ini_options]\naddopts = '-q'\ncache_dir = 123\n",
            ["-q"],
            "123",
        ),
    ],
)
@pytest.mark.skipif(version("pytest") != _PINNED_PYTEST, reason=_PYTEST_TOML_ORACLE_REASON)
def test_rust_projection_matches_pytest_toml_modes(
    tmp_path: Path,
    relative_dir: str,
    config_contents: str,
    expected_addopts: list[str],
    expected_cache: str,
) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    project_dir = tmp_path / relative_dir
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text(config_contents, encoding="utf-8")
    cwd = project_dir

    expected = _assert_matches_pytest(
        tmp_path,
        [],
        cwd=cwd,
        environment=environment,
    )
    assert expected["addopts"] == expected_addopts
    assert expected["cache_dir"] == Path(expected["rootdir"]) / expected_cache


@requires_native_pytest
@pytest.mark.parametrize(
    ("config", "cache_name"),
    [
        (
            "[tool.pytest]\naddopts = ['-q']\ncache_dir = 'chosen-cache'\n"
            "[tool.pytest.ini_options]\n",
            "chosen-cache",
        ),
        ("[tool.pytest.ini_options]\naddopts = '-q'\ncache_dir = 123\n", "123"),
        ("[tool.pytest.ini_options]\ncache_dir = true\n", "True"),
        ("[tool.pytest.ini_options]\ncache_dir = 1.25\n", "1.25"),
        ("[tool.pytest.ini_options]\ncache_dir = 1.0\n", "1.0"),
        ("[tool.pytest.ini_options]\ncache_dir = 1e20\n", "1e+20"),
        ("[tool.pytest.ini_options]\ncache_dir = 1e-5\n", "1e-05"),
        (
            "[tool.pytest.ini_options]\ncache_dir = 1255208858886835.25\n",
            "1255208858886835.2",
        ),
    ],
)
@pytest.mark.skipif(version("pytest") != _PINNED_PYTEST, reason=_PYTEST_TOML_ORACLE_REASON)
def test_valid_pyproject_toml_configurations_run_through_native_vfy(
    tmp_path: Path, config: str, cache_name: str
) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("upstream", "pytest-native")}
    for project in projects.values():
        project.write("pyproject.toml", config)
        project.write("tests/test_case.py", "def test_case():\n    assert True\n")

    oracle = projects["upstream"].run_pytest(load_xdist=False)
    native = projects["pytest-native"].run_vfy(
        "test", "--engine=native", "--workers=1", "--no-replay", "--no-cov"
    )
    for result in (oracle, native):
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
    for project in projects.values():
        assert (project.root / cache_name / "v/cache/nodeids").is_file()


@pytest.mark.parametrize(
    "config_contents",
    [
        "[pytest] # defaults\naddopts = -q\ncache_dir = custom-cache\n",
        "[pytest] ; defaults\naddopts = -q\ncache_dir = custom-cache\n",
        "[pytest]\naddopts: --override-ini cache_dir=custom-cache\n",
        "[ pytest ]\naddopts = -x\ncache_dir = ignored-cache\n",
        "[pytest]\nignored = original\n [different]\naddopts = -q\n",
        "[pytest]\ncache_dir =\n    generated-cache\naddopts =\n    -q\n    -x\n",
    ],
)
def test_rust_projection_matches_pytest_ini_headers_and_delimiters(
    tmp_path: Path, config_contents: str
) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    (tmp_path / "pytest.ini").write_text(config_contents, encoding="utf-8")
    cwd = tmp_path

    _assert_matches_pytest(
        tmp_path,
        [],
        cwd=cwd,
        environment=environment,
    )


@requires_native_pytest
def test_empty_first_line_cache_path_preserves_source_archive_green_evidence(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\ncache_dir =\n    generated-cache\n")
    project.write("src/app.py", "def value():\n    return 1\n")
    project.write(
        "test_case.py", "from src.app import value\ndef test_case():\n    assert value() == 1\n"
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--workers=1",
        "--source=src",
        "--cov-report=",
        env={
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_ADDOPTS": None,
            "GIT_CEILING_DIRECTORIES": str(tmp_path),
        },
    )
    result.expect(0)
    assert (tmp_path / "generated-cache/v/cache/nodeids").is_file()
    assert (tmp_path / ".vfy/replay.coverage").is_file()
    assert (tmp_path / ".vfy/test-run.json").is_file()


@pytest.mark.parametrize(
    "home_expression", ["current-user", "current-home", "environment-home", "unknown-user"]
)
def test_rust_projection_matches_pytest_user_home_expansion(
    tmp_path: Path, home_expression: str
) -> None:
    environment: dict[str, str | None] = {}
    cwd = tmp_path
    pwd = pytest.importorskip("pwd")
    username = str(pwd.getpwuid(os.getuid()).pw_name)
    prefix = {
        "current-user": f"~{username}",
        "current-home": "~",
        "environment-home": "~",
        "unknown-user": "~vfy-unknown-user-47b61c0f",
    }[home_expression]
    if home_expression == "unknown-user":
        with pytest.raises(KeyError):
            pwd.getpwnam(prefix[1:])
    unwritable_cache_reason = None
    if home_expression in {"current-user", "current-home"}:
        passwd_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        try:
            home_cache_context = TemporaryDirectory(prefix="vfy-pytest-cache-", dir=passwd_home)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EPERM, errno.EROFS}:
                raise
            unwritable_cache_reason = os.strerror(exc.errno)
            home_cache_context = nullcontext(None)
    else:
        home_cache_context = nullcontext(None)

    with home_cache_context as home_cache:
        cache_prefix = (
            f"{prefix}/{Path(home_cache).name}"
            if home_cache is not None
            else f"{prefix}/vfy-test-cache-{os.getpid()}-{tmp_path.name}"
        )
        (tmp_path / "pytest.ini").write_text(
            f"[pytest]\ncache_dir = {cache_prefix}/vfy-cache\n", encoding="utf-8"
        )
        if home_expression == "current-home":
            environment["HOME"] = None
        else:
            environment["HOME"] = str(tmp_path / "configured-home")
        if unwritable_cache_reason is not None:
            (tmp_path / "test_cache.py").write_text("def test_cache(): pass\n", encoding="utf-8")

        _assert_matches_pytest(
            tmp_path,
            [],
            cwd=cwd,
            environment=environment,
            fields={"cache_dir"},
            unwritable_cache_reason=unwritable_cache_reason,
        )


@pytest.mark.parametrize(
    "config_contents",
    ["[pytest\naddopts = -q\n", " [pytest]\naddopts = -q\n", "[pytest]\n addopts = -q\n"],
)
def test_invalid_pytest_config_returns_usage_status_without_project_outputs(
    tmp_path: Path, config_contents: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", config_contents)
    project.write("tests/test_invalid_config.py", "def test_never_runs():\n    assert True\n")

    upstream = project.run_pytest(
        "tests/test_invalid_config.py",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": None},
        load_xdist=False,
    )
    result = project.run_vfy(
        "test",
        "--engine=pytest",
        "--workers=1",
        "--no-replay",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": None},
    )

    assert upstream.returncode == result.returncode == 4
    assert result.stderr.startswith("ERROR: ")
    assert not (tmp_path / ".coverage").exists()
    assert not (tmp_path / ".vfy").exists()
    assert not (tmp_path / ".pytest_cache").exists()
