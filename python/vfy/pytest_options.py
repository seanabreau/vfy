"""Lightweight parsing for pytest-cov-compatible report options.

Mutation bootstrap imports this module before pytest loads its option hook.
PYTEST_DONT_REWRITE prevents an already-imported warning in strict test suites.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Protocol, override

if TYPE_CHECKING:
    import pytest

FILE_REPORTS = {"annotate", "html", "xml", "json", "markdown", "markdown-append", "lcov"}
TERM_REPORTS = {"term", "term-missing"}
REPORTS = TERM_REPORTS | FILE_REPORTS


def coverage_config_argument(path: str) -> bool | str:
    """Discover project configuration when pytest-cov's default file is absent."""
    return True if path == ".coveragerc" and not Path(path).is_file() else path


def validate_report(value: str) -> tuple[str, str | None]:
    """Parse pytest-cov's ``TYPE[:DEST]`` report syntax."""
    report, separator, destination = value.partition(":")
    if report not in REPORTS | {""}:
        choices = ", ".join(sorted(REPORTS))
        raise argparse.ArgumentTypeError(f"invalid choice: {report!r} (choose from {choices})")
    if not separator:
        return report, None
    if report in TERM_REPORTS and destination == "skip-covered":
        return report, destination
    if report not in FILE_REPORTS:
        raise argparse.ArgumentTypeError(f"output specifier not supported for: {value!r}")
    return report, destination


class CoverageOptionGroup(Protocol):
    """Accept the argparse actions shared by the pytest and independent adapters."""

    def addoption(self, *names: str, **attrs: Any) -> None:  # noqa: ANN401 - argparse action registration accepts arbitrary option metadata.
        ...


class CoverageOptionParser(Protocol):
    """Expose option-group registration without importing an external test runner."""

    def getgroup(
        self, name: str, description: str = "", after: str | None = None
    ) -> CoverageOptionGroup: ...


def validate_decision_report(value: str) -> str:
    """Parse the semantic report selector before pytest starts collecting."""
    report, separator, destination = value.partition(":")
    if report not in {"term", "json"}:
        raise argparse.ArgumentTypeError(
            f"invalid semantic report type: {report!r} (choose from json, term)"
        )
    if report == "term" and separator:
        raise argparse.ArgumentTypeError("the terminal semantic report cannot have a destination")
    if report == "json" and separator and not destination:
        raise argparse.ArgumentTypeError("the JSON semantic report destination cannot be empty")
    return value


def validate_fail_under(value: str) -> float:
    try:
        threshold = int(value)
    except ValueError:
        try:
            threshold = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError("an integer or float value is required") from None
    if threshold > 100:
        raise argparse.ArgumentTypeError("coverage threshold cannot exceed 100")
    return threshold


def validate_context(value: str) -> str:
    if value != "test":
        raise argparse.ArgumentTypeError('the only supported value is "test"')
    return value


class StoreReport(argparse.Action):
    """Accumulate one destination per report type, as pytest-cov does."""

    @override
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        # argparse passes the tuple produced by validate_report to this action.
        assert isinstance(values, tuple)
        assert len(values) == 2
        report, destination = values
        reports = namespace.cov_report
        if report in {"markdown", "markdown-append"} and destination is None:
            destination = "coverage.md"
        reports[report] = destination
        if {"markdown", "markdown-append"} <= reports.keys() and (
            reports["markdown"] == reports["markdown-append"]
        ):
            parser.error(
                "markdown and markdown-append reports cannot use the same destination; "
                "set one with --cov-report=TYPE:DEST"
            )


def register_coverage_options(parser: CoverageOptionParser) -> None:
    """Register the pytest-cov command-line contract."""
    group = parser.getgroup("cov", "coverage reporting with distributed testing support")
    group.addoption(
        "--cov",
        action="append",
        default=[],
        metavar="SOURCE",
        nargs="?",
        const=True,
        dest="cov_source",
        help=(
            "Path or package name to measure during execution (multi-allowed). "
            "Use --cov= to disable source filtering."
        ),
    )
    group.addoption(
        "--cov-reset",
        action="store_const",
        const=[],
        dest="cov_source",
        help="Reset coverage sources accumulated by earlier options.",
    )
    group.addoption(
        "--cov-report",
        action=StoreReport,
        default={},
        metavar="TYPE",
        type=validate_report,
        help=(
            "Report type: term, term-missing, annotate, html, xml, json, markdown, "
            "markdown-append, or lcov (multi-allowed)."
        ),
    )
    group.addoption(
        "--cov-config",
        action="store",
        default=".coveragerc",
        metavar="PATH",
        help="Coverage configuration file. Default: .coveragerc",
    )
    group.addoption(
        "--no-cov-on-fail",
        action="store_true",
        default=False,
        help="Do not generate coverage reports after a failed test run.",
    )
    group.addoption(
        "--no-cov",
        action="store_true",
        default=False,
        help="Disable coverage collection and reporting.",
    )
    group.addoption(
        "--cov-fail-under",
        action="store",
        metavar="MIN",
        type=validate_fail_under,
        help="Fail when total coverage is less than MIN.",
    )
    group.addoption(
        "--cov-append",
        action="store_true",
        default=False,
        help="Append to existing coverage data instead of erasing it.",
    )
    group.addoption(
        "--cov-branch",
        action="store_true",
        default=None,
        help="Enable branch coverage.",
    )
    group.addoption(
        "--cov-decisions",
        action="store_true",
        default=False,
        help="Enable semantic decision, condition, and MC/DC coverage (implies --cov-branch).",
    )
    group.addoption(
        "--cov-decision-report",
        action="store",
        default=None,
        metavar="TYPE[:DEST]",
        type=validate_decision_report,
        help=(
            "Semantic report: term or json[:PATH] (default: term; "
            "JSON defaults to vfy-decisions.json)."
        ),
    )
    for option, dest, label in (
        ("--cov-fail-under-decisions", "cov_fail_under_decisions", "decision"),
        ("--cov-fail-under-conditions", "cov_fail_under_conditions", "condition"),
        ("--cov-fail-under-mcdc", "cov_fail_under_mcdc", "MC/DC"),
    ):
        group.addoption(
            option,
            action="store",
            default=None,
            dest=dest,
            type=validate_fail_under,
            metavar="MIN",
            help=f"Fail when semantic {label} coverage is less than MIN.",
        )
    group.addoption(
        "--cov-precision",
        type=int,
        default=None,
        help="Override reporting precision.",
    )
    group.addoption(
        "--cov-context",
        action="store",
        metavar="CONTEXT",
        type=validate_context,
        help='Dynamic contexts to use. The supported value is "test".',
    )


def pytest_addoption(parser: CoverageOptionParser) -> None:
    """Parse coverage options for execution-only mutation replay."""
    register_coverage_options(parser)


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    """Retain coverage fixtures when replay loads only the option declarations."""
    if pluginmanager.hasplugin("vfy.pytest_plugin"):
        return
    import pytest  # noqa: PLC0415 - pytest owns fixture import rewriting.

    pytest.register_assert_rewrite("vfy.pytest_plugin")
    from .pytest_plugin import (  # noqa: PLC0415 - only installed-pytest replay needs its fixture declarations.
        cov as cov_fixture,
        no_cover as no_cover_fixture,
    )

    fixtures = ModuleType("vfy._coverage_replay_fixtures")
    fixtures.__dict__.update(cov=cov_fixture, no_cover=no_cover_fixture)
    pluginmanager.register(fixtures, "_vfy_coverage_replay_fixtures")
