"""Record stock pytest phase observations for native engine differentials."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _pytest.reports import TestReport


def pytest_runtest_logreport(report: TestReport) -> None:
    """Append each phase without changing pytest's execution or reporting."""
    outcome = report.outcome
    if hasattr(report, "wasxfail"):
        outcome = "xfailed" if report.skipped else "xpassed"
    elif report.failed and report.when != "call":
        outcome = "error"
    stdout = "".join(
        text for name, text in report.sections if name == f"Captured stdout {report.when}"
    )
    stderr = "".join(
        text for name, text in report.sections if name == f"Captured stderr {report.when}"
    )
    record = {
        "nodeid": report.nodeid,
        "phase": report.when,
        "outcome": outcome,
        "stdout": stdout,
        "stderr": stderr,
    }
    with Path(os.environ["PYTEST_STOCK_PHASE_LOG"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
