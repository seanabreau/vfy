"""Record stock pytest phase reports for maxfail differentials."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _pytest.reports import CollectReport, TestReport


def _append(record: dict[str, str]) -> None:
    destination = Path(os.environ["PYTEST_STOCK_MAXFAIL_PHASES"])
    with destination.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")


def pytest_collectreport(report: CollectReport) -> None:
    """Record collection errors because they count toward pytest's maxfail."""
    if report.failed:
        _append({"nodeid": report.nodeid, "phase": "collect", "outcome": "error"})


def pytest_runtest_logreport(report: TestReport) -> None:
    """Record ordered test phases without changing pytest's execution."""
    outcome = report.outcome
    if hasattr(report, "wasxfail"):
        outcome = "xfailed" if report.skipped else "xpassed"
    elif report.failed and report.when != "call":
        outcome = "error"
    _append({"nodeid": report.nodeid, "phase": report.when, "outcome": outcome})
