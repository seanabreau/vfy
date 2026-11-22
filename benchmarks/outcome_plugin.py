"""Record pytest outcomes independently of vfy's result model."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _pytest.reports import TestReport

_OWNER_VARIABLE = "VFY_BENCHMARK_OWNER_PID"
_sessions: list[tuple[object, bool, str | None]] = []


def pytest_configure(config: object) -> None:
    """Bind each session's output before tests may replace ``os.environ``."""

    if hasattr(config, "workerinput"):
        _sessions.append((config, False, None))
        return
    owner = os.environ.get(_OWNER_VARIABLE)
    if owner is None:
        os.environ[_OWNER_VARIABLE] = str(os.getpid())
        authoritative = True
    else:
        authoritative = owner == str(os.getpid())
    _sessions.append((config, authoritative, os.environ.get("VFY_BENCHMARK_OUTCOMES")))


def pytest_unconfigure(config: object) -> None:
    """Restore the enclosing session after a nested pytest invocation."""

    if _sessions and _sessions[-1][0] is config:
        _sessions.pop()


def pytest_runtest_logreport(report: TestReport) -> None:
    """Record every pytest setup, call, and teardown report."""

    if not _sessions:
        return
    _config, authoritative, destination = _sessions[-1]
    if not authoritative:
        return
    if not destination:
        return
    context = getattr(report, "context", None)
    nodeid = report.nodeid
    subtest: dict[str, object] | None = None
    if context is not None:
        # Subtest call reports share the enclosing item's nodeid. Include
        # the subtest message and parameters to distinguish their outcomes.
        values = getattr(context, "kwargs", {})
        subtest = {
            "msg": getattr(context, "msg", None),
            "kwargs": dict(values) if isinstance(values, dict) else repr(values),
        }
        descriptor = json.dumps(subtest, sort_keys=True, default=repr, separators=(",", ":"))
        nodeid = f"{nodeid}::[subtest]{descriptor}"
    record = {
        "nodeid": nodeid,
        "when": report.when,
        "outcome": report.outcome,
        "wasxfail": (
            str(getattr(report, "wasxfail", None)) if getattr(report, "wasxfail", None) else None
        ),
    }
    if subtest is not None:
        record["subtest"] = subtest
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def outcome_plugin_path() -> Path:
    """Return the absolute path to this plug-in file."""

    return Path(__file__).resolve()
