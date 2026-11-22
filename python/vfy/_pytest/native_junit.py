"""JUnit collection reports, options, and attribute hand-off for the Rust serializer."""

from __future__ import annotations

import platform
from datetime import datetime
from typing import TYPE_CHECKING

import pytest
from _pytest import junitxml as _pytest_junitxml
from _pytest.config import Config
from _pytest.reports import CollectReport

from . import parallel
from .native_state import (
    NativeRunResult,
    UnsupportedExtensionError,
    _native_junit_collection_reports_key,
    _native_junit_reporter_key,
)
from .upstream_adaptation import compatibility_policy

if TYPE_CHECKING:
    from _pytest.main import Session


class _NativeJUnitCollectionReporter:
    """Pass collection failures and skips to Rust's JUnit serializer."""

    def __init__(self, reports: list[tuple[str, str, str, str, int | None]]) -> None:
        self.reports = reports

    @pytest.hookimpl
    def pytest_collectreport(self, report: CollectReport) -> None:
        if report.outcome == "passed":
            return
        location = report.location
        file = str(location[0]) if location is not None else report.nodeid
        raw_line = location[1] if location is not None else None
        line = raw_line if isinstance(raw_line, int) else None
        self.reports.append(
            (
                report.nodeid,
                report.outcome,
                str(report.longrepr),
                file,
                line,
            )
        )


def _validate_junit_configuration(config: Config) -> None:
    if not config.option.xmlpath:
        return
    accepted = {
        "junit_family": {"legacy", "xunit1", "xunit2"},
        "junit_logging": {"no", "log", "system-out", "system-err", "out-err", "all"},
        "junit_duration_report": {"total", "call"},
    }
    for name, values in accepted.items():
        value = config.getini(name)
        if value not in values:
            raise UnsupportedExtensionError(f"native pytest does not support {name}={value!r}")


def _disable_junit_reporter(config: Config) -> None:
    """Keep pytest's empty report plugin from replacing the native XML result."""
    reporter = config.stash.get(_pytest_junitxml.xml_key, None)
    if reporter is not None:
        config.stash[_native_junit_reporter_key] = reporter
        del config.stash[_pytest_junitxml.xml_key]
        config.pluginmanager.unregister(reporter)


def _junit_retains_passing_output(config: Config) -> bool:
    return bool(
        config.option.xmlpath
        and config.getini("junit_logging") != "no"
        and config.getini("junit_log_passing_tests")
    )


def _junit_global_properties(config: Config) -> list[tuple[str, str]]:
    reporter = config.stash.get(_native_junit_reporter_key, None)
    if reporter is None:
        reporter = config.stash.get(_pytest_junitxml.xml_key, None)
    return [(str(name), str(value)) for name, value in getattr(reporter, "global_properties", ())]


def _junit_options(session: Session, result: NativeRunResult) -> dict[str, object]:
    config = session.config
    family = config.getini("junit_family")
    required = result.required_junit_locations(family)
    locations: dict[str, tuple[str, int | None]] = {}
    if parallel.is_worker_collection_controller(config) or (
        required is None and parallel.is_collected_fork_controller(config, result)
    ):
        locations = result.junit_locations(family)
    elif required is None:
        for item in session.items:
            file, line, _domain = item.location
            locations[item.nodeid] = (str(file), line)
    elif required:
        requested = set(required)
        for item in session.items:
            if item.nodeid in requested:
                file, line, _domain = item.location
                locations[item.nodeid] = (str(file), line)
    collection_reports = config.stash.get(_native_junit_collection_reports_key, [])
    return {
        "suite_name": config.getini("junit_suite_name"),
        "prefix": config.option.junitprefix,
        "family": family,
        "logging": config.getini("junit_logging"),
        "report_duration": config.getini("junit_duration_report"),
        "junit_case_count": compatibility_policy(config).junit_case_count,
        "junit_message_style": compatibility_policy(config).junit_message_style,
        "log_passing_tests": config.getini("junit_log_passing_tests"),
        "timestamp": datetime.now().astimezone().isoformat(),
        "hostname": platform.node(),
        "locations": locations,
        "global_properties": _junit_global_properties(config),
        "collection_reports": collection_reports,
    }


def case_xml_attributes(item: pytest.Item) -> list[tuple[str, str]]:
    """Consume attributes recorded by pytest's JUnit fixture for this case."""
    config = item.config
    reporter = config.stash.get(_native_junit_reporter_key, None)
    if reporter is None:
        reporter = config.stash.get(_pytest_junitxml.xml_key, None)
    if not isinstance(reporter, _pytest_junitxml.LogXML):
        return []
    identity = getattr(item, "id", None)
    node = reporter.node_reporters.get((item.nodeid if identity is None else identity, None))
    if node is None:
        return []
    attributes = list(node.attrs.items())
    node.attrs.clear()
    return attributes


def _retains_native_junit_locations(config: Config) -> bool:
    return bool(
        config.option.xmlpath
        and (
            config.getini("junit_family") != "xunit2"
            or (parallel.is_worker() and parallel.worker_collection_enabled(config))
        )
    )
