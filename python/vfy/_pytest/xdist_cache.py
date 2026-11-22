"""Connect upstream xdist reports to Rust's cache session."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import pytest
from _pytest.reports import TestReport

from vfy.native import _pytest

from .xdist_support import is_xdist_controller, is_xdist_worker

if TYPE_CHECKING:
    from _pytest.main import Session

    from .native_pytest import _NativeCache


class XdistCacheReporter:
    """Persist pytest reports once, leaving workers' cache fixtures enabled."""

    def __init__(self, cache: _NativeCache) -> None:
        self.cache = cache
        self._report_owners: list[tuple[str, str, str]] = []
        if getattr(cache._config.option, "stepwise", False):
            raise pytest.UsageError(
                "native pytest does not implement stepwise execution with xdist; "
                "remove -n and the xdist plugin, or run this workflow with pytest"
            )

    @pytest.hookimpl
    def pytest_runtest_logreport(self, report: TestReport) -> None:
        if not is_xdist_worker(self.cache._config):
            # Xdist's crash report uses "???" for its phase. Rust owns the
            # failed outcome; adapt this protocol-only phase to a call report.
            phase = "call" if report.when == "???" else report.when or "setup"
            nodeid = report.nodeid
            outcome = report.outcome
            if type(nodeid) is not str or type(phase) is not str or type(outcome) is not str:
                # Preserve unusual report fields until the plugin releases its session.
                self._report_owners.append((nodeid, phase, outcome))
            self.cache._session._append_xdist_report(nodeid, phase, outcome)

    def finish_outcomes(self) -> None:
        """Finish Rust's selected session from the reports pytest actually received."""
        timestamp = datetime.now().isoformat()  # noqa: DTZ005 - pytest stores local time.
        self.cache._session._finish_xdist_reports(timestamp)

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session: Session) -> None:
        if is_xdist_controller(session.config):
            # Upstream snapshots its session cache before workers can write.
            # Its controller retains prior IDs because workers own collection.
            self.cache._session.select_items([], [], [])

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: Session) -> None:
        if not is_xdist_controller(session.config):
            return
        try:
            if session.config.option.collectonly:
                self.cache.finish_collection()
                return
            self.cache.initialize()
            self.finish_outcomes()
        except _pytest.CachePathError as exc:
            self.cache.warn(f"could not create cache path {self.cache._cachedir}: {exc}")
        except _pytest.CacheWriteError as exc:
            self.cache.warn(f"cache could not write native session state: {exc}")
