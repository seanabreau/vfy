"""Stock-versus-independent runs that compare public pytest output."""

from __future__ import annotations

import re

from .vfy_project import CommandResult, VfyProject

_STOCK = ("-q", "-p", "no:cacheprovider")


def normalized_output(result: CommandResult) -> list[str]:
    """Return stdout lines without the elapsed-time suffix."""

    return [re.sub(r" in [0-9.]+s$", "", line) for line in result.stdout.splitlines()]


def run_pair(project: VfyProject, *arguments: str) -> tuple[CommandResult, CommandResult]:
    """Run stock pytest and the independent engine quietly; require equal exit statuses."""

    stock = project.run_engine("stock", *arguments, stock=_STOCK)
    independent = project.run_engine("native", "-q", *arguments)
    assert independent.returncode == stock.returncode, independent.stdout + independent.stderr
    return stock, independent


def run_matching(project: VfyProject, *arguments: str) -> CommandResult:
    """Require equal exit statuses and time-normalized stdout; return the independent result."""

    stock, independent = run_pair(project, *arguments)
    assert normalized_output(independent) == normalized_output(stock)
    return independent
