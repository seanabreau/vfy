"""Pytest plug-in that records complete public runtest observations.

This module is loaded only by :mod:`pytest_execution_record`. It registers one
stateful observer for the outer pytest session. Nested in-process sessions can
load this module without contaminating the outer record.
"""

from __future__ import annotations

import hashlib
import json
import linecache
import os
import re
import secrets
import sys
import tempfile
import warnings
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from scripts.pytest_execution_observations import normalize_text
else:
    from pytest_execution_observations import normalize_text

if TYPE_CHECKING:
    from typing import Protocol

    from _pytest.config import Config
    from _pytest.main import Session
    from _pytest.reports import TestReport

    class _NativeExecutionResult(Protocol):
        def warning_records(self) -> list[tuple[str, str, int, str]]: ...

        def publish_execution_record(
            self,
            path: str,
            warnings: list[dict[str, object]],
        ) -> None: ...


_state = {"registered": False}


def _normalize(
    value: object,
    roots: tuple[str, str, str],
    pytest_temporary: str | None = None,
) -> str:
    return normalize_text(
        value,
        Path(roots[0]),
        roots[1],
        roots[2],
        pytest_temporary=pytest_temporary,
    )


def _warning_text(message: warnings.WarningMessage, roots: tuple[str, str, str]) -> str:
    return _normalize(
        warnings.formatwarning(
            message.message,
            message.category,
            message.filename,
            message.lineno,
        ).rstrip(),
        roots,
    )


def _exception(report: TestReport, outcome: str, roots: tuple[str, str, str]) -> str | None:
    if outcome in {"passed", "xpassed"}:
        return None
    if outcome == "xfailed" and hasattr(report, "wasxfail"):
        return _normalize(str(report.wasxfail), roots)
    longrepr = report.longrepr
    reprcrash = getattr(longrepr, "reprcrash", None)
    message = getattr(reprcrash, "message", None)
    if isinstance(message, str):
        return _normalize(message, roots)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return _normalize(str(longrepr[2]), roots)
    return _normalize(str(longrepr), roots)


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class _ExecutionRecorder:
    def __init__(
        self,
        *,
        mode: str,
        root: str,
        temporary: str,
        runner: str,
        raw_path: str | None,
        observer_path: str,
        session_token: str,
        inventory_nodeids: frozenset[str],
        inventory_nodeids_sha256: str,
        outer_config: Config | None = None,
    ) -> None:
        self.mode = mode
        self.roots = (root, temporary, runner)
        self.raw_path = raw_path
        self.observer_path = observer_path
        self.session_token = session_token
        self.inventory_nodeids = inventory_nodeids
        self.inventory_nodeids_sha256 = inventory_nodeids_sha256
        self.outer_config = outer_config
        self.observer_nonce = secrets.token_hex(32)
        self.reports: list[dict[str, Any]] = []
        self.warning_events: list[tuple[str, dict[str, object]]] = []
        self.warning_metadata: list[dict[str, object]] = []
        self.collected: int | None = None
        self.collected_nodeids: list[str] = []
        self.exit_status: int | None = None
        self.rootdir: str | None = None
        self.loaded_pytest_plugins: list[dict[str, str]] = []
        self.native_result: _NativeExecutionResult | None = None

    def _native_execution_result(self, result: _NativeExecutionResult) -> None:
        """Retain Rust's completed record until late session warnings are known."""
        if self.mode != "native" or self.outer_config is None:
            raise RuntimeError("native result was supplied outside the native outer session")
        self.native_result = result
        self._native_execution_warning_records(result.warning_records())

    def _native_execution_warning_records(self, records: list[tuple[str, str, int, str]]) -> None:
        """Observe newly merged worker warnings without replaying earlier events."""
        for message, filename, lineno, nodeid in records:
            self.warning_events.append(
                (
                    nodeid,
                    {
                        "message": _normalize(message, self.roots).rstrip(),
                        "filename": _normalize(filename, self.roots),
                        "lineno": lineno,
                    },
                )
            )

    def _native_warning_records(self) -> list[dict[str, object]]:
        return [{"nodeid": nodeid, **message} for nodeid, message in self.warning_events]

    def pytest_warning_recorded(
        self,
        warning_message: warnings.WarningMessage,
        when: str,
        nodeid: str,
        location: tuple[str, int, str] | None,
    ) -> None:
        """Keep warning multiplicity and order without inventing a runtest phase."""
        del when, location
        text = _warning_text(warning_message, self.roots)
        warning = warning_message.message
        if (
            pytest.__version__ == "9.1.1"
            and warning_message.category is pytest.PytestRemovedIn10Warning
            and isinstance(warning, Warning)
            and len(warning.args) == 1
            and isinstance(warning.args[0], str)
            and warning.args[0].startswith(
                "Passing a non-Collection iterable to parametrize is deprecated.\n"
            )
            and warning.args[0].endswith(
                "https://docs.pytest.org/en/stable/deprecations.html#parametrize-iterators"
            )
        ):
            source_line = linecache.getline(
                warning_message.filename, warning_message.lineno
            ).strip()
            framework = sys.modules.get("_pytest.python")
            framework_file = getattr(framework, "__file__", None)
            marker_origin = (
                framework_file is not None
                and Path(warning_message.filename).resolve() == Path(framework_file).resolve()
                and warning_message.lineno == 124
                and source_line
                == "metafunc.parametrize(*marker.args, **marker.kwargs, _param_mark=marker)"
            )
            self.warning_metadata.append(
                {
                    "nodeid": nodeid,
                    "message": text,
                    "filename": _normalize(warning_message.filename, self.roots),
                    "lineno": warning_message.lineno,
                    "category": "PytestRemovedIn10Warning",
                    "raw_message": warning.args[0],
                    "source_line": source_line,
                    "emitter_route": "builtin-marker-parametrize" if marker_origin else None,
                }
            )
        self.warning_events.append(
            (
                nodeid,
                {
                    "message": text,
                    "filename": _normalize(warning_message.filename, self.roots),
                    "lineno": warning_message.lineno,
                },
            )
        )

    def pytest_runtest_logreport(self, report: TestReport) -> None:
        """Record every setup, call, and teardown report in hook order."""
        outcome = report.outcome
        if hasattr(report, "wasxfail"):
            if report.skipped:
                outcome = "xfailed"
            elif report.failed:
                outcome = "failed"
            else:
                outcome = "xpassed"
        elif report.failed and report.when != "call":
            outcome = "error"
        exception = _exception(report, outcome, self.roots)
        diagnostic = None
        if outcome not in {"passed", "xpassed"}:
            wasxfail = getattr(report, "wasxfail", None)
            if (
                outcome == "xfailed"
                and isinstance(wasxfail, str)
                and wasxfail.startswith("[NOTRUN] ")
            ):
                diagnostic = wasxfail
            else:
                diagnostic = report.longreprtext
        stdout = "".join(
            text for name, text in report.sections if name == f"Captured stdout {report.when}"
        )
        self.reports.append(
            {
                "nodeid": report.nodeid,
                "phase": report.when,
                "outcome": outcome,
                "exception": exception,
                "diagnostic": diagnostic,
                "stdout": stdout,
                "stderr": "".join(
                    text
                    for name, text in report.sections
                    if name == f"Captured stderr {report.when}"
                ),
                "log": "".join(
                    text for name, text in report.sections if name == f"Captured log {report.when}"
                ),
            }
        )

    def cases(self) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
        cases: list[dict[str, Any]] = []
        by_nodeid: dict[str, list[dict[str, Any]]] = {}
        case_by_nodeid: dict[str, dict[str, Any]] = {}
        for raw_report in self.reports:
            report = dict(raw_report)
            nodeid = report.pop("nodeid")
            if nodeid not in by_nodeid or (
                report["phase"] == "setup"
                and by_nodeid[nodeid]
                and by_nodeid[nodeid][-1]["phase"] == "teardown"
            ):
                reports: list[dict[str, Any]] = []
                by_nodeid[nodeid] = reports
                case = {"nodeid": nodeid, "reports": reports, "warnings": []}
                case_by_nodeid[nodeid] = case
                cases.append(case)
            by_nodeid[nodeid].append(report)
        session_warnings: list[dict[str, object]] = []
        for nodeid, message in self.warning_events:
            if nodeid not in by_nodeid:
                session_warnings.append({"nodeid": nodeid, **message})
                continue
            case_by_nodeid[nodeid]["warnings"].append(message)
        return cases, session_warnings

    def pytest_collection_finish(self, session: Session) -> None:
        self.collected_nodeids = [item.nodeid for item in session.items]

    def pytest_sessionfinish(self, session: Session, exitstatus: int) -> None:
        """Retain final status while later reporting hooks are still running."""
        self.collected = session.testscollected
        self.exit_status = int(exitstatus)
        self.rootdir = str(Path(session.config.rootpath).resolve())
        self.loaded_pytest_plugins = [
            {
                "name": session.config.pluginmanager.get_canonical_name(plugin),
                "distribution": re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower(),
                "version": distribution.version,
            }
            for plugin, distribution in session.config.pluginmanager.list_plugin_distinfo()
        ]

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_unconfigure(self, config: Config) -> Generator[None, object, None]:
        """Publish after every other unconfigure and warning hook has completed."""
        del config
        try:
            yield
        finally:
            self._publish()

    def _publish(self) -> None:
        if self.collected is None or self.exit_status is None or self.rootdir is None:
            raise RuntimeError("stock execution recorder has no completed session")
        tmp_path_factory = getattr(self.outer_config, "_tmp_path_factory", None)
        basetemp = getattr(tmp_path_factory, "_basetemp", None)
        pytest_temporary = str(Path(basetemp).resolve()) if basetemp is not None else None
        if pytest_temporary is not None:
            for report in self.reports:
                exception = report["exception"]
                if isinstance(exception, str):
                    report["exception"] = _normalize(exception, self.roots, pytest_temporary)
            for _, warning in self.warning_events:
                warning["message"] = _normalize(warning["message"], self.roots, pytest_temporary)
                warning["filename"] = _normalize(warning["filename"], self.roots, pytest_temporary)
        pytest_file = pytest.__file__
        if pytest_file is None:
            raise RuntimeError("pytest has no import origin")
        identity = {
            "schema_version": 2,
            "producer_pid": os.getpid(),
            "session_token": self.session_token,
            "observer_nonce": self.observer_nonce,
            "session_depth": 0,
            "rootdir": self.rootdir,
            "pytest_temporary": pytest_temporary,
            "pytest_origin": str(Path(pytest_file).resolve()),
            "loaded_pytest_plugins": self.loaded_pytest_plugins,
            "inventory_nodeids_sha256": self.inventory_nodeids_sha256,
            "inventory_nodeid_count": len(self.inventory_nodeids),
        }
        if self.mode == "native":
            imports = []
            loaded = {"vfy._native"}
            loaded.update(
                name
                for name, module in list(sys.modules.items())
                if (name == "vfy" or name.startswith("vfy."))
                and isinstance(getattr(module, "__file__", None), str)
            )
            for name in sorted(loaded):
                module = sys.modules.get(name)
                origin = getattr(module, "__file__", None)
                if not isinstance(origin, str):
                    raise RuntimeError(f"native observer cannot identify loaded module {name}")
                path = Path(origin).resolve()
                imports.append(
                    {
                        "module": name,
                        "origin": str(path),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
            identity["native_imports"] = imports
        _write_atomic(Path(self.observer_path), identity)
        if self.mode == "native":
            if self.raw_path is None or self.native_result is None:
                raise RuntimeError("native execution recorder has no completed Rust result")
            self.native_result.publish_execution_record(
                self.raw_path,
                self._native_warning_records(),
            )
            return
        if self.mode != "pytest":
            return
        if self.raw_path is None:
            raise RuntimeError("stock execution recorder has no raw destination")
        cases, session_warnings = self.cases()
        _write_atomic(
            Path(self.raw_path),
            {
                **identity,
                "collected": self.collected,
                "collected_nodeids": self.collected_nodeids,
                "exit_status": self.exit_status,
                "cases": cases,
                "session_warnings": session_warnings,
                "warning_metadata": self.warning_metadata,
            },
        )


def pytest_configure(config: Config) -> None:
    """Register the observer once in this process, on the outer plug-in manager."""
    if _state["registered"]:
        return
    inventory_nodeids_path = Path(os.environ["VFY_PYTEST_RECORD_INVENTORY_NODEIDS"])
    inventory_nodeids_bytes = inventory_nodeids_path.read_bytes()
    inventory_nodeids_sha256 = hashlib.sha256(inventory_nodeids_bytes).hexdigest()
    if inventory_nodeids_sha256 != os.environ["VFY_PYTEST_RECORD_INVENTORY_NODEIDS_SHA256"]:
        raise RuntimeError("pytest execution inventory hash is invalid")
    inventory_nodeids = json.loads(inventory_nodeids_bytes)
    if (
        not isinstance(inventory_nodeids, list)
        or any(not isinstance(nodeid, str) for nodeid in inventory_nodeids)
        or len(inventory_nodeids) != len(set(inventory_nodeids))
    ):
        raise RuntimeError("pytest execution inventory is malformed")
    config.pluginmanager.register(
        _ExecutionRecorder(
            mode=os.environ["VFY_PYTEST_RECORD_MODE"],
            root=os.environ["VFY_PYTEST_RECORD_ROOT"],
            temporary=os.environ["VFY_PYTEST_RECORD_TEMP"],
            runner=os.environ["VFY_PYTEST_RECORD_PREFIX"],
            raw_path=os.environ.get("VFY_PYTEST_RECORD_RAW"),
            observer_path=os.environ["VFY_PYTEST_OBSERVER_RESULT"],
            session_token=os.environ["VFY_PYTEST_RECORD_SESSION"],
            inventory_nodeids=frozenset(inventory_nodeids),
            inventory_nodeids_sha256=inventory_nodeids_sha256,
            outer_config=config,
        ),
        "_vfy_execution_recorder",
    )
    _state["registered"] = True
