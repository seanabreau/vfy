"""Upstream warning capture and terminal publication for native runs."""

from __future__ import annotations

import warnings
from collections.abc import Sequence

from _pytest.config import Config
from _pytest.terminal import WarningReport
from _pytest.warnings import warning_record_to_str

from .native_state import NativeRunResult


class _NativeSessionWarningRecorder:
    """Retain upstream warning events outside the Rust execution scopes."""

    def __init__(self, records: list[tuple[str, str, int, str]]) -> None:
        self.records = records

    def pytest_warning_recorded(
        self, warning_message: warnings.WarningMessage, nodeid: str
    ) -> None:
        self.records.append(
            (
                warning_record_to_str(warning_message),
                warning_message.filename,
                warning_message.lineno,
                nodeid,
            )
        )


def _publish_native_warnings(config: Config, result: NativeRunResult) -> None:
    terminal = config.pluginmanager.getplugin("terminalreporter")
    if terminal is None:
        return
    _publish_warning_records(config, result.warning_records())


def _publish_warning_records(config: Config, records: Sequence[tuple[str, str, int, str]]) -> None:
    terminal = config.pluginmanager.getplugin("terminalreporter")
    if terminal is None:
        return
    for message, filename, lineno, nodeid in records:
        terminal._add_stats(
            "warnings",
            [WarningReport(message=message, nodeid=nodeid, fslocation=(filename, lineno))],
        )
