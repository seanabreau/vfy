"""Serial Coverage.py reference for the independent benchmark frontend."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import coverage
from coverage.results import should_fail_under

from vfy.independent.facade import hookimpl

if TYPE_CHECKING:
    from vfy.independent.protocol import Config, Item, Session


def _config_strings(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise RuntimeError("oracle coverage configuration must contain string lists")
    return list(value)


class Contexts:
    def __init__(self, oracle: Oracle) -> None:
        self.oracle = oracle

    def pytest_runtest_setup(self, item: Item) -> None:
        self.oracle.switch(item.nodeid, "setup")

    def pytest_runtest_call(self, item: Item) -> None:
        self.oracle.switch(item.nodeid, "run")

    def pytest_runtest_teardown(self, item: Item) -> None:
        self.oracle.switch(item.nodeid, "teardown")


class Oracle:
    def __init__(self) -> None:
        if os.environ.get("VFY_NATIVE_PARALLEL_DIR") or os.environ.get("PYTEST_XDIST_WORKER"):
            raise RuntimeError("the Coverage.py oracle supports one serial process only")
        if coverage.Coverage.current() is not None:
            raise RuntimeError("the Coverage.py oracle requires no active collector")
        self.root = Path.cwd()
        self.data_path = Path(os.environ["VFY_ORACLE_DATA"]).resolve()
        self.evidence = Path(os.environ["VFY_ORACLE_EVIDENCE"]).resolve()
        source = json.loads(os.environ["VFY_ORACLE_SOURCE_JSON"])
        if source is not None and (
            not isinstance(source, list) or any(not isinstance(item, str) for item in source)
        ):
            raise ValueError("VFY_ORACLE_SOURCE_JSON must be null or a list of strings")
        config = os.environ.get("VFY_ORACLE_CONFIG") or True
        branch_arg = os.environ.get("VFY_ORACLE_BRANCH", "config")
        if branch_arg not in {"config", "true", "false"}:
            raise ValueError("VFY_ORACLE_BRANCH must be config, true, or false")
        branch = None if branch_arg == "config" else branch_arg == "true"
        self.collector = coverage.Coverage(
            data_file=str(self.data_path),
            data_suffix=True,
            config_file=config,
            source=source,
            branch=branch,
        )
        self.reporter = coverage.Coverage(
            data_file=str(self.data_path),
            data_suffix=False,
            config_file=config,
            source=source,
            branch=branch,
        )
        if self.collector.get_option("run:dynamic_context"):
            raise RuntimeError("oracle requires hook-owned dynamic contexts")
        concurrency = set(_config_strings(self.collector.get_option("run:concurrency")))
        if concurrency - {"thread"}:
            raise RuntimeError(f"oracle does not support concurrency modes: {sorted(concurrency)}")
        patches = _config_strings(self.collector.get_option("run:patch"))
        unsupported = set(patches) - {"subprocess"}
        if unsupported:
            raise RuntimeError(f"oracle does not support coverage patches: {sorted(unsupported)}")
        self.subprocess_patch = "subprocess" in patches
        self.finished = False
        self.collector.start()
        internal = self.collector._collector
        if internal is None or internal.tracer_name() != "CTracer":
            self.collector.stop()
            raise RuntimeError("oracle requires Coverage.py CTracer")
        process_config = os.environ.get("COVERAGE_PROCESS_CONFIG")
        if self.subprocess_patch and not process_config:
            self.collector.stop()
            raise RuntimeError("Coverage.py subprocess patch did not activate")
        self.record: dict[str, object] = {
            "status": "started",
            "coverage_version": coverage.__version__,
            "tracer": internal.tracer_name(),
            "source": source,
            "config": config,
            "branch": branch_arg,
            "patches": patches,
            "subprocess_config_sha256": (
                hashlib.sha256(process_config.encode()).hexdigest() if process_config else None
            ),
            "data_file": str(self.data_path),
            "pid": os.getpid(),
        }
        self.evidence.write_text(json.dumps(self.record, indent=2) + "\n")

    def switch(self, nodeid: str, phase: str) -> None:
        self.collector.switch_context(f"{nodeid}|{phase}")

    @hookimpl(wrapper=True)
    def pytest_runtest_call(self, item: Item) -> Generator[None, object, object]:
        disabled = bool(item.get_closest_marker("no_cover") or "no_cover" in item.fixturenames)
        if disabled:
            self.collector.stop()
        try:
            return (yield)
        finally:
            if disabled:
                self.collector.start()

    @hookimpl(wrapper=True)
    def pytest_runtestloop(self, session: Session) -> Generator[None, object, object]:
        result = yield
        self.finish()
        return result

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        try:
            previous_directory = Path.cwd()
        except OSError:
            previous_directory = None
        os.chdir(self.root)
        try:
            self.collector.stop()
            self.collector.save()
            parent = Path(self.collector.get_data().data_filename()).resolve()
            # Subprocess patch output must be isolated under this data-file directory.
            shards = sorted(
                path
                for path in self.data_path.parent.glob(self.data_path.name + ".*")
                if path.resolve() != parent
            )
            self.record["parent_shard"] = str(parent)
            self.record["child_shards"] = [str(path) for path in shards]
            self.evidence.write_text(json.dumps(self.record, indent=2) + "\n")
            if self.subprocess_patch and not shards:
                raise RuntimeError(
                    "Coverage.py subprocess patch produced no child coverage evidence"
                )
            self.reporter.combine([str(self.data_path.parent)], strict=True, keep=True)
            self.reporter.save()
            total = self.reporter.json_report(outfile=os.environ["VFY_ORACLE_JSON"])
            self.reporter.xml_report(outfile=os.environ["VFY_ORACLE_XML"])
            threshold = self.reporter.get_option("report:fail_under")
            precision = self.reporter.get_option("report:precision")
            if not isinstance(threshold, (int, float)) or not isinstance(precision, int):
                raise RuntimeError("oracle report threshold and precision must be numeric")
            if should_fail_under(total, threshold, precision):
                raise RuntimeError(
                    f"Coverage.py reference total {total} is below fail-under={threshold}"
                )
            self.record["status"] = "finished"
            self.evidence.write_text(json.dumps(self.record, indent=2) + "\n")
        finally:
            if previous_directory is not None:
                os.chdir(previous_directory)

    def close(self) -> None:
        if not self.finished:
            self.collector.stop()


@hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: Config, args: Sequence[str]) -> None:
    if os.environ.get("VFY_ORACLE_ENABLED") != "1":
        raise RuntimeError("set VFY_ORACLE_ENABLED=1 for the benchmark-only oracle")
    if "--no-cov" not in args:
        raise RuntimeError("oracle requires --no-cov to disable vfy coverage")
    oracle = Oracle()
    early_config.pluginmanager.register(oracle, "_coverage_py_oracle")
    early_config.add_cleanup(oracle.close)
    early_config.pluginmanager.register(Contexts(oracle), "_coverage_py_oracle_contexts")
