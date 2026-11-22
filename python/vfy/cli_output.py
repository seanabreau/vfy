"""Output-destination and message helpers shared by vfy commands."""

from __future__ import annotations

import argparse
import os
import sys

from .output import OutputPolicy


def _writes_stdout(destination: str | None) -> bool:
    return not destination or destination == "-"


def _report_color(choice: str | bool | None) -> bool:
    """Resolve the global color policy and standard NO_COLOR/FORCE_COLOR conventions."""
    if choice is True or choice == "always":
        return True
    if choice is False or choice == "never":
        return False
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("FORCE_COLOR"):
        return True
    return bool(sys.stdout.isatty())


def _message(args: argparse.Namespace, text: str) -> None:
    """Write a confirmation where it cannot corrupt a report written to stdout."""
    OutputPolicy.from_args(args).notice(text)
