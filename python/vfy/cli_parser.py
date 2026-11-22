"""Argument parser construction and command-line classification for vfy."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, TypedDict

from ._cli_arguments import escape_arguments, restore_arguments
from ._version import __version__
from .errors import VfyError
from .native import (
    NativeTestInvocation,
    is_test_cli,
    rewrite_serial_xdist,
    test_cli_schema,
    validate_vfy_options_json,
)

if TYPE_CHECKING:
    from argparse import _SubParsersAction


class _ArgumentOptions(TypedDict, total=False):
    action: str
    choices: list[str] | None
    const: object
    default: object
    dest: str
    help: str
    metavar: str
    nargs: str
    type: Callable[[str], str | int]


REPORT_COMMANDS = (
    "report",
    "xml",
    "json",
    "lcov",
    "html",
    "annotate",
)


def _workers_value(value: str) -> str | int:
    try:
        validate_vfy_options_json("test", json.dumps({"workers": value}))
    except ValueError:
        raise argparse.ArgumentTypeError(
            "workers must be auto, logical, or an integer from 1 to 256"
        ) from None
    return value if value in {"auto", "logical"} else int(value)


def _test_workers_value(value: str) -> str | int:
    # pytest-xdist's `-n 0` keeps the run in the controller process.
    return 1 if value == "0" else _workers_value(value)


def _data_file(
    parser: argparse.ArgumentParser,
    rcfile_help: str | None = "read this Coverage.py configuration file instead of discovering one",
) -> None:
    parser.add_argument(
        "--data-file",
        metavar="PATH",
        help="read and write coverage data in this file (default: .coverage, or $COVERAGE_FILE)",
    )
    if rcfile_help is None:
        return
    parser.add_argument(
        "--rcfile",
        metavar="PATH",
        help=rcfile_help,
    )


def _report_options(
    parser: argparse.ArgumentParser, *, fail_under: bool = True, precision: bool = True
) -> None:
    _data_file(parser)
    parser.add_argument(
        "--include",
        action="append",
        metavar="GLOB",
        help="report only files matching this pattern; repeatable",
    )
    parser.add_argument(
        "--omit",
        action="append",
        metavar="GLOB",
        help="leave out files matching this pattern; repeatable",
    )
    parser.add_argument(
        "--contexts",
        action="append",
        metavar="REGEX",
        help="report only measurements whose context matches; repeatable",
    )
    # Accept --color after the subcommand without replacing a global value
    # when the local option is absent. Show only the global option in help.
    parser.add_argument(
        "--color",
        nargs="?",
        const="always",
        choices=("auto", "always", "never"),
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-color",
        dest="color",
        action="store_const",
        const="never",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    if precision:
        parser.add_argument(
            "--precision",
            type=int,
            metavar="N",
            help="show N decimal places in percentages (default: 0)",
        )
    if fail_under:
        parser.add_argument(
            "--fail-under",
            type=float,
            metavar="PERCENT",
            help="exit with status 2 when the total is below this percentage",
        )
    parser.add_argument(
        "--ignore-errors",
        action="store_true",
        default=None,
        help="skip files whose source is missing or cannot be parsed",
    )


def _semantic_options(parser: argparse.ArgumentParser) -> None:
    _data_file(parser, rcfile_help=None)
    parser.add_argument(
        "--include",
        action="append",
        metavar="GLOB",
        help="report only decision definitions in matching files; repeatable",
    )
    parser.add_argument(
        "--omit",
        action="append",
        metavar="GLOB",
        help="omit decision definitions in matching files; repeatable",
    )
    parser.add_argument(
        "--contexts",
        action="append",
        metavar="REGEX",
        help="use observations whose context matches; repeatable",
    )
    parser.add_argument(
        "--source",
        action="append",
        metavar="PATH",
        help="add Python files from this path, including unexecuted decisions; repeatable",
    )
    parser.add_argument(
        "--exclude-decisions",
        action="append",
        metavar="REGEX",
        help="exclude expressions or decision kinds matching this regex; repeatable",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default=None,
        help="write the human-readable report or JSON (default: text)",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="write the report here; use - or omit it for stdout",
    )
    parser.add_argument(
        "--pretty-print",
        action="store_true",
        help="indent JSON output (no effect on text output)",
    )
    parser.add_argument(
        "--history",
        default=None,
        metavar="PATH",
        help="read test durations from this file to estimate the fastest relevant test",
    )
    for name, label in (
        ("decisions", "decision"),
        ("conditions", "condition"),
        ("mcdc", "masking MC/DC"),
    ):
        parser.add_argument(
            f"--fail-under-{name}",
            type=float,
            default=None,
            metavar="PERCENT",
            help=f"exit 2 when {label} coverage is below this percentage",
        )


def _base_parser() -> tuple[argparse.ArgumentParser, _SubParsersAction[argparse.ArgumentParser]]:
    parser = argparse.ArgumentParser(
        prog="vfy",
        description="A Python test runner and coverage tool, built in Rust.",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"vfy {__version__}",
        help="show the version",
    )
    parser.add_argument(
        "--config",
        dest="config_file",
        metavar="PATH",
        help="use this vfy configuration file",
    )
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="ignore configuration files and environment-selected config",
    )
    parser.add_argument(
        "--color",
        nargs="?",
        const="always",
        choices=("auto", "always", "never"),
        default=None,
        help="control colored output (default: auto)",
    )
    parser.add_argument(
        "--no-color",
        dest="color",
        action="store_const",
        const="never",
        help=argparse.SUPPRESS,
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v", "--verbose", action="count", default=0, help="show more diagnostics"
    )
    verbosity.add_argument("-q", "--quiet", action="store_true", help="show only requested output")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    return parser, sub


def _add_test_parser(sub: _SubParsersAction[argparse.ArgumentParser]) -> None:
    test = sub.add_parser(
        "test",
        help="run tests with the independent engine and optional coverage",
        epilog=(
            "coverage options (no -- separator):\n"
            "  --cov=SOURCE              measure a path, package, or module; repeatable\n"
            "  --cov-branch              measure branch coverage\n"
            "  --cov-report=TYPE[:DEST]  choose a report format and optional destination\n"
            "  --no-cov                  disable coverage collection and reporting\n"
            "\n"
            "example:\n"
            "  vfy test --cov=src --cov-branch --cov-report=term-missing\n"
            "\n"
            "optional pytest integration help (requires vfy[pytest]):\n"
            "  python -m pytest -p no:pytest_cov -p vfy.pytest_plugin --help\n"
            "vfy supports a subset of pytest options; see\n"
            "https://neocline-vfy-docs.pages.dev/about/compatibility/"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_test_options(test)
    test.add_argument(
        "target",
        nargs="*",
        metavar="TARGET",
        help="run only the tests at these files, directories, node IDs, or PATH:LINE locations",
    )
    test.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        metavar="PYTEST_ARGS",
        help="pass these options to pytest, including --cov options",
    )


def _add_test_options(test: argparse.ArgumentParser) -> None:
    test.set_defaults(
        mode="full",
        no_replay=False,
        fresh=False,
        baseline=None,
        export_baseline=None,
        plan_only=False,
        plan_output=None,
        audit_selection=False,
        confirm_failures=False,
    )
    for spec in test_cli_schema():
        names = spec["names"]
        kind = spec["kind"]
        options: _ArgumentOptions = {"dest": spec["dest"]}
        if spec["metavar"] is not None:
            options["metavar"] = spec["metavar"]
        if spec["help"]:
            options["help"] = spec["help"]
        else:
            options["help"] = argparse.SUPPRESS
        if kind == "flag":
            options["action"] = "store_true"
            if names == ["--no-color"]:
                options.update(action="store_const", const="never", default=argparse.SUPPRESS)
        elif kind == "color":
            options.update(
                nargs="?", const="always", choices=spec["choices"], default=argparse.SUPPRESS
            )
        elif kind == "choice":
            options["choices"] = spec["choices"]
            if names == ["--engine"]:
                options["default"] = "native"
        elif kind == "workers":
            options["type"] = _test_workers_value
        elif kind == "append":
            options["action"] = "append"
        test.add_argument(*names, **options)


def _build_test_parser() -> argparse.ArgumentParser:
    parser, sub = _base_parser()
    _add_test_parser(sub)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser, sub = _base_parser()
    _add_test_parser(sub)

    explain = sub.add_parser(
        "explain",
        help="explain which tests reach a source location",
        description=(
            "Show test reachability, selection reasons, and semantic evidence for a location."
        ),
    )
    _data_file(explain, rcfile_help=None)
    explain.add_argument(
        "--history",
        default=None,
        metavar="PATH",
        help=(
            "read recorded test durations from this file "
            "(default: configured history, else .vfy/test-history.sqlite3)"
        ),
    )
    explain.add_argument("--json", action="store_true", help="write a machine-readable explanation")
    explain.add_argument(
        "location", metavar="PATH:LINE", help="source file and one-based line number to explain"
    )

    coverage = sub.add_parser("coverage", help="collect, report, or check coverage")
    coverage_sub = coverage.add_subparsers(
        dest="coverage_command", required=True, metavar="COMMAND"
    )

    report = coverage_sub.add_parser("report", help="write a terminal coverage report")
    _report_options(report)
    report.add_argument(
        "-m", "--show-missing", action="store_true", default=None, help="list missing line numbers"
    )
    report.add_argument(
        "--skip-covered",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="leave out files that are fully covered",
    )
    report.add_argument(
        "--skip-empty",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="leave out files that contain no statements",
    )
    report.add_argument(
        "--sort",
        metavar="COLUMN",
        help=(
            "sort by name, stmts, miss, branch, brpart, or cover (default: name)."
            " Use --sort=-cover for descending coverage"
        ),
    )
    report.add_argument(
        "--format",
        choices=("text", "markdown", "total"),
        help="write text, Markdown, or only the total percentage (default: text)",
    )

    xml = coverage_sub.add_parser("xml", help="write a Cobertura XML report")
    _report_options(xml)
    xml.add_argument("-o", "--output", metavar="PATH", help="write the report here or use -")

    json_parser = coverage_sub.add_parser("json", help="write a Coverage.py-shaped JSON report")
    _report_options(json_parser)
    json_parser.add_argument(
        "-o", "--output", metavar="PATH", help="write the report here or use -"
    )
    json_parser.add_argument(
        "--pretty-print", action="store_true", default=None, help="indent JSON"
    )
    json_parser.add_argument(
        "--show-contexts", action="store_true", default=None, help="include executing contexts"
    )

    lcov = coverage_sub.add_parser("lcov", help="write an LCOV report")
    _report_options(lcov)
    lcov.add_argument("-o", "--output", metavar="PATH", help="write the report here or use -")

    html = coverage_sub.add_parser("html", help="write an HTML coverage report")
    _report_options(html)
    html.add_argument("-d", "--directory", metavar="DIR", help="write the HTML site here")
    html.add_argument("--title", metavar="TITLE", help="title of the generated site")
    html.add_argument(
        "--skip-covered",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="leave out files that are fully covered",
    )
    html.add_argument(
        "--skip-empty",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="leave out files that contain no statements",
    )

    annotate = coverage_sub.add_parser("annotate", help="annotate source files with coverage")
    _report_options(annotate, fail_under=False)
    annotate.add_argument("-d", "--directory", metavar="DIR", help="write annotated copies here")

    run = coverage_sub.add_parser("run", help="run a Python program and collect coverage")
    run.add_argument("-a", "--append", action="store_true", help="append to existing data")
    run.add_argument("--branch", action="store_true", default=None, help="measure branch coverage")
    run.add_argument(
        "--decisions",
        action="store_true",
        default=None,
        help="measure semantic decision, condition, and MC/DC coverage",
    )
    run.add_argument(
        "-p",
        "--parallel-mode",
        action="store_true",
        default=None,
        help="write data to a uniquely named file for coverage combine",
    )
    run.add_argument("-m", "--module", action="store_true", help="run TARGET as a module")
    run.add_argument(
        "--source",
        action="append",
        metavar="PATH",
        help="measure files under this path; repeatable",
    )
    run.add_argument(
        "--include",
        action="append",
        metavar="GLOB",
        help="measure only files matching this pattern; repeatable",
    )
    run.add_argument(
        "--omit",
        action="append",
        metavar="GLOB",
        help="leave out files matching this pattern; repeatable",
    )
    run.add_argument("--context", metavar="NAME", help="label measurements with this context")
    _data_file(run)
    run.add_argument("target", metavar="TARGET", help="Python script to run, or module with -m")
    run.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        metavar="ARGUMENTS",
        help="pass these arguments to the program",
    )

    combine = coverage_sub.add_parser("combine", help="combine parallel coverage data files")
    combine.add_argument("paths", nargs="*", metavar="PATH", help="files or shard directories")
    _data_file(combine)
    combine.add_argument("--keep", action="store_true", help="keep input shards")
    combine.add_argument("--strict", action="store_true", help="fail when no shards are found")

    erase = coverage_sub.add_parser("erase", help="erase coverage data")
    _data_file(erase)
    erase.add_argument("--parallel", action="store_true", help="also delete parallel shards")

    debug = coverage_sub.add_parser("debug", help="show coverage diagnostics")
    debug.add_argument("topic", choices=["data"], help="choose the diagnostic topic")
    _data_file(debug)

    check = coverage_sub.add_parser(
        "check",
        help="check decision and MC/DC evidence",
        description=(
            "Check decision, condition, and masking MC/DC evidence from a decision-enabled run."
        ),
        epilog=(
            "examples:\n"
            "  vfy test --all --cov=src --cov-decisions\n"
            "  vfy coverage check --format json --output build/decisions.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    check.add_argument(
        "--strict",
        action="store_true",
        help="treat omitted decision, condition, and MC/DC thresholds as 100%%",
    )
    _semantic_options(check)

    mutate = sub.add_parser(
        "mutate",
        help="measure whether tests detect plausible faults",
        description="Apply mutations and report the tests that detect them.",
    )
    mutate.add_argument(
        "--engine",
        choices=("native", "pytest-native"),
        default="native",
        help="test execution engine (default: native; pytest-native requires installed pytest)",
    )
    _data_file(
        mutate,
        "pass this configuration file to pytest as -c and read it for Coverage.py settings; "
        "vfy settings come from --config",
    )
    scope = mutate.add_mutually_exclusive_group()
    scope.add_argument(
        "--changed", action="store_true", help="mutate changed Python files (default)"
    )
    scope.add_argument(
        "--all", action="store_true", help="mutate every measured/source Python file"
    )
    mutate.add_argument(
        "--base", metavar="REF", help="mutate Python files changed since the merge base with REF"
    )
    mutate.add_argument(
        "--source",
        action="append",
        metavar="PATH",
        help="mutate Python files under this root; repeatable",
    )
    mutate.add_argument(
        "--history",
        default=None,
        metavar="PATH",
        help=(
            "use this test history file for recorded durations "
            "(default: configured history, else .vfy/test-history.sqlite3)"
        ),
    )
    mutate.add_argument(
        "--operator",
        action="append",
        metavar="NAME",
        help="run only the named mutation operator; repeatable",
    )
    mutate.add_argument(
        "--max-mutations",
        type=int,
        default=0,
        metavar="N",
        help="run at most N mutations (default: 0, no limit)",
    )
    mutate.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="limit the seconds one mutation may run",
    )
    mutate.add_argument(
        "--timeout-policy",
        choices=("adaptive", "fixed"),
        default=None,
        help="choose the mutation timeout policy (default: adaptive)",
    )
    mutate.add_argument(
        "-j",
        "--workers",
        type=_workers_value,
        default=None,
        metavar="N|auto|logical",
        help="set the number of test workers (default: 1; auto with --engine=pytest-native)",
    )
    mutate.add_argument("--no-cache", action="store_true", help="skip the mutation cache")
    mutate.add_argument(
        "--test-isolation",
        choices=("serial", "file"),
        default=None,
        help=(
            "isolate tests per file or run serially "
            "(default: serial; native uses file on single-threaded POSIX)"
        ),
    )
    mutate.add_argument(
        "--fail-on-survivor", action="store_true", help="exit 2 on a surviving mutation"
    )
    mutate.add_argument(
        "--format", choices=("text", "json"), default="text", help="choose the output format"
    )
    mutate.add_argument("-o", "--output", metavar="PATH", help="write the report here or use -")
    mutate.add_argument("--pretty-print", action="store_true", help="indent JSON output")
    mutate.add_argument(
        "target",
        nargs="?",
        metavar="PATH:LINE",
        help="mutate only the code at this source location",
    )
    mutate.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        metavar="PYTEST_ARGS",
        help="pass these arguments to pytest to choose and configure the tests",
    )

    config = sub.add_parser("config", help="inspect vfy configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True, metavar="COMMAND")
    config_sub.add_parser("show", help="show resolved settings and their source")
    config_sub.add_parser("validate", help="validate configuration without running tests")

    diagnostics = sub.add_parser("diagnostics", help="show runtime and test backend diagnostics")
    diagnostics.add_argument(
        "--json", action="store_true", help="write machine-readable diagnostics"
    )
    diagnostics.add_argument(
        "--engine",
        choices=("native", "pytest-native"),
        default="native",
        help="choose the test engine to inspect (default: native)",
    )
    diagnostics.add_argument(
        "--collect",
        action="store_true",
        help="run collection with the selected engine without executing test bodies",
    )
    diagnostics.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        metavar="PYTEST_ARGS",
        help="optional pytest arguments after --, for example: diagnostics -- -n 4",
    )

    cache = sub.add_parser("cache", help="inspect or clean vfy caches")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True, metavar="COMMAND")
    cache_sub.add_parser("dir", help="show the project cache directory")
    cache_sub.add_parser("clean", help="remove only project-local vfy cache files")
    return parser


def _check_command_spelling(raw: list[str]) -> None:
    command_hints = {
        command: f"vfy coverage {command}"
        for command in (*REPORT_COMMANDS, "combine", "erase", "debug", "run")
    }
    command_hints["check"] = "vfy coverage check"
    # Only inspect the first command position. Looking through every token
    # would mistake values such as ``--format json`` for a command.
    index = 0
    while index < len(raw):
        token = raw[index]
        if token == "--":
            break
        if token == "--config":
            index += 2
            continue
        if token == "--color":
            if index + 1 < len(raw) and raw[index + 1] in {"auto", "always", "never"}:
                index += 2
            else:
                index += 1
            continue
        if token.startswith(("--config=", "--color=", "-")) or token in {
            "--isolated",
            "--quiet",
            "-q",
        }:
            index += 1
            continue
        if token in command_hints:
            raise VfyError(f"unknown command {token!r}; use {command_hints[token]}")
        break


def _rewrite_serial_xdist_arguments(raw: list[str]) -> list[str] | None:
    """Map pytest-xdist's `-n 0` to vfy's one-worker, serial-xdist form."""
    try:
        return rewrite_serial_xdist(raw)
    except UnicodeEncodeError:
        escaped, marker = escape_arguments(raw)
        result = rewrite_serial_xdist(escaped)
        return None if result is None else restore_arguments(result, marker)


def _is_test_command(raw: list[str]) -> bool:
    try:
        return is_test_cli(raw)
    except UnicodeEncodeError:
        escaped, _ = escape_arguments(raw)
        return is_test_cli(escaped)


def _parse_mutation_arguments(raw: list[str], command_index: int) -> argparse.Namespace:
    if "--" in raw[command_index + 1 :]:
        raise VfyError("vfy mutate accepts pytest options directly; remove the `--` separator")
    # The first option vfy does not recognize starts the forwarded pytest tail.
    parser = build_parser()
    _, unknown = parser.parse_known_args(raw)
    unknown_options = set(unknown)
    implicit = next(
        (
            position
            for position in range(command_index + 1, len(raw))
            if raw[position] in unknown_options
        ),
        None,
    )
    args = parser.parse_args(raw if implicit is None else raw[:implicit])
    if implicit is not None:
        args.pytest_args = raw[implicit:]
    args.target, args.pytest_args = NativeTestInvocation.mutation_cli_arguments(
        args.target, args.pytest_args
    )
    return args


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse vfy arguments and suggest valid commands for unsupported forms."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if _is_test_command(raw):
        from .run.entry import parse  # noqa: PLC0415 - run.entry imports this module

        return parse(raw)
    _check_command_spelling(raw)
    command_index = None
    index = 0
    while index < len(raw):
        token = raw[index]
        if token in {"--config", "--color"}:
            if token == "--config" or (
                index + 1 < len(raw) and raw[index + 1] in {"auto", "always", "never"}
            ):
                index += 2
            else:
                index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        command_index = index
        break
    if command_index is not None and raw[command_index] == "mutate":
        args = _parse_mutation_arguments(raw, command_index)
    else:
        args = build_parser().parse_args(raw)
    if args.command == "test" and args.engine == "native":
        rewritten = _rewrite_serial_xdist_arguments(raw)
        if rewritten is not None:
            return _parse_args(rewritten)
    if args.color is None:
        args.color = "auto"
    if args.command == "coverage":
        args.command = args.coverage_command
    return args
