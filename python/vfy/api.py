"""Coverage.py-shaped public API for data access, merging, and reports."""

from __future__ import annotations

import os
import re
import signal
import sys
import threading
import warnings
import weakref
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from copy import deepcopy
from enum import Enum
from importlib.machinery import ModuleSpec, PathFinder
from importlib.util import find_spec
from pathlib import Path
from types import FrameType, ModuleType
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, cast

from .analysis import DEFAULT_EXCLUDES, DEFAULT_PARTIALS
from .collector import MonitoringCollector
from .config import load as load_config
from .config_types import Config
from .data import CoverageData, CoverageStore, DecisionData, FileCoverage
from .errors import ConfigError, CoverageWarning, NoDataError, UnsupportedFeatureError, VfyError
from .native import (
    ReportRow,
    branch_statistics,
    build_profile,
    format_line_ranges,
    parser_backend,
    validate_run_patch,
    write_report_output,
)
from .plugin_support import Plugins

if TYPE_CHECKING:
    from .semantic_reports import DecisionReport


class ModuleSource(Protocol):
    __file__: str | None


type Morf = str | os.PathLike[str] | ModuleType | ModuleSource
type ReportKey = tuple[
    tuple[str, ...] | None,
    str,
    bool,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    bool,
    bool,
]


class TextWriter(Protocol):
    def write(self, text: str, /) -> object: ...


class _SourcePathFinder(Protocol):
    def _get_spec(self, fullname: str, path: Sequence[str]) -> ModuleSpec | None: ...


def _find_source_package_spec(fullname: str, locations: Sequence[str]) -> ModuleSpec | None:
    if fullname in sys.modules:
        return find_spec(fullname)
    for finder in sys.meta_path:
        if finder is PathFinder:
            # Avoid namespace-path construction, which requires an imported parent.
            # This metadata query still passes the full name to path-entry finders.
            spec = cast(_SourcePathFinder, finder)._get_spec(fullname, locations)
            if spec is not None and spec.loader is None and not spec.submodule_search_locations:
                spec = None
        else:
            spec = finder.find_spec(fullname, locations)
        if spec is not None:
            return spec
    return None


class _DefaultDataFile(Enum):
    MISSING = "MISSING"


_COVERAGE_CONCURRENCY_CHOICES = frozenset(
    {"eventlet", "gevent", "greenlet", "multiprocessing", "thread"}
)
_SUPPORTED_CONCURRENCY = frozenset({"multiprocessing", "thread"})


def _check_concurrency(values: Sequence[str]) -> None:
    """Reject concurrency choices Coverage.py does not define, then those vfy cannot collect."""
    unknown = set(values) - _COVERAGE_CONCURRENCY_CHOICES
    if unknown:
        raise ConfigError(f"Unknown concurrency choices: {', '.join(sorted(unknown))}")
    unsupported = set(values) - _SUPPORTED_CONCURRENCY
    if unsupported:
        raise ValueError(f"unsupported concurrency: {sorted(unsupported)!r}")


def _pattern_list(value: str | Iterable[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def _save_before_os_exit(coverage: Coverage) -> None:
    """Apply Coverage.py's `[run] patch = _exit`: save data before `os._exit`."""
    original = os._exit

    def save_then_exit(status: int) -> NoReturn:
        # The process hook retains stopped instances until exit, as Coverage.py does.
        with suppress(Exception):
            coverage.save()
        original(status)

    # os._exit is a writable process hook; the wrapper preserves its call signature.
    os._exit = save_then_exit  # ty: ignore[invalid-assignment]


class Coverage:
    """Collect, store, and report coverage through a Coverage.py-shaped API."""

    __signature__: _inspect.Signature
    _instances = threading.local()

    def __init__(
        self,
        data_file: str | _DefaultDataFile | None = _DefaultDataFile.MISSING,
        data_suffix: str | bool | None = None,
        cover_pylib: bool | None = None,
        auto_data: bool = False,
        timid: bool | None = None,
        branch: bool | None = None,
        config_file: bool | str = True,
        source: Iterable[str] | None = None,
        source_pkgs: Iterable[str] | None = None,
        source_dirs: Iterable[str] | None = None,
        omit: str | Iterable[str] | None = None,
        include: str | Iterable[str] | None = None,
        debug: Iterable[str] | None = None,
        concurrency: str | Iterable[str] | None = None,
        check_preimported: bool = False,
        context: str | None = None,
        messages: bool = False,
        plugins: Iterable[Callable[[Plugins], None]] | None = None,
        decisions: bool = False,
    ) -> None:
        if timid:
            raise ValueError("vfy has no timid tracer: sys.monitoring is always used")
        self._subprocess_config_file = (
            os.path.abspath(config_file)
            if isinstance(config_file, str)
            and os.path.normcase(config_file) != os.path.normcase(os.devnull)
            else config_file
        )
        config: Config = {}
        if config_file is not False:
            config = load_config(config_file if isinstance(config_file, str) else None)
        run_config = config.get("run", {})
        report_config = config.get("report", {})
        vfy_config = config.get("vfy", {})
        vfy_run = vfy_config.get("run", {})

        if branch is None:
            branch = run_config.get("branch")
        if not decisions:
            decisions = bool(vfy_run.get("decisions", False))
        if source is None:
            source = run_config.get("source")
        source = None if source is None else list(source)
        source_dirs = None if source_dirs is None else list(source_dirs)
        if source_pkgs is None:
            source_pkgs = run_config.get("source_pkgs")
        # Coverage.py copies constructor include/omit to the report options, which
        # take precedence over the configuration file's report patterns.
        constructor_report_include = None if include is None else _pattern_list(include)
        constructor_report_omit = None if omit is None else _pattern_list(omit)
        if context is None:
            context = run_config.get("context")
        no_disk = data_file is None
        # Coverage.py gives COVERAGE_FILE precedence over run:data_file and reports
        # the configured name as written, not the resolved path.
        configured_data_file: str = (
            os.fspath(data_file)
            if isinstance(data_file, (str, os.PathLike))
            else os.getenv("COVERAGE_FILE") or run_config.get("data_file") or ".coverage"
        )
        if data_file is _DefaultDataFile.MISSING:
            data_file = os.getenv("COVERAGE_FILE") or run_config.get("data_file")
        if concurrency is None:
            concurrency = run_config.get("concurrency")
        concurrency_values = (
            [concurrency] if isinstance(concurrency, str) else list(concurrency or [])
        )
        _check_concurrency(concurrency_values)
        self._data_no_disk = no_disk
        # Coverage.py fixes the data destination when data is first initialized.
        self._data_initialized = False
        self._data_suffix = data_suffix
        self._started_once = False
        self._start_options_initialized = False
        # Like Coverage.py, a parallel run without an explicit suffix reads the
        # base file but collects into a process-specific shard.
        self._suffix_on_start = (
            data_suffix is None
            and not no_disk
            and (bool(run_config.get("parallel")) or "multiprocessing" in concurrency_values)
        )
        self.data = CoverageData(
            data_file or os.getenv("COVERAGE_FILE") or ".coverage",
            suffix=data_suffix,
            no_disk=no_disk,
        )
        self._data_base_filename = self.data.base_filename()
        self.decisions = bool(decisions)
        self.branch = bool(branch) or self.decisions
        self.source = [*list(source or []), *list(source_dirs or [])]
        self.source_pkgs = list(source_pkgs or [])
        self.include = (
            list(run_config.get("include", []))
            if constructor_report_include is None
            else constructor_report_include.copy()
        )
        self.omit = (
            list(run_config.get("omit", []))
            if constructor_report_omit is None
            else constructor_report_omit.copy()
        )
        self.context = context or ""
        self.cover_pylib = (
            bool(run_config.get("cover_pylib", False)) if cover_pylib is None else cover_pylib
        )
        self.relative_files = bool(run_config.get("relative_files", False))
        self.auto_data = auto_data
        self.config_file = config_file
        self._paths: dict[str, list[str]] = {
            name: list(aliases) for name, aliases in config.get("paths", {}).items()
        }
        self.data._path_aliases = [list(aliases) for aliases in self._paths.values()]
        self._sigterm = bool(run_config.get("sigterm", False))
        self._old_sigterm = None
        self._at_fork_registered = False
        self._pid = os.getpid()
        self.check_preimported = check_preimported
        self.messages = messages
        self.debug = list(debug or [])
        self.concurrency = concurrency_values
        # Third-party configuration hooks may register arbitrary Python option values.
        self._options: dict[str, Any] = {
            "run:branch": self.branch,
            "run:cover_pylib": self.cover_pylib,
            # Coverage.py reads the environment before subsequent set_option calls.
            "run:core": os.getenv("COVERAGE_CORE") or run_config.get("core"),
            "run:decisions": self.decisions,
            "run:source": None if source is None else list(source),
            "run:source_dirs": list(source_dirs or []),
            "run:include": self.include,
            "run:omit": self.omit,
            "run:context": context,
            "run:dynamic_context": run_config.get("dynamic_context"),
            "run:data_file": configured_data_file,
            "run:concurrency": list(concurrency_values),
            "run:disable_warnings": list(run_config.get("disable_warnings", [])),
            "run:parallel": (
                bool(run_config.get("parallel")) if data_suffix is None else bool(data_suffix)
            ),
            "run:plugins": list(run_config.get("plugins", [])),
            "run:relative_files": self.relative_files,
            "run:sigterm": self._sigterm,
            "run:source_pkgs": self.source_pkgs,
            "report:fail_under": report_config.get("fail_under", 0.0),
            "run:patch": list(run_config.get("patch", [])),
            "report:precision": report_config.get("precision", 0),
            "report:include": (
                report_config.get("include")
                if constructor_report_include is None
                else constructor_report_include
            ),
            "report:omit": (
                report_config.get("omit")
                if constructor_report_omit is None
                else constructor_report_omit
            ),
            "report:contexts": report_config.get("contexts"),
            "report:exclude_lines": list(report_config.get("exclude_lines", DEFAULT_EXCLUDES)),
            "report:exclude_also": list(report_config.get("exclude_also", [])),
            "report:partial_branches": list(
                report_config.get("partial_branches", DEFAULT_PARTIALS)
            ),
            "report:partial_also": list(report_config.get("partial_also", [])),
            "report:show_missing": report_config.get("show_missing", False),
            "report:ignore_errors": report_config.get("ignore_errors", False),
            "report:skip_covered": report_config.get("skip_covered", False),
            "report:skip_empty": report_config.get("skip_empty", False),
            "report:sort": report_config.get("sort"),
            "report:format": report_config.get("format"),
            "html:directory": config.get("html", {}).get("directory", "htmlcov"),
            "html:title": config.get("html", {}).get("title", "Coverage report"),
            "html:extra_css": config.get("html", {}).get("extra_css"),
            "html:show_contexts": config.get("html", {}).get("show_contexts", False),
            "html:skip_covered": config.get("html", {}).get("skip_covered"),
            "html:skip_empty": config.get("html", {}).get("skip_empty"),
            "xml:output": config.get("xml", {}).get("output", "coverage.xml"),
            "xml:package_depth": config.get("xml", {}).get("package_depth", 99),
            "lcov:output": config.get("lcov", {}).get("output", "coverage.lcov"),
            "json:output": config.get("json", {}).get("output", "coverage.json"),
            "json:pretty_print": config.get("json", {}).get("pretty_print", False),
            "json:show_contexts": config.get("json", {}).get("show_contexts", False),
        }
        self._plugin_option_sections = set(run_config.get("plugins", []))
        self._load_configured_options(config)
        self._initialize_plugins(config, plugins)
        self._collector: MonitoringCollector | None = None
        self._reported_collection_data: CoverageData | None = None
        self._pending_collection: CoverageStore | None = None
        self._collection_invalidated = False
        self._warn_pending = False
        self._warn_no_data = True
        self._warn_unimported_source = True
        self._unmatched_source_packages: list[str] | None = None
        self._report_batch_cache: dict[ReportKey, list[ReportRow]] | None = None
        if self.auto_data:
            self.load()

    def _load_configured_options(self, config: Config) -> None:
        for section, values in config.items():
            if isinstance(values, dict):
                if section == "plugin":
                    for module, plugin_values in values.items():
                        if isinstance(plugin_values, dict):
                            for name, value in plugin_values.items():
                                self._options.setdefault(f"{module}:{name}", deepcopy(value))
                elif section != "paths":
                    option_section = section.removeprefix("plugin:")
                    for name, value in values.items():
                        self._options.setdefault(f"{option_section}:{name}", deepcopy(value))

    def _initialize_plugins(
        self, config: Config, plugins: Iterable[Callable[[Plugins], None]] | None
    ) -> None:
        self._plugins = Plugins()
        self._plugins.load_from_config(config.get("run", {}).get("plugins", []), config)
        self._plugins.load_from_callables(plugins or [])
        if self.decisions and self._plugins.file_tracers:
            names = ", ".join(plugin._coverage_plugin_name for plugin in self._plugins.file_tracers)
            raise UnsupportedFeatureError(
                "semantic decision coverage does not support file-tracer plug-ins "
                f"({names}); disable decisions or use a dynamic-context plug-in"
            )
        for pattern in self._options.get("report:exclude_also", []):
            self.exclude(pattern)
        for pattern in self._options.get("report:partial_also", []):
            self.exclude(pattern, "partial")
        for plugin in self._plugins.configurers:
            plugin.configure(self)

    def start(self) -> None:
        try:
            validate_run_patch(self._options["run:patch"])
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        collector = None if self._collection_invalidated else self._collector
        if collector is None:
            self._data_initialized = True
            if not self._start_options_initialized:
                self._sigterm = bool(self._options["run:sigterm"])
            if self._suffix_on_start:
                self._suffix_on_start = False
                self.data._use_suffix(True)
            if (
                self.data._path_aliases
                and not self.data
                and not self.data._no_disk
                and Path(self.data.data_filename()).exists()
            ):
                self.data.read()
            collector = self._new_collector()
        if collector.include_ignored:
            self._warn("--include is ignored because --source is set", "include-ignored")
        if collector is self._collector:
            self._install_process_hooks()
            return
        collector.start()
        try:
            self._install_process_hooks()
        except BaseException:
            # Failed hook registration must release the monitoring tool before a retry.
            collector.disable()
            raise
        self._collector = collector
        self._start_options_initialized = True
        self._collection_invalidated = False
        self._started_once = True
        stack = getattr(self._instances, "stack", [])
        if self not in stack:
            stack.append(self)
        self._instances.stack = stack
        if "multiprocessing" in self.concurrency:
            from .multiprocessing_support import (  # noqa: PLC0415 - only multiprocessing runs patch process startup
                measure_children,
            )

            measure_children(self)

    def _install_process_hooks(self) -> None:
        if hasattr(os, "register_at_fork") and not self._at_fork_registered:
            reference = weakref.ref(self)

            def after_in_child() -> None:
                coverage = reference()
                if coverage is not None:
                    coverage._after_fork()

            os.register_at_fork(after_in_child=after_in_child)
            self._at_fork_registered = True
        if (
            self._sigterm
            and self._old_sigterm is None
            and threading.current_thread() is threading.main_thread()
            and hasattr(signal, "SIGTERM")
        ):
            self._old_sigterm = signal.signal(signal.SIGTERM, self._on_sigterm)
        if "_exit" in self._options["run:patch"]:
            _save_before_os_exit(self)

    def _new_collector(self) -> MonitoringCollector:
        # Like Coverage.py, a source entry that is not on disk names a package,
        # such as `source = ["pkg"]` for `src/pkg` installed or on the path.
        packages = [source for source in self.source if not os.path.exists(source)]
        return MonitoringCollector(
            branch=self.branch,
            include=self.include,
            omit=self.omit,
            source=[source for source in self.source if source not in packages],
            source_pkgs=[*self.source_pkgs, *packages],
            context=self.context,
            plugins=self._plugins.file_tracers,
            dynamic_contexts=self._plugins.context_switchers,
            cover_pylib=self.cover_pylib,
            relative_files=self.relative_files,
            decisions=self.decisions,
            dynamic_context=self._dynamic_context(),
            core=self._options["run:core"],
        )

    def _dynamic_context(self) -> str:
        value = self._options.get("run:dynamic_context") or ""
        if not isinstance(value, str) or value not in {"", "none", "test_function"}:
            raise ConfigError(f"Don't understand dynamic_context setting: {value!r}")
        return value

    def _after_fork(self) -> None:
        if self._collector is None or self._pid == os.getpid():
            return
        inherited_collector = self._collector
        self._collector.disable()
        self._collector = None
        self._pid = os.getpid()
        aliases = self.data._path_aliases
        reported_destination = self._reported_collection_data
        self.data = CoverageData(self.data.base_filename(), suffix=True, no_disk=self.data._no_disk)
        self.data._path_aliases = aliases
        self._reported_collection_data = None
        if reported_destination is not None:
            self.data._store = CoverageStore(
                has_arcs=self.branch, metadata={"has_arcs": "1" if self.branch else "0"}
            )
            self._reported_collection_data = CoverageData(
                reported_destination.base_filename(),
                suffix=True,
                no_disk=reported_destination._no_disk,
            )
            self._reported_collection_data._path_aliases = aliases
        self._pending_collection = None
        if self._collection_invalidated:
            self._collector = inherited_collector
            return
        collector = self._new_collector()
        try:
            collector.start()
            # CPython carries per-code-object monitoring state across a fork.
            collector.restart_after_fork()
        except BaseException:
            collector.disable()
            stack = getattr(self._instances, "stack", [])
            if self in stack:
                stack.remove(self)
            raise
        self._collector = collector
        self._collection_invalidated = False

    def _adopt_forked_data_file(self, path: str) -> None:
        """Write a forked worker's data to the shard its supervisor registered.

        The at-fork hook gives the child a fresh collector and an empty store whose
        file name carries a per-process suffix. A supervisor that validates its
        shards needs a fixed name, which the child knows only after the fork.
        """
        if self._collector is None or self._pid != os.getpid():
            raise RuntimeError("coverage did not restart in this forked process")
        aliases = self.data._path_aliases
        self.data = CoverageData(path, no_disk=self.data._no_disk)
        self.data._path_aliases = aliases
        self.data._store = CoverageStore(
            has_arcs=self.branch, metadata={"has_arcs": "1" if self.branch else "0"}
        )
        self.data.set_context(self.context)

    def _use_child_data_file(self, basename: str) -> None:
        """Write this forked child's shard beside its supervisor's other child shards."""
        if self.data.base_filename() == str(Path(basename).resolve()):
            return
        aliases = self.data._path_aliases
        store = self.data._store
        self.data = CoverageData(basename, suffix=True, no_disk=self.data._no_disk)
        self.data._path_aliases = aliases
        self.data._store = store
        self.data.set_context(self.context)

    def _on_sigterm(self, _signum: int, _frame: FrameType | None) -> None:
        self.stop()
        self.save()
        previous = self._old_sigterm
        signal.signal(signal.SIGTERM, previous)
        os.kill(os.getpid(), signal.SIGTERM)

    def stop(self) -> None:
        if self._collector is None:
            return
        if self._collection_invalidated:
            self._collector.disable()
            collected = None
        else:
            self._note_unmatched_source_packages(self._collector)
            collected = self._collector.stop()
        self._collector = None
        self._collection_invalidated = False
        if collected is not None:
            self._warn_pending = True
            if self.data._path_aliases:
                if self._pending_collection is None:
                    self._pending_collection = collected
                else:
                    self._pending_collection.merge(collected)
            else:
                self.data._store.merge(collected)
        stack = getattr(self._instances, "stack", [])
        if self in stack:
            stack.remove(self)
        if self.auto_data:
            self.save()

    @classmethod
    def current(cls) -> Coverage | None:
        """Return the innermost active coverage instance in this thread."""
        stack = cast("list[Coverage]", getattr(cls._instances, "stack", []))
        return stack[-1] if stack else None

    @contextmanager
    def collect(self) -> Iterator[None]:
        """Collect coverage for the duration of a ``with`` block."""
        self.start()
        try:
            yield
        finally:
            self.stop()

    @property
    def _started(self) -> bool:
        return self._collector is not None

    def switch_context(self, new_context: str | None) -> None:
        if not self._started:
            raise VfyError("Cannot switch context, coverage is not started")
        self.context = new_context or ""
        self.data.set_context(self.context)
        if self._collector is not None:
            self._collector.switch_context(self.context)

    def save(self) -> None:
        self._prepare_save()
        self.data.write()

    def _prepare_save(self) -> None:
        """Flush the Rust collector without replacing the destination file."""
        self._flush_collection(discover_sources=True)
        if self.decisions:
            self.data._store.metadata["vfy_optimize"] = str(sys.flags.optimize)

    def load(self) -> None:
        self._data_initialized = True
        if self._suffix_on_start and Path(self.data.data_filename()).exists():
            # Coverage.py keeps collecting into a parallel run's existing base file.
            self._suffix_on_start = False
        self.data.read()
        self._pending_collection = None
        if self._collector is not None:
            self._collector.reset()
            if self.data._path_aliases:
                self._collector.disable()
                self._collection_invalidated = True

    def erase(self) -> None:
        destination = self.data
        if not self._data_initialized and destination.data_filename() != self._data_base_filename:
            # Coverage.py's first erase opens the base file before choosing a run suffix.
            destination = CoverageData(self._data_base_filename, no_disk=self._data_no_disk)
        self._data_initialized = True
        aliases = self.data._path_aliases
        parallel = bool(self._options["run:parallel"]) or "multiprocessing" in self.concurrency
        destination.erase(
            parallel=parallel and destination.data_filename() == self._data_base_filename
        )
        self._pending_collection = None
        if self._collector is not None:
            self._collector.reset()
            if aliases:
                self._collector.disable()
                self._collection_invalidated = True
        if aliases:
            self.data = CoverageData(self._data_base_filename, no_disk=self._data_no_disk)
            self.data._path_aliases = aliases
            self._reported_collection_data = None
            self._started_once = True

    def get_data(self) -> CoverageData:
        return self._flush_collection()

    def _flush_collection(self, *, discover_sources: bool = False) -> CoverageData:
        self._data_initialized = True
        collector = None if self._collection_invalidated else self._collector
        destination = self._reported_collection_data
        if destination is None:
            destination = self.data
        has_collection = self._pending_collection is not None or collector is not None
        source_paths = None
        if (
            self._reported_collection_data is not None
            and (self.source or self.source_pkgs)
            and has_collection
        ):
            source_paths = (
                collector.snapshot(discover_sources=True).measured_files()
                if collector is not None
                else self._pending_collection.measured_files()
                if self._pending_collection is not None
                else []
            )
        if self._pending_collection is not None:
            destination._store.merge(self._pending_collection)
            self._pending_collection = None
        if collector is not None:
            destination._store = collector.flush(
                destination._store, discover_sources=discover_sources
            )
        if collector is not None:
            self._warn_pending = True
            self._note_unmatched_source_packages(collector)
        if self._reported_collection_data is not None and has_collection:
            destination.write()
        if source_paths:
            current = self.data._store.to_rust()
            for path in source_paths:
                current.touch_file(path)
            self.data._store = CoverageStore(rust_data=current)
        self._warn_after_collection()
        return self.data

    def _warn(self, message: str, slug: str) -> None:
        if slug not in (self._options["run:disable_warnings"] or []):
            warnings.warn(f"{message} ({slug})", category=CoverageWarning, stacklevel=3)

    def _note_unmatched_source_packages(self, collector: MonitoringCollector) -> None:
        current = collector.unmatched_source_packages()
        previous = self._unmatched_source_packages
        self._unmatched_source_packages = (
            current if previous is None else [name for name in previous if name in current]
        )

    def _warn_after_collection(self) -> None:
        if not self._warn_pending:
            return
        self._warn_pending = False
        # Coverage.py defers source-package validation until collected data is saved.
        packages = self._unmatched_source_packages if self._warn_unimported_source else None
        for package in packages or []:
            module = sys.modules.get(package)
            if module is None:
                self._warn(f"Module {package} was never imported.", "module-not-imported")
                continue
            filename = getattr(module, "__file__", None)
            if filename is None and hasattr(module, "__path__"):
                continue
            if filename is None or not os.path.exists(filename):
                self._warn(f"Module {package} has no Python source.", "module-not-python")
            else:
                self._warn(
                    f"Module {package} was previously imported, but not measured",
                    "module-not-measured",
                )
        if self._warn_no_data and not self.data:
            self._warn("No data was collected.", "no-data-collected")

    def get_decision_data(self) -> DecisionData:
        """Return the Rust semantic decision observations for this run."""
        return self.get_data()._store.decision_data()

    def decision_report(
        self,
        *,
        include: Sequence[str] | None = None,
        omit: Sequence[str] | None = None,
        contexts: Sequence[str] | None = None,
        source: Sequence[str] | None = None,
        exclude_decisions: Sequence[str] | None = None,
        format: str = "text",  # noqa: A002 - mirrors CLI and report API terminology
        output: str | None = None,
        pretty: bool = False,
    ) -> DecisionReport:
        """Build the opt-in Rust semantic report model.

        ``format`` may be ``"text"`` or ``"json"``. The structured report is
        returned in all cases; ``output`` optionally writes its rendering.
        """
        from .semantic_reports import (  # noqa: PLC0415 - collection does not use semantic reports
            build_semantic_report,
            render_semantic_text,
            semantic_json,
        )

        store = self._report_store()
        report = build_semantic_report(
            store,
            include=include,
            omit=omit,
            contexts=contexts,
            source_roots=source or self.source,
            exclude_decisions=exclude_decisions,
        )
        if format not in {"text", "json"}:
            raise ValueError(f"unsupported semantic report format: {format!r}")
        content = (
            semantic_json(report, pretty=pretty)
            if format == "json"
            else render_semantic_text(report)
        )
        if output is None:
            return report
        write_report_output(content, output)
        return report

    def combine(
        self,
        data_paths: Iterable[str] | None = None,
        strict: bool = False,
        keep: bool = False,
    ) -> None:
        self.data._combine(data_paths, strict=strict, keep=keep, relative_files=self.relative_files)

    @staticmethod
    def _morf_path(morf: Morf, *, canonicalize: bool = True) -> str:
        # Coverage accepts module-like objects exposing __file__, including third-party proxies.
        candidate = cast("str | os.PathLike[str] | None", getattr(morf, "__file__", morf))
        if candidate is None:
            raise TypeError("module has no __file__")
        filename = os.fspath(candidate)
        return str(Path(filename).resolve()) if canonicalize else filename

    def analysis2(self, morf: Morf) -> tuple[str, list[int], list[int], list[int], str]:
        """Return filename, statements, excluded, missing, and missing ranges."""

        row = self._build_report_rows(
            [morf], include=[], omit=[], contexts=[], ignore_errors=False
        )[0]
        missing = sorted(row.missing)
        return (
            row.data_filename,
            sorted(row.executable_lines),
            sorted(row.excluded_lines),
            missing,
            format_line_ranges(missing),
        )

    def analysis(self, morf: Morf) -> tuple[str, list[int], list[int], str]:
        filename, statements, _excluded, missing, formatted = self.analysis2(morf)
        return filename, statements, missing, formatted

    def branch_stats(self, morf: Morf) -> dict[int, tuple[int, int]]:
        """Return ``possible, taken`` branch counts keyed by source line."""

        row = self._build_report_rows(
            [morf], include=[], omit=[], contexts=[], ignore_errors=False
        )[0]
        return branch_statistics(sorted(row.possible_arcs), sorted(row.covered_arcs))

    def exclude(self, regex: str, which: str = "exclude") -> None:
        option = self._exclude_option(which)
        re.compile(regex)
        self._options[option].append(regex)

    def clear_exclude(self, which: str = "exclude") -> None:
        self._options[self._exclude_option(which)] = []

    def get_exclude_list(self, which: str = "exclude") -> list[str]:
        return list(self._options[self._exclude_option(which)])

    @staticmethod
    def _exclude_option(which: str) -> str:
        if which == "exclude":
            return "report:exclude_lines"
        if which == "partial":
            return "report:partial_branches"
        raise ValueError(f"unknown exclusion list: {which!r}")

    def get_option(self, option_name: str) -> Any:  # noqa: ANN401 - third-party plug-in option values are arbitrary Python objects
        if option_name == "paths":
            return deepcopy(self._paths)
        if option_name not in self._options:
            section, _, _name = option_name.partition(":")
            if section in self._plugin_option_sections:
                return None
            raise ConfigError(f"No such option: {option_name!r}")
        return deepcopy(self._options[option_name])

    def set_option(self, option_name: str, value: Any) -> None:  # noqa: ANN401 - Coverage.py permits arbitrary third-party plug-in options
        if option_name == "paths":
            self._paths = deepcopy(value)
            self.data._path_aliases = [list(aliases) for aliases in self._paths.values()]
            return
        if option_name not in self._options:
            section, _, _name = option_name.partition(":")
            if section not in self._plugin_option_sections:
                raise ConfigError(f"No such option: {option_name!r}")
        if option_name == "run:concurrency" and not self._start_options_initialized:
            _check_concurrency([value] if isinstance(value, str) else list(value or []))
        self._options[option_name] = deepcopy(value)
        section, name = option_name.split(":", 1)
        if section == "run":
            self._set_run_option(name, value)

    def _set_run_option(self, name: str, value: Any) -> None:  # noqa: ANN401 - mirrors the public option adapter
        if name in {"source", "source_dirs"}:
            self.source = [
                *(self._options["run:source"] or []),
                *(self._options["run:source_dirs"] or []),
            ]
        elif name in {"include", "omit"}:
            setattr(self, name, list(value or []))
        elif name == "source_pkgs":
            self.source_pkgs = list(value or [])
        elif name == "branch":
            self.branch = bool(value)
        elif name == "cover_pylib":
            self.cover_pylib = bool(value)
        elif name == "context":
            self.context = str(value or "")
        elif name == "data_file" and not self._data_initialized:
            self.data = CoverageData(value, suffix=self._data_suffix, no_disk=self._data_no_disk)
            self.data._path_aliases = [list(aliases) for aliases in self._paths.values()]
            self._data_base_filename = self.data.base_filename()
        elif name == "parallel" and not self._data_initialized:
            self._suffix_on_start = (
                self._data_suffix is None
                and not self._data_no_disk
                and (bool(value) or "multiprocessing" in self.concurrency)
            )
        elif name == "relative_files" and not self._data_initialized:
            self.relative_files = bool(value)
        elif name == "concurrency" and not self._start_options_initialized:
            self.concurrency = [value] if isinstance(value, str) else list(value or [])
            if not self._data_initialized:
                self._suffix_on_start = (
                    self._data_suffix is None
                    and not self._data_no_disk
                    and (
                        bool(self._options["run:parallel"]) or "multiprocessing" in self.concurrency
                    )
                )

    def sys_info(self) -> list[tuple[str, object]]:

        return [
            ("implementation", "vfy"),
            ("version", __import__("vfy").__version__),
            ("native build", build_profile()),
            ("parser", parser_backend()),
            ("python", sys.version.replace("\n", " ")),
            ("branch", self.branch),
            ("plugins", [plugin._coverage_plugin_name for plugin in self._plugins]),
        ]

    def _prepare_data_for_reporting(self) -> None:
        aliases = self.data._path_aliases
        if not aliases:
            return
        data_exists = not self.data._no_disk and Path(self.data.data_filename()).exists()
        if not self._started_once and not self.data and data_exists:
            self.data.read()
        mapped_store = self.data._store.remap_paths_for_reporting(
            aliases, relative_files=self.relative_files
        )
        mapped_data = CoverageData(no_disk=True)
        mapped_data._store = mapped_store
        mapped_data._path_aliases = aliases
        if self._started_once and self._reported_collection_data is None:
            self._reported_collection_data = self.data
        self.data = mapped_data
        if self._report_batch_cache is not None:
            self._report_batch_cache.clear()

    @contextmanager
    def _report_batch(self) -> Iterator[None]:
        """Reuse Rust source analysis across a group of report formats."""
        if self._report_batch_cache is not None:
            yield
            return
        self._report_batch_cache = {}
        try:
            yield
        finally:
            self._report_batch_cache = None

    def _report_patterns(self, name: str) -> list[str]:
        value: object = self.get_option(f"report:{name}")
        if value is None:
            return []
        if isinstance(value, Iterable):
            patterns: list[str] = []
            for item in value:
                if not isinstance(item, str):
                    break
                patterns.append(item)
            else:
                return patterns
        raise ConfigError(f"report:{name} must be an iterable of strings")

    def _build_report_rows(
        self,
        morfs: Iterable[Morf] | None,
        *,
        include: Sequence[str] | None = None,
        omit: Sequence[str] | None = None,
        contexts: Sequence[str] | None = None,
        ignore_errors: bool | None = None,
        line_contexts: bool = False,
    ) -> list[ReportRow]:
        from . import reports  # noqa: PLC0415 - collection does not build report rows

        ignore_errors = bool(
            self.get_option("report:ignore_errors") if ignore_errors is None else ignore_errors
        )
        if include is None:
            include = self._report_patterns("include")
        if omit is None:
            omit = self._report_patterns("omit")
        if contexts is None:
            contexts = self._report_patterns("contexts")
        paths = (
            None
            if morfs is None
            else tuple(self._morf_path(morf, canonicalize=False) for morf in morfs)
        )
        key = (
            paths,
            str(Path.cwd()),
            self.relative_files,
            tuple(include or ()),
            tuple(omit or ()),
            tuple(contexts or ()),
            tuple(self.get_exclude_list("exclude")),
            tuple(self.get_exclude_list("partial")),
            ignore_errors,
            line_contexts,
        )
        if self._report_batch_cache is not None and key in self._report_batch_cache:
            return self._report_batch_cache[key]
        store = self._report_store()
        if paths is not None and any(self._plugins):
            # Third-party file reporters retain their existing morf protocol.
            store = store.select({self._morf_path(path) for path in paths})
        rows = reports.build_rows(
            store,
            include=include,
            omit=omit,
            contexts=contexts,
            excludes=self.get_exclude_list("exclude"),
            partials=self.get_exclude_list("partial"),
            plugins=self._plugins,
            ignore_errors=ignore_errors,
            relative_files=self.relative_files,
            requested_paths=paths,
            line_contexts=line_contexts,
        )
        if not rows:
            raise NoDataError("No data to report.")
        if self._report_batch_cache is not None:
            self._report_batch_cache[key] = rows
        return rows

    @staticmethod
    def _percentage(rows: list[ReportRow]) -> float:
        from .reports import totals  # noqa: PLC0415 - collection does not compute report totals

        return totals(rows).percent

    def _report_store(self) -> CoverageStore:
        self._warn_after_collection()
        if self.data._path_aliases:
            store = self.get_data()._store
            data_exists = not self.data._no_disk and Path(self.data.data_filename()).exists()
            if not self._started_once and not self.data and data_exists:
                self.data.read()
                store = self.data._store
        elif self._collector is not None:
            store = self.data._store.copy()
            store.merge(self._collector.snapshot(discover_sources=True))
        else:
            data_exists = not self.data._no_disk and Path(self.data.data_filename()).exists()
            if not self._started_once and not self.data and data_exists:
                self.data.read()
            store = self.data._store
        # File-tracer plug-ins are rejected in decision mode. Keep the store
        # Rust-backed so definitions and observations reach the report model.
        if self.decisions:
            return store
        for plugin in self._plugins:
            for source_root in self.source:
                for filename in plugin.find_executable_files(source_root):
                    path = str(Path(filename).resolve())
                    file = store.files.setdefault(path, FileCoverage(path))
                    file.tracer = plugin._coverage_plugin_name
        return store

    def report(
        self,
        morfs: Iterable[Morf] | None = None,
        show_missing: bool | None = None,
        ignore_errors: bool | None = None,
        file: TextWriter | None = None,
        omit: Sequence[str] | None = None,
        include: Sequence[str] | None = None,
        skip_covered: bool | None = None,
        contexts: Sequence[str] | None = None,
        skip_empty: bool | None = None,
        precision: int | None = None,
        sort: str | None = None,
        output_format: str | None = None,
    ) -> float:
        from .reports import (  # noqa: PLC0415 - load renderers only for requested reports
            render_markdown,
            render_terminal,
            render_total,
        )

        if output_format is None:
            output_format = str(self.get_option("report:format") or "text")
        if output_format not in {"text", "markdown", "total"}:
            raise ValueError(f"unknown report output format: {output_format!r}")

        self._prepare_data_for_reporting()
        rows = self._build_report_rows(
            morfs,
            include=include,
            omit=omit,
            contexts=contexts,
            ignore_errors=ignore_errors,
        )
        resolved_skip_covered = bool(
            self.get_option("report:skip_covered") if skip_covered is None else skip_covered
        )
        resolved_skip_empty = bool(
            self.get_option("report:skip_empty") if skip_empty is None else skip_empty
        )
        resolved_sort = self.get_option("report:sort") if sort is None else sort
        resolved_precision = int(
            self.get_option("report:precision") if precision is None else precision
        )
        resolved_missing = bool(
            self.get_option("report:show_missing") if show_missing is None else show_missing
        )
        if output_format == "markdown":
            content = render_markdown(
                rows,
                resolved_precision,
                resolved_missing,
                skip_covered=resolved_skip_covered,
                skip_empty=resolved_skip_empty,
                sort=resolved_sort,
            )
        elif output_format == "total":
            content = render_total(
                rows,
                resolved_precision,
                skip_covered=resolved_skip_covered,
                skip_empty=resolved_skip_empty,
                sort=resolved_sort,
            )
        else:
            content = render_terminal(
                rows,
                resolved_precision,
                resolved_missing,
                skip_covered=resolved_skip_covered,
                skip_empty=resolved_skip_empty,
                sort=resolved_sort,
            )
        (file or sys.stdout).write(content)
        return self._percentage(rows)

    def xml_report(
        self,
        morfs: Iterable[Morf] | None = None,
        outfile: str | None = None,
        ignore_errors: bool | None = None,
        omit: Sequence[str] | None = None,
        include: Sequence[str] | None = None,
        contexts: Sequence[str] | None = None,
        skip_empty: bool | None = None,
    ) -> float:
        from .reports import (  # noqa: PLC0415 - load renderers only for requested reports
            render_xml,
            select_rows,
            write_output,
        )

        self._prepare_data_for_reporting()
        rows = self._build_report_rows(
            morfs,
            include=include,
            omit=omit,
            contexts=contexts,
            ignore_errors=ignore_errors,
        )
        rows = select_rows(
            rows,
            skip_empty=bool(
                self.get_option("report:skip_empty") if skip_empty is None else skip_empty
            ),
        )
        write_output(
            render_xml(
                rows,
                self.get_option("xml:package_depth"),
                self.source,
                self.relative_files,
            ),
            outfile or self.get_option("xml:output"),
        )
        return self._percentage(rows)

    def json_report(
        self,
        morfs: Iterable[Morf] | None = None,
        outfile: str | None = None,
        ignore_errors: bool | None = None,
        omit: Sequence[str] | None = None,
        include: Sequence[str] | None = None,
        contexts: Sequence[str] | None = None,
        pretty_print: bool | None = None,
        show_contexts: bool | None = None,
    ) -> float:
        from .reports import (  # noqa: PLC0415 - load renderers only for requested reports
            render_json,
            write_output,
        )

        self._prepare_data_for_reporting()
        show_contexts = bool(
            self.get_option("json:show_contexts") if show_contexts is None else show_contexts
        )
        rows = self._build_report_rows(
            morfs,
            include=include,
            omit=omit,
            contexts=contexts,
            ignore_errors=ignore_errors,
            line_contexts=show_contexts,
        )
        write_output(
            render_json(
                rows,
                pretty=bool(
                    self.get_option("json:pretty_print") if pretty_print is None else pretty_print
                ),
                show_contexts=show_contexts,
                precision=int(self.get_option("report:precision")),
            ),
            outfile or self.get_option("json:output"),
        )
        return self._percentage(rows)

    def lcov_report(
        self,
        morfs: Iterable[Morf] | None = None,
        outfile: str | None = None,
        ignore_errors: bool | None = None,
        omit: Sequence[str] | None = None,
        include: Sequence[str] | None = None,
        contexts: Sequence[str] | None = None,
    ) -> float:
        from .reports import (  # noqa: PLC0415 - load renderers only for requested reports
            render_lcov,
            write_output,
        )

        self._prepare_data_for_reporting()
        rows = self._build_report_rows(
            morfs,
            include=include,
            omit=omit,
            contexts=contexts,
            ignore_errors=ignore_errors,
        )
        write_output(render_lcov(rows), outfile or self.get_option("lcov:output"))
        return self._percentage(rows)

    def html_report(
        self,
        morfs: Iterable[Morf] | None = None,
        directory: str | None = None,
        ignore_errors: bool | None = None,
        omit: Sequence[str] | None = None,
        include: Sequence[str] | None = None,
        extra_css: str | None = None,
        title: str | None = None,
        skip_covered: bool | None = None,
        show_contexts: bool | None = None,
        contexts: Sequence[str] | None = None,
        skip_empty: bool | None = None,
        precision: int | None = None,
    ) -> float:
        from .reports import (  # noqa: PLC0415 - load renderers only for requested reports
            write_html_report,
        )

        self._prepare_data_for_reporting()
        show_contexts = bool(
            self.get_option("html:show_contexts") if show_contexts is None else show_contexts
        )
        rows = self._build_report_rows(
            morfs,
            include=include,
            omit=omit,
            contexts=contexts,
            ignore_errors=ignore_errors,
            line_contexts=show_contexts,
        )
        html_skip_covered = self.get_option("html:skip_covered")
        html_skip_empty = self.get_option("html:skip_empty")
        if skip_covered is None:
            skip_covered = bool(
                self.get_option("report:skip_covered")
                if html_skip_covered is None
                else html_skip_covered
            )
        if skip_empty is None:
            skip_empty = bool(
                self.get_option("report:skip_empty") if html_skip_empty is None else html_skip_empty
            )
        write_html_report(
            rows,
            directory or self.get_option("html:directory"),
            title=self.get_option("html:title") if title is None else title,
            precision=int(self.get_option("report:precision") if precision is None else precision),
            show_contexts=show_contexts,
            extra_css=self.get_option("html:extra_css") if extra_css is None else extra_css,
            skip_covered=bool(skip_covered),
            skip_empty=bool(skip_empty),
        )
        return self._percentage(rows)

    def annotate(
        self,
        morfs: Iterable[Morf] | None = None,
        directory: str | None = None,
        ignore_errors: bool | None = None,
        omit: Sequence[str] | None = None,
        include: Sequence[str] | None = None,
        contexts: Sequence[str] | None = None,
    ) -> None:
        from .reports import (  # noqa: PLC0415 - load renderers only for requested reports
            write_annotation_report,
        )

        self._prepare_data_for_reporting()
        rows = self._build_report_rows(
            morfs,
            include=include,
            omit=omit,
            contexts=contexts,
            ignore_errors=ignore_errors,
        )
        write_annotation_report(rows, directory)


# Expose Coverage.py's signature to introspection while accepting decisions=.
try:
    import inspect as _inspect

    _coverage_signature = _inspect.signature(Coverage)
    Coverage.__signature__ = _coverage_signature.replace(
        parameters=[
            parameter
            for parameter in _coverage_signature.parameters.values()
            if parameter.name != "decisions"
        ]
    )
except (AttributeError, ValueError):
    pass
