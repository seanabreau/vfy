"""Build the reduced rerun and the summary for failure confirmation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .engine import TestEngine

if TYPE_CHECKING:
    from .execution import TestRunSummary


def _confirmation_pytest_args(
    pytest_args: list[str], temporary: Path, *, route: TestEngine
) -> list[str]:
    """Preserve execution options while removing controls that can skip confirmations."""
    value_options = {
        "-n",
        "--numprocesses",
        "--maxfail",
        "--junitxml",
        "--junit-xml",
        "--html",
    }
    flag_options = {
        "-x",
        "--exitfirst",
        "--lf",
        "--last-failed",
        "--ff",
        "--failed-first",
        "--nf",
        "--new-first",
        "--sw",
        "--stepwise",
        "--sw-skip",
        "--stepwise-skip",
        "--sw-reset",
        "--stepwise-reset",
    }
    result: list[str] = []
    redirect_junit = False
    redirect_html = False
    index = 0
    while index < len(pytest_args):
        argument = pytest_args[index]
        if argument in value_options:
            redirect_junit |= argument in {"--junitxml", "--junit-xml"}
            redirect_html |= argument == "--html"
            index += 2
            continue
        if argument in flag_options:
            index += 1
            continue
        if argument.startswith(("--numprocesses=", "--maxfail=")) or (
            argument.startswith("-n") and argument != "-n"
        ):
            index += 1
            continue
        if argument.startswith(("--junitxml=", "--junit-xml=")):
            redirect_junit = True
            index += 1
            continue
        if argument.startswith("--html="):
            redirect_html = True
            index += 1
            continue
        result.append(argument)
        index += 1
    if route is TestEngine.PYTEST:
        result.extend(("-n", "0"))
    result.extend(
        [
            "--maxfail=0",
            "--no-cov",
            "-o",
            f"cache_dir={temporary / 'pytest-cache'}",
        ]
    )
    if redirect_junit:
        result.append(f"--junitxml={temporary / 'confirmation.xml'}")
    if redirect_html:
        result.append(f"--html={temporary / 'confirmation.html'}")
    return result


def _confirmation_message(primary: TestRunSummary, confirmation: TestRunSummary) -> str:
    if confirmation.infrastructure_failed or any(
        nodeid not in confirmation.outcomes for nodeid in primary.failed
    ):
        return "Failure confirmation could not complete; the original run remains failed"
    remaining = [nodeid for nodeid in primary.failed if confirmation.outcomes[nodeid] == "failed"]
    passed = [nodeid for nodeid in primary.failed if confirmation.outcomes[nodeid] == "passed"]
    if len(passed) == len(primary.failed):
        return (
            f"{len(passed)} failed test{'s' if len(passed) != 1 else ''} passed on confirmation; "
            "the original run remains failed"
        )
    if not passed:
        return (
            f"{len(remaining)} test{'s' if len(remaining) != 1 else ''} remained failed "
            "on confirmation"
        )
    return (
        f"{len(remaining)} test{'s' if len(remaining) != 1 else ''} remained failed; "
        f"{len(passed)} passed on confirmation"
    )
