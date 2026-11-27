"""Exit code, diagnostic, and stream behavior for every subcommand."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
from contextlib import suppress
from pathlib import Path
from types import FrameType

import pytest
from coverage import CoverageData as ReferenceData

import vfy
from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

DATA_COMMANDS = ("report", "xml", "json", "lcov", "html", "annotate", "debug")
ALL_COMMANDS = (*DATA_COMMANDS, "combine", "erase", "run")

ANSI = "\x1b["


def main(arguments: list[str]) -> int:
    """Run the real ``vfy`` command in the current directory and replay its streams."""

    result = VfyProject(Path.cwd()).run_vfy(*arguments)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def _arguments(command: str, target: str = "program.py") -> list[str]:
    if command in {"test", "mutate"}:
        return [command]
    if command == "debug":
        return ["coverage", "debug", "data"]
    if command == "run":
        return ["coverage", "run", target]
    return ["coverage", command]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with half of one file covered, so the total is exactly 50%."""
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "example.py"
    source.write_text("first = 1\nif first:\n    second = 2\nthird = 3\n", encoding="utf-8")
    (tmp_path / "program.py").write_text("value = 1\n", encoding="utf-8")
    data = ReferenceData(str(tmp_path / ".coverage"))
    data.add_lines({str(source): {1, 2}})
    data.write()
    return tmp_path


@pytest.mark.parametrize(
    ("command", "code", "out", "err"),
    [
        ("report", 0, None, ""),
        ("xml", 0, "", "Wrote XML report to coverage.xml\n"),
        ("json", 0, "", "Wrote JSON report to coverage.json\n"),
        ("lcov", 0, "", "Wrote LCOV report to coverage.lcov\n"),
        ("html", 0, "", "Wrote HTML report to htmlcov/index.html\n"),
        ("annotate", 0, "", "Annotated 1 file(s)\n"),
        ("erase", 0, "", ""),
    ],
)
def test_successful_commands_keep_diagnostics_off_stdout(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    code: int,
    out: str | None,
    err: str,
) -> None:
    assert main(_arguments(command)) == code
    captured = capsys.readouterr()
    if out is not None:
        assert captured.out == out
    assert captured.err == err


def test_debug_data_writes_its_payload_to_stdout(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["coverage", "debug", "data"]) == 0
    captured = capsys.readouterr()
    assert captured.out == (
        f"path: {project / '.coverage'}\n"
        "schema: 7\n"
        "has_arcs: False\n"
        "files: 1\n"
        f"  {project / 'example.py'}: 2 lines\n"
    )
    assert captured.err == ""


def test_semantic_check_help_lists_options_without_saved_data(tmp_path: Path) -> None:
    result = VfyProject(tmp_path).run_vfy("coverage", "check", "--help")

    assert result.returncode == 0
    assert "--fail-under-mcdc PERCENT" in result.stdout
    assert "--history PATH" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("command", DATA_COMMANDS)
def test_missing_data_reports_one_diagnostic_line(
    project: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    (project / ".coverage").unlink()

    assert main(_arguments(command)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "vfy: error: coverage data file not found: .coverage\n"


@pytest.mark.parametrize(
    ("command", "suggestion"),
    [
        ("report", "unknown command 'report'; use vfy coverage report"),
        ("check", "use vfy coverage check"),
    ],
)
def test_unsupported_command_suggests_the_valid_command(
    tmp_path: Path, command: str, suggestion: str
) -> None:
    result = VfyProject(tmp_path).run_vfy(command)

    assert result.returncode == 1
    assert suggestion in result.stderr


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_unrecognized_options_exit_two(project: Path, command: str) -> None:
    arguments = _arguments(command)
    result = VfyProject(project).run_vfy(arguments[0], "--not-an-option", *arguments[1:])

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.endswith("vfy: error: unrecognized arguments: --not-an-option\n")


@pytest.mark.parametrize(
    "arguments",
    [
        ("explain", "--rcfile", "setup.cfg", "app.py:1"),
        ("coverage", "check", "--rcfile", "setup.cfg"),
    ],
)
def test_commands_without_coverage_configuration_reject_rcfile(
    project: Path, arguments: tuple[str, ...]
) -> None:
    result = VfyProject(project).run_vfy(*arguments)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "vfy: error: unrecognized arguments: --rcfile" in result.stderr


@pytest.mark.parametrize(("version", "status"), [("9.1rc1", 0), ("9.1.invalid", 1)])
def test_pytest_engine_parses_installed_version_metadata(
    tmp_path: Path, version: str, status: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_example.py", "def test_example():\n    pass\n")
    project.write(
        "metadata/pytest-9.1.dist-info/METADATA",
        f"Metadata-Version: 2.1\nName: pytest\nVersion: {version}\n",
    )

    result = project.run_vfy(
        "test",
        "--engine=pytest",
        "--no-cov",
        "-q",
        env={"PYTHONPATH": str(tmp_path / "metadata")},
    )

    result.expect(status)
    if status == 0:
        assert "1 passed" in result.stdout
    else:
        assert result.stdout == ""
        assert f"cannot parse the installed pytest version {version!r}" in result.stderr


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_invalid_configuration_values_fail_before_any_work(
    project: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    (project / ".coveragerc").write_text("[report]\nprecision = 1.5\n", encoding="utf-8")

    assert main(_arguments(command)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"vfy: error: invalid value for [report] precision ({project / '.coveragerc'})\n"
    )


@pytest.mark.parametrize("command", ["report", "xml", "json", "lcov", "html"])
def test_threshold_shortfall_exits_two_with_the_measured_total(
    project: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    assert main([*_arguments(command), "--fail-under", "99.5"]) == 2
    captured = capsys.readouterr()
    assert captured.err.endswith("vfy: required coverage of 99.5% was not reached; total: 50.00%\n")


def test_threshold_matches_coverage_py_exit_codes(project: Path) -> None:
    reference = VfyProject(project).run_coverage_py(
        "report",
        "--fail-under=99.5",
    )

    assert reference.returncode == 2
    assert main(["coverage", "report", "--fail-under", "99.5"]) == 2


@pytest.mark.parametrize(
    ("precision", "threshold", "status"),
    [("0", "80", 0), ("2", "80", 2), ("1", "79.7", 0), ("2", "79.7", 2)],
)
def test_threshold_compares_the_total_rounded_to_the_report_precision(
    project: Path, precision: str, threshold: str, status: int
) -> None:
    source = project / "wide.py"
    source.write_text("".join(f"value_{index} = 1\n" for index in range(59)), encoding="utf-8")
    data = ReferenceData(str(project / ".coverage"))
    data.add_lines({str(source): set(range(1, 48))})
    data.write()
    options = ("report", f"--precision={precision}", f"--fail-under={threshold}")

    reference = VfyProject(project).run_coverage_py(*options)
    actual = VfyProject(project).run_vfy("coverage", *options)

    assert actual.returncode == reference.returncode, actual.stderr
    assert reference.returncode == status


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize(
    ("measured", "arguments"),
    [
        (False, ("report", "--fail-under=90")),
        (False, ("report", "--format=total")),
        (True, ("report", "--include=elsewhere/*")),
        (False, ("json",)),
        (False, ("xml",)),
        (False, ("lcov",)),
        (False, ("html",)),
        (False, ("annotate",)),
        (True, ("combine",)),
        (True, ("combine", "--keep")),
    ],
)
def test_nothing_to_report_or_combine_fails_like_coverage_py(
    tmp_path: Path, measured: bool, arguments: tuple[str, ...]
) -> None:
    project = VfyProject(tmp_path)
    source = project.write("example.py", "value = 1\n")
    data = ReferenceData(str(tmp_path / ".coverage"))
    data.add_lines({str(source): {1}} if measured else {})
    data.write()
    before = _tree(tmp_path)

    reference = project.run_coverage_py(*arguments)
    assert _tree(tmp_path) == before
    actual = project.run_vfy("coverage", *arguments)

    assert reference.returncode == 1
    assert reference.stdout in {"No data to report.\n", "No data to combine\n"}
    # vfy writes diagnostics to stderr; Coverage.py prints its message to stdout.
    assert (actual.returncode, actual.stdout, actual.stderr) == (
        reference.returncode,
        "",
        f"vfy: error: {reference.stdout}",
    )
    assert _tree(tmp_path) == before


def test_missing_data_matches_coverage_py_exit_code(project: Path) -> None:
    (project / ".coverage").unlink()
    reference = VfyProject(project).run_coverage_py("report")

    assert reference.returncode == 1
    assert main(["coverage", "report"]) == 1


def test_missing_source_and_ignore_errors(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "example.py").unlink()

    assert main(["coverage", "report"]) == 1
    assert capsys.readouterr().err.endswith(
        "No source for code: '" + str(project / "example.py") + "'.\n"
    )

    assert main(["coverage", "report", "--ignore-errors"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "vfy: error: No data to report.\n"


def test_combine_without_shards_keeps_the_strict_diagnostic(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["coverage", "combine", "--strict"]) == 1
    assert capsys.readouterr().err == "vfy: error: no coverage data files found\n"


def test_json_written_to_stdout_parses_as_json(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["coverage", "json", "-o", "-"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["meta"]["format"] == 3
    assert captured.err == ""


def test_xml_written_to_stdout_parses_as_xml(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["coverage", "xml", "-o", "-"]) == 0
    captured = capsys.readouterr()
    assert ET.fromstring(captured.out).tag == "coverage"
    assert captured.err == ""


def test_lcov_written_to_stdout_carries_no_diagnostics(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["coverage", "lcov", "-o", "-"]) == 0
    captured = capsys.readouterr()
    assert captured.out.endswith("end_of_record\n")
    assert captured.err == ""


def test_terminal_report_is_plain_when_stdout_is_not_a_terminal(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["coverage", "report"]) == 0
    assert ANSI not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("environment", "arguments", "colored"),
    [
        ({}, [], False),
        ({"FORCE_COLOR": "1"}, [], True),
        ({"NO_COLOR": "1", "FORCE_COLOR": "1"}, [], False),
        ({"NO_COLOR": "1"}, ["--color"], True),
        ({"FORCE_COLOR": "1"}, ["--no-color"], False),
    ],
)
def test_color_follows_the_flags_and_the_environment(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    arguments: list[str],
    colored: bool,
) -> None:
    for name in ("NO_COLOR", "FORCE_COLOR"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert main(["coverage", "report", *arguments]) == 0
    assert (ANSI in capsys.readouterr().out) is colored


@pytest.mark.skipif(sys.platform == "win32", reason="a pseudo-terminal requires POSIX")
def test_color_defaults_to_on_for_a_terminal(project: Path) -> None:
    import pty  # noqa: PLC0415 - POSIX-only pseudo-terminal

    master, slave = pty.openpty()
    environment = {k: v for k, v in os.environ.items() if k not in {"NO_COLOR", "FORCE_COLOR"}}
    try:
        process = subprocess.run(
            [sys.executable, "-m", "vfy", "coverage", "report"],
            cwd=project,
            env=environment,
            stdout=slave,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        # Darwin can discard buffered output when the last slave descriptor closes.
        import select  # noqa: PLC0415 - POSIX-only pseudo-terminal

        ready, _, _ = select.select([master], [], [], 5)
        assert ready, process.stderr
        output = os.read(master, 65536).decode()
    finally:
        os.close(master)
        os.close(slave)

    assert process.returncode == 0, process.stderr
    assert ANSI in output


@pytest.fixture
def covered_file(project: Path) -> Path:
    """Add a fully covered file to the project fixture."""
    source = project / "complete.py"
    source.write_text("value = 1\n", encoding="utf-8")
    data = ReferenceData(str(project / ".coverage"))
    data.read()
    data.add_lines({str(source): {1}})
    data.write()
    return source


@pytest.mark.parametrize(
    ("configured", "arguments", "listed"),
    [
        (None, [], True),
        (None, ["--skip-covered"], False),
        ("true", [], False),
        ("true", ["--no-skip-covered"], True),
        ("false", ["--skip-covered"], False),
    ],
)
def test_skip_covered_honors_the_flag_over_the_configuration(
    project: Path,
    covered_file: Path,
    capsys: pytest.CaptureFixture[str],
    configured: str | None,
    arguments: list[str],
    listed: bool,
) -> None:
    if configured is not None:
        (project / ".coveragerc").write_text(
            f"[report]\nskip_covered = {configured}\n", encoding="utf-8"
        )

    assert main(["coverage", "report", *arguments]) == 0
    assert (covered_file.name in capsys.readouterr().out) is listed


def test_machine_readable_reports_never_carry_ansi(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")

    assert main(["coverage", "json", "-o", "-"]) == 0
    assert ANSI not in capsys.readouterr().out


def test_cache_clean_rejects_root_links_without_touching_the_target(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    sentinel = project.write("outside/sentinel", "keep")
    cache = tmp_path / ".vfy"
    if sys.platform == "win32":
        result = project.run("cmd", "/c", "mklink", "/J", str(cache), str(sentinel.parent))
        result.expect(0)
    else:
        cache.symlink_to(sentinel.parent, target_is_directory=True)
    result = project.run_vfy("--isolated", "cache", "clean")
    assert result.returncode == 1
    assert "symlink or junction" in result.stderr
    assert sentinel.read_text() == "keep"
    assert os.path.lexists(cache)


def test_cache_cleanup_preserves_coverage_and_supports_quiet(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".vfy/nested/history.json", "{}")
    coverage = project.write(".coverage", "keep")
    for _ in range(2):
        result = project.run_vfy("--quiet", "--isolated", "cache", "clean")
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert not (tmp_path / ".vfy").exists()
    assert coverage.read_text() == "keep"


@pytest.mark.parametrize(
    ("section", "setting", "value", "command"),
    [
        ("test", "selection", '"sfae"', "test"),
        ("test", "workers", '"0"', "test"),
        ("mutate", "timeout", "-1", "mutate"),
        ("check", "format", '"yaml"', "check"),
        ("check", "fail_under_mcdc", "101", "check"),
    ],
)
def test_config_validation_and_execution_reject_the_same_domain_error(
    tmp_path: Path, section: str, setting: str, value: str, command: str
) -> None:
    project = VfyProject(tmp_path)
    config = project.write("pyproject.toml", f"[tool.vfy.{section}]\n{setting} = {value}\n")
    validation = project.run_vfy("config", "validate")
    execution = project.run_vfy(*_arguments(command))
    assert validation.returncode == execution.returncode == 1
    assert validation.stdout == execution.stdout == ""
    assert validation.stderr == execution.stderr
    assert setting in validation.stderr
    assert list(tmp_path.iterdir()) == [config]


@pytest.mark.parametrize(
    ("command", "option", "value", "key"),
    [
        ("mutate", "--timeout", "nan", "timeout"),
        ("mutate", "--timeout", "0", "timeout"),
        ("check", "--fail-under-mcdc", "101", "fail_under_mcdc"),
    ],
)
def test_invalid_cli_values_fail_before_any_artifacts(
    tmp_path: Path, command: str, option: str, value: str, key: str
) -> None:
    result = VfyProject(tmp_path).run_vfy("--isolated", *_arguments(command), option, value)
    assert result.returncode == 1
    assert result.stdout == ""
    assert key in result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("filename", "contents", "flags", "discovery"),
    [
        (".coveragerc", "[run]\nbranch = true\n", [], "Coverage.py fallback"),
        ("pyproject.toml", '[tool.vfy.test]\nworkers = "2"\n', [], "nearest [tool.vfy]"),
        ("custom.ini", "[run]\nbranch = true\n", ["--config", "custom.ini"], "explicit"),
        (".coveragerc", "[run]\nbranch = true\n", ["--isolated"], "isolated"),
    ],
)
def test_config_and_diagnostics_report_the_source_that_was_loaded(
    tmp_path: Path, filename: str, contents: str, flags: list[str], discovery: str
) -> None:
    project = VfyProject(tmp_path)
    source = project.write(filename, contents)
    env = {"COVERAGE_RCFILE": None}
    shown = project.run_vfy(*flags, "config", "show", env=env)
    diagnostics = project.run_vfy(*flags, "diagnostics", "--json", env=env)
    validated = project.run_vfy(*flags, "config", "validate", env=env)
    assert shown.returncode == diagnostics.returncode == validated.returncode == 0
    payload = json.loads(shown.stdout)
    expected = None if discovery == "isolated" else (filename if flags else str(source))
    diagnostics_payload = json.loads(diagnostics.stdout)
    assert payload["source"] == diagnostics_payload["config"] == expected
    assert diagnostics_payload["backend"] == "native"
    assert payload["provenance"]["discovery"] == discovery
    if expected is None:
        assert payload["settings"] == {}
    else:
        assert expected in validated.stdout
        assert payload["settings"]


@pytest.mark.parametrize("json_output", [False, True])
def test_diagnostics_accepts_coverage_source_pkgs_without_mutation(
    tmp_path: Path, json_output: bool
) -> None:
    project = VfyProject(tmp_path)
    config = project.write(
        "pyproject.toml",
        '[tool.coverage.run]\nsource_pkgs = ["packaging"]\n',
    )
    before = {
        path.relative_to(tmp_path): None if path.is_dir() else path.read_bytes()
        for path in tmp_path.rglob("*")
    }

    arguments = ("diagnostics", "--json") if json_output else ("diagnostics",)
    diagnostics = project.run_vfy(*arguments)

    diagnostics.expect(0)
    assert diagnostics.stderr == ""
    if json_output:
        payload = json.loads(diagnostics.stdout)
        configuration = payload["vfy_configuration"]
        assert configuration == {"status": "valid", "error": None, "action": None}
    else:
        assert "vfy configuration: valid" in diagnostics.stdout

    validation = project.run_vfy("config", "validate")
    validation.expect(0)
    assert f"Configuration is valid: {config}" in validation.stdout
    after = {
        path.relative_to(tmp_path): None if path.is_dir() else path.read_bytes()
        for path in tmp_path.rglob("*")
    }
    assert after == before
    assert not (tmp_path / ".coverage").exists()
    assert not (tmp_path / ".vfy").exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native test engine does not support Windows",
)
def test_diagnostics_accepts_current_supported_runtime_for_default_tests(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_ready.py", "def test_ready():\n    assert True\n")

    diagnostics = project.run_vfy("diagnostics", "--json")
    diagnostics.expect(0)
    readiness = json.loads(diagnostics.stdout)["test_readiness"]
    assert readiness["runtime_issues"] == []
    assert readiness["status"] == "needs_test_run"

    trial = project.run_vfy("test", "--all", "--no-cov", "-q")
    trial.expect(0)
    assert "1 passed" in trial.stdout


@pytest.mark.skipif(os.name != "posix", reason="independent execution requires POSIX")
def test_default_diagnostics_collects_without_pytest_and_keeps_native_checks_explicit(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(".coverage", "accepted coverage")
    project.write(".vfy/test-history.json", "accepted history")
    project.write(
        "conftest.py",
        "import pytest\nfrom pathlib import Path\n"
        "from importlib.metadata import PackageNotFoundError, version\n"
        "try:\n    version('pytest')\n"
        "except PackageNotFoundError:\n    pass\n"
        "else:\n    raise AssertionError('collection gained installed pytest')\n"
        "Path('conftest-imported').touch()\n"
        "@pytest.hookimpl(wrapper=True)\n"
        "def pytest_runtest_setup(item):\n    yield\n",
    )
    project.write(
        "test_ready.py",
        "from pathlib import Path\nimport pytest\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_ready(value):\n    Path('test-body-ran').touch()\n",
    )
    # Retain the actual vfy package without exposing its coinstalled dependencies.
    package_root = tmp_path / ".runtime"
    shutil.copytree(
        Path(vfy.__file__).parent,
        package_root / "vfy",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    environment = {"PYTHONPATH": str(package_root)}
    arguments = ("-S", "-m", "vfy", "diagnostics")
    initial = project.run_python(*arguments, "--json", env=environment)
    initial.expect(0)
    payload = json.loads(initial.stdout)
    assert payload["pytest"] is False
    assert payload["engine"] == "native"
    assert "native_readiness" not in payload
    assert payload["test_readiness"]["runtime_issues"] == []
    assert payload["test_readiness"]["status"] == "needs_test_run"
    assert payload["static_scan"]["performed"] is False
    assert payload["test_readiness"]["static_scan_complete"] is None
    assert not (tmp_path / "conftest-imported").exists()

    collected = project.run_python(*arguments, "--collect", "--json", env=environment)
    collected.expect(0)
    readiness = json.loads(collected.stdout)["test_readiness"]
    assert readiness["status"] == "collected_support_unknown", readiness
    assert readiness["collection"]["returncode"] == 0, readiness
    assert readiness["collection"]["collected_count"] == 2, readiness
    assert (tmp_path / "conftest-imported").exists()
    assert not (tmp_path / "test-body-ran").exists()
    assert (tmp_path / ".coverage").read_text() == "accepted coverage"
    assert (tmp_path / ".vfy/test-history.json").read_text() == "accepted history"

    native = project.run_python(*arguments, "--engine=pytest-native", "--json", env=environment)
    native.expect(0)
    payload = json.loads(native.stdout)
    assert payload["test_readiness"] == payload["native_readiness"]
    assert payload["native_readiness"]["status"] == "blocked"
    assert any(
        "pytest is not installed" in issue
        for issue in payload["native_readiness"]["runtime_issues"]
    )
    assert [hook["kind"] for hook in payload["runtime_hooks"]] == ["unsupported_hook_wrapper"]


@requires_native_pytest
def test_html_config_is_accepted_by_test(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pyproject.toml",
        "[tool.coverage.report]\nskip_covered = true\n"
        "[tool.coverage.html]\nskip_covered = false\nskip_empty = true\n",
    )
    project.write("test_sample.py", "def test_ok():\n    assert True\n")
    test = project.run_vfy("test", "--engine=native", "--all", "--no-cov", "-q")
    test.expect(0)


def test_html_coverage_command_applies_report_configuration(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pyproject.toml",
        "[tool.coverage.report]\nskip_covered = true\n"
        "[tool.coverage.html]\nskip_covered = false\nskip_empty = true\n",
    )
    full = project.write("full.py", "value = 1\n")
    partial = project.write("partial.py", "value = 2\n")
    empty = project.write("empty.py", "# no statements\n")
    data = ReferenceData(str(tmp_path / ".coverage"))
    data.add_lines(
        {str(full.resolve()): {1}, str(partial.resolve()): set(), str(empty.resolve()): set()}
    )
    data.write()

    default = project.run_vfy("coverage", "html", "-d", "all-html")
    default.expect(0)
    index = (tmp_path / "all-html" / "index.html").read_text(encoding="utf-8")
    assert "full.py" in index
    assert "partial.py" in index
    assert "empty.py" not in index

    filtered = project.run_vfy("coverage", "html", "-d", "filtered-html", "--skip-covered")
    filtered.expect(0)
    index = (tmp_path / "filtered-html" / "index.html").read_text(encoding="utf-8")
    assert "full.py" not in index
    assert "partial.py" in index


def test_diagnostics_reports_native_conflicts_from_pytest_addopts(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini",
        "[pytest]\naddopts = -n 8 --dist loadfile --cov-append -p anyio.pytest_plugin\n",
    )

    result = project.run_vfy("diagnostics", "--engine=pytest-native", "--json")

    result.expect(0)
    payload = json.loads(result.stdout)
    conflicts = payload["plugin_conflicts"]
    assert {conflict["kind"] for conflict in conflicts} == {"pytest_plugin"}
    assert payload["xdist_conflicts"] == []
    assert payload["native_readiness"]["status"] == "blocked"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native test engine does not support Windows",
)
@pytest.mark.parametrize(
    ("plugin_name", "fixture", "declaration"),
    [
        ("pytest_mock", "mocker", "['pytest_mock']"),
        ("pytest_mock.plugin", "mocker", "['pytest_mock.plugin']"),
        ("platformdirs.pytest_plugin", "platformdirs_isolated", "['platformdirs.pytest_plugin']"),
        ("pytester", "pytester", "['pytester']"),
        ("pytester", "pytester", "'pytester,pytester_assertions'"),
    ],
)
def test_diagnostics_accepts_supported_fixture_declarations_without_importing_conftest(
    tmp_path: Path, plugin_name: str, fixture: str, declaration: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "from pathlib import Path\n"
        "Path('conftest-imported').touch()\n"
        f"pytest_plugins = {declaration}\n",
    )
    project.write(
        "test_plugin.py", f"def test_plugin({fixture}):\n    assert {fixture} is not None\n"
    )

    diagnostics = project.run_vfy(
        "diagnostics", "--engine=pytest-native", "--json", "--", "-p", plugin_name
    )
    diagnostics.expect(0)
    payload = json.loads(diagnostics.stdout)
    assert payload["plugin_conflicts"] == []
    assert payload["native_readiness"]["status"] == "needs_native_trial"
    assert not (tmp_path / "conftest-imported").exists()

    collection = project.run_vfy("diagnostics", "--engine=pytest-native", "--collect", "--json")
    collection.expect(0)
    readiness = json.loads(collection.stdout)["native_readiness"]
    assert readiness["status"] == "collected_support_unknown", readiness
    assert readiness["collection"]["collected_count"] == 1
    assert (tmp_path / "conftest-imported").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
@pytest.mark.parametrize("declaration", [False, True])
def test_diagnostics_accepts_supported_xdist_options_and_hooks_without_importing_conftest(
    tmp_path: Path, declaration: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = -n 2 --dist=loadscope\n")
    project.write(
        "conftest.py",
        "from pathlib import Path\n"
        "Path('conftest-imported').touch()\n"
        + ("pytest_plugins = ['xdist.plugin']\n" if declaration else "")
        + "def pytest_configure_node(node):\n    node.workerinput['custom'] = True\n"
        "def pytest_runtest_logreport(report):\n    pass\n",
    )
    diagnostics = project.run_vfy("diagnostics", "--engine=pytest-native", "--json")
    diagnostics.expect(0)
    payload = json.loads(diagnostics.stdout)
    assert payload["plugin_conflicts"] == []
    assert payload["runtime_hooks"] == []
    assert payload["native_readiness"]["status"] == "needs_native_trial"
    assert not (tmp_path / "conftest-imported").exists()


def test_diagnostics_scans_conftests_without_importing_them(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "from pathlib import Path\n"
        "Path('conftest-imported').touch()\n"
        "pytest_plugins: list[str] = ['third_party']\n"
        "@pytest.hookimpl(specname='pytest_runtest_call')\n"
        "def pytest_custom_name(item): pass\n",
    )
    project.write("nested/conftest.py", "pytest_plugins = make_plugins()\n")
    project.write("broken/conftest.py", "def pytest_broken(\n")
    project.write(".venv/conftest.py", "pytest_plugins = ['ignored_environment']\n")
    project.write("vendor/conftest.py", "pytest_plugins = ['ignored_vendor']\n")

    result = project.run_vfy("diagnostics", "--engine=pytest-native", "--json")

    result.expect(0)
    payload = json.loads(result.stdout)
    assert payload["static_scan"]["conftest_count"] == 3
    assert payload["static_scan"]["truncated"] is False
    conflicts = [row for row in payload["plugin_conflicts"] if row["kind"] == "conftest_plugin"]
    assert len(conflicts) == 2
    assert "third_party" in conflicts[0]["detail"]
    assert "cannot statically resolve" in conflicts[1]["detail"]
    hooks = payload["runtime_hooks"]
    assert [row["kind"] for row in hooks] == ["syntax_error", "unsupported_hook"]
    assert hooks[1]["name"] == "pytest_runtest_call"
    assert hooks[1]["line"] == 5
    assert not (tmp_path / "conftest-imported").exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native test engine does not support Windows",
)
@pytest.mark.parametrize("phase", ["setup", "teardown"])
@pytest.mark.parametrize("wrapper_option", ["wrapper", "hookwrapper"])
@pytest.mark.parametrize("specname_style", ["omitted", "alias", "empty"])
def test_diagnostics_identifies_phase_wrappers_rejected_by_native_execution(
    tmp_path: Path, phase: str, wrapper_option: str, specname_style: str
) -> None:
    project = VfyProject(tmp_path)
    hook_name = f"pytest_runtest_{phase}"
    function_name = "phase_alias" if specname_style == "alias" else hook_name
    specname = (
        f"specname={hook_name!r}, "
        if specname_style == "alias"
        else "specname='', "
        if specname_style == "empty"
        else ""
    )
    project.write(
        "conftest.py",
        "import pytest\n"
        "from pathlib import Path\n"
        "Path('conftest-imported').touch()\n"
        f"@pytest.hookimpl({specname}{wrapper_option}=True)\n"
        f"def {function_name}(item):\n"
        "    yield\n",
    )
    project.write(
        "test_blocked.py",
        "from pathlib import Path\ndef test_blocked():\n    Path('test-body-ran').touch()\n",
    )

    diagnostics = project.run_vfy("diagnostics", "--engine=pytest-native", "--json")
    diagnostics.expect(0)
    payload = json.loads(diagnostics.stdout)
    assert payload["native_readiness"]["status"] == "potentially_blocked"
    assert len(payload["runtime_hooks"]) == 1
    hook = payload["runtime_hooks"][0]
    assert hook["kind"] == "unsupported_hook_wrapper"
    assert hook["name"] == hook_name
    assert Path(hook["path"]) == tmp_path / "conftest.py"
    assert hook["line"] == 5
    assert not (tmp_path / "conftest-imported").exists()

    collect = project.run_vfy("diagnostics", "--engine=pytest-native", "--collect", "--json")
    collect.expect(0)
    readiness = json.loads(collect.stdout)["native_readiness"]
    assert readiness["status"] == "blocked"
    assert readiness["collection"]["status"] == "failed"
    assert hook_name in readiness["collection"]["diagnostic"]

    execution = project.run_vfy("test", "--engine=pytest-native", "--all", "--no-cov", "-q")
    assert execution.returncode != 0, execution.stdout + execution.stderr
    assert "conftest phase hook wrappers" in execution.stdout + execution.stderr
    assert hook_name in execution.stdout + execution.stderr
    assert not (tmp_path / "test-body-ran").exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native test engine does not support Windows",
)
@pytest.mark.parametrize("phase", ["setup", "teardown"])
@pytest.mark.parametrize(
    "declaration",
    [
        "@pytest.hookimpl(wrapper=False, hookwrapper=False)\ndef pytest_runtest_{phase}(item):\n",
        "@pytest.hookimpl(wrapper=False)\n"
        "@pytest.hookimpl(wrapper=True)\n"
        "def pytest_runtest_{phase}(item):\n",
        "@pytest.hookimpl\n@pytest.hookimpl(wrapper=True)\ndef pytest_runtest_{phase}(item):\n",
        "@pytest.hookimpl(specname='pytest_runtest_{phase}', hookwrapper=False)\n"
        "@pytest.hookimpl(specname='pytest_runtest_call', hookwrapper=True)\n"
        "def pytest_phase_alias(item):\n",
    ],
    ids=[
        "disabled",
        "outer-wrapper-disabled",
        "outer-defaults",
        "outer-specname-and-hookwrapper-disabled",
    ],
)
def test_diagnostics_accepts_plain_phase_hooks_with_effective_wrapper_options(
    tmp_path: Path, phase: str, declaration: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import pytest\n"
        "from pathlib import Path\n"
        + declaration.format(phase=phase)
        + "    Path('phase-ran').touch()\n",
    )
    project.write("test_ready.py", "def test_ready():\n    assert True\n")

    diagnostics = project.run_vfy("diagnostics", "--engine=pytest-native", "--json")
    diagnostics.expect(0)
    payload = json.loads(diagnostics.stdout)
    assert payload["runtime_hooks"] == []
    assert payload["native_readiness"]["status"] == "needs_native_trial"

    reference = project.run_pytest("-q", load_xdist=False)
    reference.expect(0)
    assert "1 passed" in reference.stdout
    assert (tmp_path / "phase-ran").exists()
    (tmp_path / "phase-ran").unlink()

    execution = project.run_vfy("test", "--engine=pytest-native", "--all", "--no-cov", "-q")
    execution.expect(0)
    assert "1 passed" in execution.stdout
    assert (tmp_path / "phase-ran").exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native test engine does not support Windows",
)
def test_diagnostics_shares_conftest_hook_rejection_with_native_execution(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "def pytest_runtest_call(item):\n    raise AssertionError('the test body must not run')\n",
    )
    project.write("test_blocked.py", "def test_blocked():\n    assert True\n")

    diagnostics = project.run_vfy("diagnostics", "--engine=pytest-native", "--json")
    collect = project.run_vfy("diagnostics", "--engine=pytest-native", "--collect", "--json")
    execution = project.run_vfy("test", "--engine=pytest-native", "--no-cov")

    assert diagnostics.returncode == collect.returncode == 0, diagnostics.stderr + collect.stderr
    payload = json.loads(diagnostics.stdout)
    hook = next(row for row in payload["runtime_hooks"] if row["name"] == "pytest_runtest_call")
    assert hook["path"].endswith("conftest.py")
    assert hook["scope"] == "project-wide static scan"
    assert payload["native_readiness"]["status"] == "potentially_blocked"
    collection = json.loads(collect.stdout)["native_readiness"]["collection"]
    assert collection["status"] == "failed"
    assert "pytest_runtest_call" in collection["diagnostic"]
    assert json.loads(collect.stdout)["native_readiness"]["status"] == "blocked"
    assert execution.returncode != 0
    assert "pytest_runtest_call" in execution.stderr
    assert "the test body must not run" not in execution.stderr


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native test engine does not support Windows",
)
def test_diagnostics_collect_reports_exact_parametrized_count_without_running_tests(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_collection.py",
        "from pathlib import Path\n"
        "import pytest\n\n"
        "def test_one():\n    Path('test-body-ran').write_text('yes')\n\n"
        "@pytest.mark.parametrize('value', [1, 2, 3])\n"
        "def test_many(value):\n    assert value > 0\n",
    )
    project.write("test_not_selected.py", "def test_not_selected():\n    pass\n")

    result = project.run_vfy("diagnostics", "--collect", "--json", "--", "test_collection.py")

    result.expect(0)
    payload = json.loads(result.stdout)
    collection = payload["test_readiness"]["collection"]
    assert collection["status"] == "collected", collection
    assert collection["collected_count"] == 4, collection
    assert collection["support"] == "unknown"
    assert payload["test_readiness"]["status"] == "collected_support_unknown"
    assert not (tmp_path / "test-body-ran").exists()
    assert not (tmp_path / ".coverage").exists()
    assert not (tmp_path / ".vfy" / "test-history.sqlite3").exists()

    text = project.run_vfy("diagnostics", "--collect", "--", "test_collection.py")

    text.expect(0)
    assert "4 tests collected" in text.stdout
    assert (
        "Independent readiness: collection passed; execution support remains unknown" in text.stdout
    )
    assert "collected_support_unknown" not in text.stdout


@requires_native_pytest
def test_diagnostics_collect_reports_registered_distributions(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "diagnostics_probe-1.0.dist-info/METADATA",
        "Metadata-Version: 2.1\nName: diagnostics-probe\nVersion: 1.0\n",
    )
    project.write(
        "diagnostics_probe-1.0.dist-info/entry_points.txt",
        "[pytest11]\ndiagnostics_probe = diagnostics_probe\n",
    )
    project.write("diagnostics_probe.py", "def pytest_collection_finish(session):\n    pass\n")
    project.write("test_ok.py", "def test_ok():\n    pass\n")
    arguments = ("diagnostics", "--engine=native", "--collect")
    load = ("--", "-p", "diagnostics_probe")

    disabled = project.run_vfy(*arguments, "--json").expect(0)
    collected = project.run_vfy(*arguments, "--json", *load).expect(0)
    assert json.loads(disabled.stdout)["test_readiness"]["collection"]["loaded_plugins"] == []
    collection = json.loads(collected.stdout)["test_readiness"]["collection"]
    assert collection["loaded_plugins"] == [
        {"distribution": "diagnostics-probe", "name": "diagnostics_probe", "version": "1.0"}
    ]
    text = project.run_vfy(*arguments, *load).expect(0)
    assert "diagnostics-probe 1.0 (diagnostics_probe)" in text.stdout


@requires_native_pytest
@pytest.mark.parametrize("missing", ["vfy_missing_dependency", "_pytest._vfy_missing_private"])
def test_diagnostics_collect_identifies_plug_in_import_failure(
    tmp_path: Path, missing: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "diagnostics_probe-1.0.dist-info/METADATA",
        "Metadata-Version: 2.1\nName: diagnostics-probe\nVersion: 1.0\n",
    )
    project.write(
        "diagnostics_probe-1.0.dist-info/entry_points.txt",
        "[pytest11]\ndiagnostics_probe = diagnostics_probe_module\n",
    )
    project.write("diagnostics_probe_module.py", f"import {missing}\n")
    project.write("test_ok.py", "def test_ok():\n    pass\n")

    result = project.run_vfy(
        "diagnostics",
        "--engine=native",
        "--collect",
        "--json",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None},
    ).expect(0)

    collection = json.loads(result.stdout)["test_readiness"]["collection"]
    assert collection["status"] == "failed"
    assert collection["loaded_plugins"] is None
    diagnostic = collection["diagnostic"]
    assert "diagnostics_probe_module.py" in diagnostic
    assert f"No module named '{missing}'" in diagnostic
    assert (
        "Failed to load pytest plug-in 'diagnostics-probe' "
        "(entry point 'diagnostics_probe', module 'diagnostics_probe_module')."
    ) in diagnostic
    assert ("vfy test --engine=pytest" in diagnostic) == missing.startswith("_pytest.")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native test engine does not support Windows",
)
def test_diagnostics_collect_counts_tests_when_addopts_forces_color(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = --color=yes\n")
    project.write("test_collection.py", "def test_one():\n    pass\n\ndef test_two():\n    pass\n")

    result = project.run_vfy("diagnostics", "--collect")

    result.expect(0)
    assert "2 tests collected; execution support remains unknown" in result.stdout
    assert "\x1b" not in result.stdout


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native test engine does not support Windows",
)
def test_diagnostics_collect_reports_unsupported_items_and_missing_fixtures(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import pytest\n\n"
        "class CheckItem(pytest.Item):\n"
        "    def runtest(self):\n"
        "        pass\n\n"
        "class CheckFile(pytest.File):\n"
        "    def collect(self):\n"
        "        yield CheckItem.from_parent(self, name='check')\n\n"
        "def pytest_collect_file(parent, file_path):\n"
        "    if file_path.suffix == '.chk':\n"
        "        return CheckFile.from_parent(parent, path=file_path)\n",
    )
    project.write("types.chk", "")
    project.write(
        "test_speed.py",
        "def test_fast(benchmark):\n    benchmark(sum, [1])\n\n"
        "def test_slow(benchmark):\n    benchmark(sum, [2])\n\n"
        "def test_plain():\n    pass\n",
    )

    result = project.run_vfy("diagnostics", "--engine=pytest-native", "--collect", "--json")

    result.expect(0)
    readiness = json.loads(result.stdout)["native_readiness"]
    assert readiness["status"] == "blocked"
    assert readiness["collection"]["unsupported_items"] is True
    assert readiness["collection"]["missing_fixtures"] is True
    text = project.run_vfy("diagnostics", "--engine=pytest-native", "--collect")
    assert (
        "vfy: native execution does not run 1 collected item, "
        "such as types.chk::check has type conftest.CheckItem"
    ) in text.stdout
    assert (
        "vfy: collected tests request fixtures that no loaded plug-in or conftest.py defines: "
        "benchmark (2 tests)"
    ) in text.stdout

    (tmp_path / "types.chk").unlink()
    fixtures_only = project.run_vfy("diagnostics", "--engine=pytest-native", "--collect", "--json")
    readiness = json.loads(fixtures_only.stdout)["native_readiness"]
    assert readiness["status"] == "potentially_blocked"
    assert readiness["collection"]["unsupported_items"] is False


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native test engine does not support Windows",
)
def test_diagnostics_collect_omits_test_ids_that_mention_errors(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_errors.py",
        "def test_value_error_message():\n    pass\n\n"
        "def test_collected_parser_error():\n    pass\n",
    )

    result = project.run_vfy("diagnostics", "--collect")

    result.expect(0)
    assert "2 tests collected; execution support remains unknown" in result.stdout
    assert "test_errors.py::" not in result.stdout


@pytest.mark.skipif(os.name != "posix", reason="collection process groups require POSIX")
@pytest.mark.parametrize("engine", ["pytest-native", "native"])
@pytest.mark.parametrize("stop", ["interrupt", "terminate"])
def test_diagnostics_collection_stops_descendants_and_can_be_reused(
    tmp_path: Path, stop: str, engine: str
) -> None:
    import fcntl  # noqa: PLC0415 - POSIX-only process-liveness proof

    project = VfyProject(tmp_path, timeout=25)
    project.write(".coverage", "accepted coverage")
    project.write(".vfy/test-history.json", "accepted history")
    project.write(
        "descendant.py",
        "import fcntl, os, signal\n"
        "from pathlib import Path\n"
        "with open('descendant.lock', 'w') as stream:\n"
        "    fcntl.flock(stream, fcntl.LOCK_EX)\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    Path('descendant.pid').write_text(str(os.getpid()))\n"
        "    print('ready', flush=True)\n"
        "    signal.pause()\n",
    )
    project.write(
        "conftest.py",
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, 'descendant.py'], stdout=subprocess.PIPE, text=True\n"
        ")\n"
        "assert child.stdout.readline() == 'ready\\n'\n"
        "Path('collection-ready').touch()\n"
        "time.sleep(60)\n",
    )
    project.write("test_case.py", "def test_case():\n    pass\n")
    try:
        command = project.start_vfy("diagnostics", f"--engine={engine}", "--collect", "--json")
        signum = signal.SIGINT if stop == "interrupt" else signal.SIGTERM
        try:
            project.wait_for_path("collection-ready", timeout=10)
            command.send_signal(signum)
            result = command.wait(timeout=20)
        finally:
            if command.poll() is None:
                command.terminate()
                command.wait(timeout=20)
        assert result.returncode == -signum, result.stderr
        if stop == "interrupt":
            assert "KeyboardInterrupt" in result.stderr
        assert (tmp_path / "collection-ready").exists(), result.stderr

        def cleanup_deadline(_signum: int, _frame: FrameType | None) -> None:
            raise AssertionError("descendant survived owned process-group cleanup")

        # SIGKILL delivery can precede the descendant's file-descriptor release.
        previous_alarm = signal.getsignal(signal.SIGALRM)
        assert previous_alarm is not None
        signal.signal(signal.SIGALRM, cleanup_deadline)
        try:
            signal.alarm(5)
            with (tmp_path / "descendant.lock").open() as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_alarm)
        assert (tmp_path / ".coverage").read_text() == "accepted coverage"
        assert (tmp_path / ".vfy/test-history.json").read_text() == "accepted history"
        project.write("conftest.py", "")
        recovered = project.run_vfy("diagnostics", f"--engine={engine}", "--collect", "--json")
        recovered.expect(0)
        collection = json.loads(recovered.stdout)["test_readiness"]["collection"]
        assert collection["status"] == "collected", collection
        assert collection["collected_count"] == 1, collection
    finally:
        pid_path = tmp_path / "descendant.pid"
        if pid_path.exists():
            with (tmp_path / "descendant.lock").open() as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    with suppress(ProcessLookupError):
                        os.kill(int(pid_path.read_text()), signal.SIGKILL)


@pytest.mark.parametrize("explicit", [False, True])
def test_explicit_cli_defaults_override_config_in_execution(tmp_path: Path, explicit: bool) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pyproject.toml",
        '[tool.vfy.mutate]\nworkers = "2"\ntimeout = 9\nhistory = "custom/history.json"\n',
    )
    flags = ["--workers", "auto", "--timeout", "30", "--history", ".vfy/test-history.sqlite3"]
    result = project.run_vfy(
        "--verbose", "mutate", "--max-mutations", "-1", *(flags if explicit else [])
    )
    # Reject the mutation limit after resolving settings, without launching workers.
    assert result.returncode == 1
    assert "vfy: workers: " + ("auto" if explicit else "2") + "\n" in result.stderr
    assert "vfy: timeout: " + ("30.0" if explicit else "9.0") + "\n" in result.stderr
    assert (
        "vfy: history: "
        + (".vfy/test-history.sqlite3" if explicit else "custom/history.json")
        + "\n"
        in result.stderr
    )
    assert not (tmp_path / ".vfy").exists()


def test_quiet_suppresses_confirmations_but_keeps_errors_and_reports(project: Path) -> None:
    command = VfyProject(project)
    saved = command.run_vfy("--quiet", "coverage", "json")
    assert (saved.returncode, saved.stdout, saved.stderr) == (0, "", "")
    assert json.loads((project / "coverage.json").read_text())["meta"]["format"] == 3
    normal = command.run_vfy("coverage", "report")
    quiet = command.run_vfy("--quiet", "coverage", "report")
    verbose = command.run_vfy("--verbose", "coverage", "report")
    assert normal.stdout == quiet.stdout == verbose.stdout
    assert quiet.stderr == ""
    assert "vfy: Configuration:" in verbose.stderr
    error = command.run_vfy("--quiet", "coverage", "report", "--data-file", "missing")
    assert error.returncode == 1
    assert "coverage data file not found" in error.stderr


@pytest.mark.parametrize(
    ("arguments", "output"),
    [
        (("report",), None),
        (("annotate",), None),
        (("html",), "htmlcov/index.html"),
        (("json",), "coverage.json"),
        (("xml",), "coverage.xml"),
        (("lcov",), "coverage.lcov"),
        (("combine", ".coverage.shard"), ".coverage"),
        (("erase",), None),
        (("debug", "data"), None),
        (("run", "program.py"), None),
    ],
)
def test_subcommand_quiet_option_matches_coverage_py(
    tmp_path: Path, arguments: tuple[str, ...], output: str | None
) -> None:
    for engine in ("coverage", "vfy"):
        project = VfyProject(tmp_path / engine)
        source = project.write("example.py", "value = 1\n")
        project.write("program.py", "value = 1\n")
        name = ".coverage.shard" if arguments[0] == "combine" else ".coverage"
        data = ReferenceData(str(project.root / name))
        data.add_lines({str(source): {1}})
        data.write()
        # Coverage.py accepts the option after the command name, before positionals.
        command = (arguments[0], "-q", *arguments[1:])
        result = (
            project.run_coverage_py(*command)
            if engine == "coverage"
            else project.run_vfy("coverage", *command)
        )
        if output is not None:
            result.expect(0)
            assert (result.stdout, result.stderr) == ("", "")
            assert (project.root / output).stat().st_size > 0
            if arguments[0] == "combine":
                assert not (project.root / name).exists()
        elif engine == "coverage":
            result.expect(1)
            assert "no such option: -q" in result.stderr
        else:
            result.expect(2)
            assert "unrecognized arguments: -q" in result.stderr


def test_subcommand_quiet_option_keeps_stdout_payload_and_errors(project: Path) -> None:
    command = VfyProject(project)
    normal = command.run_vfy("coverage", "json", "-o", "-")
    quiet = command.run_vfy("coverage", "json", "--quiet", "-o", "-")

    normal.expect(0)
    quiet.expect(0)
    assert quiet.stderr == ""
    assert json.loads(quiet.stdout)["totals"] == json.loads(normal.stdout)["totals"]

    error = command.run_vfy("coverage", "json", "--quiet", "--data-file", "missing")
    error.expect(1)
    assert error.stdout == ""
    assert "coverage data file not found" in error.stderr


def test_subcommand_quiet_option_conflicts_with_verbose(project: Path) -> None:
    conflict = VfyProject(project).run_vfy("--verbose", "coverage", "json", "--quiet")

    assert conflict.returncode == 2
    assert conflict.stdout == ""
    assert "argument -q/--quiet: not allowed with argument -v/--verbose" in conflict.stderr
    assert not (project / "coverage.json").exists()


def test_verbose_keeps_json_clean_and_conflicts_with_quiet(tmp_path: Path) -> None:
    command = VfyProject(tmp_path)
    result = command.run_vfy("--verbose", "--isolated", "config", "show")
    assert result.returncode == 0
    assert json.loads(result.stdout)["settings"] == {}
    assert "Configuration: defaults (isolated)" in result.stderr
    conflict = command.run_vfy("--quiet", "--verbose", "config", "show")
    assert conflict.returncode == 2
    assert conflict.stdout == ""
    assert "not allowed with argument" in conflict.stderr


@pytest.mark.parametrize(
    ("flag", "pytest_flags", "verbosity"),
    [("--quiet", [], 0), ("--quiet", ["-q"], -1), ("--verbose", [], 0)],
)
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native test engine does not support Windows",
)
def test_vfy_output_controls_preserve_child_pytest_verbosity(
    tmp_path: Path, flag: str, pytest_flags: list[str], verbosity: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_verbosity.py",
        "def test_verbosity(pytestconfig):\n"
        f"    assert pytestconfig.option.verbose == {verbosity}\n",
    )
    result = project.run_vfy(flag, "--isolated", "test", "-j", "1", *pytest_flags)
    result.expect(0)
    assert "1 passed" in result.stdout
    if flag == "--quiet":
        assert "Running the full suite" not in result.stderr
    else:
        assert "vfy: workers: 1" in result.stderr
        assert "Running the full suite" in result.stderr


def test_cache_honors_explicit_configuration_before_cleanup(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".coveragerc", "[run\n")
    project.write("custom.toml", '[tool.vfy.test]\nworkers = "1"\n')
    project.write(".vfy/history.json", "{}")
    result = project.run_vfy("--verbose", "--config", "custom.toml", "cache", "clean")
    result.expect(0)
    assert "Configuration: custom.toml (explicit)" in result.stderr
    assert not (tmp_path / ".vfy").exists()


def test_explicit_toml_loads_vfy_settings_before_cache_mutation(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    config = project.write("custom.toml", '[tool.vfy.test]\nworkers = "0"\n')
    sentinel = project.write(".vfy/history.json", "keep")
    validation = project.run_vfy("--config", str(config), "config", "validate")
    cleanup = project.run_vfy("--config", str(config), "cache", "clean")
    assert validation.returncode == cleanup.returncode == 1
    assert validation.stderr == cleanup.stderr
    assert "workers" in validation.stderr
    assert sentinel.read_text() == "keep"


def test_vfy_settings_expand_environment_and_accept_numeric_workers(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pyproject.toml",
        '[tool.vfy.test]\nworkers = 2\nhistory = "${HISTORY_ROOT?}/history.json"\n'
        'source = ["${SOURCE_ROOT?}"]\n[tool.vfy.mutate]\nworkers = "${WORKERS?}"\n',
    )
    result = project.run_vfy(
        "config", "show", env={"HISTORY_ROOT": "build", "SOURCE_ROOT": "src", "WORKERS": "3"}
    )
    result.expect(0)
    assert json.loads(result.stdout)["settings"]["vfy"] == {
        "test": {"workers": 2, "history": "build/history.json", "source": ["src"]},
        "mutate": {"workers": 3},
    }
    missing = project.run_vfy(
        "config", "validate", env={"HISTORY_ROOT": None, "SOURCE_ROOT": "src", "WORKERS": "3"}
    )
    assert missing.returncode == 1
    assert "HISTORY_ROOT" in missing.stderr
    assert "tool.vfy.test" in missing.stderr
