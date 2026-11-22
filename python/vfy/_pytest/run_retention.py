"""Retention of control-exception state for upstream session finish."""

from __future__ import annotations

from _pytest.config import Config

from .execution_record import _execution_record_identity, _publish_or_retain_execution_record
from .native_state import NativeRunResult, _interrupted_result_key, _session_cleanup_error_key
from .native_warnings import _publish_native_warnings
from .terminal_capture import _publish_terminal_capture_reports


def retain_session_cleanup_error(config: Config, error: BaseException) -> None:
    """Keep the original fixture exception for upstream sessionfinish dispatch."""
    config.stash[_session_cleanup_error_key] = error


def retain_interrupted_result(config: Config, result: NativeRunResult) -> None:
    """Retain completed phases and warnings after a control exception."""
    config.stash[_interrupted_result_key] = result
    terminal = config.pluginmanager.getplugin("terminalreporter")
    if terminal is not None:
        _publish_terminal_capture_reports(config, result, terminal)
    _publish_native_warnings(config, result)
    _publish_or_retain_execution_record(config, result, _execution_record_identity(config))
