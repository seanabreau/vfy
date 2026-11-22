"""Coverage.py-compatible data access backed by the Rust store."""

from __future__ import annotations

import glob
import os
import re
import secrets
import socket
import string
import threading
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .errors import DataError, NoDataError
from .native import CoverageData as ExtensionCoverageData


class PathMapper(Protocol):
    def map(self, path: str) -> str: ...


class CoverageData:
    """Manage measured data using the common Coverage.py API surface.

    The on-disk SQLite format is Coverage.py schema 7. The ``dumps`` format is
    private to vfy and only guaranteed to round-trip through a matching
    vfy build.
    """

    def __init__(
        self,
        basename: str | None = None,
        suffix: str | bool | None = None,
        no_disk: bool = False,
        warn: Callable[[str], None] | None = None,
        debug: object | None = None,
    ) -> None:
        self._basename = str(Path(basename or ".coverage").resolve())
        self._suffix = suffix
        self._no_disk = no_disk
        self._store = CoverageStore()
        self._filename = self._data_filename()
        self._current_context = ""
        self._query_patterns: tuple[re.Pattern[str], ...] | None = None
        self._lock = threading.RLock()
        self._warn = warn
        self._debug = debug
        self._path_aliases: list[list[str]] = []

    def __bool__(self) -> bool:
        native = self._store.rust_data
        return not native.is_empty() if native is not None else bool(self._store.files)

    def _data_filename(self) -> str:
        if self._suffix is True:
            token = "".join(secrets.choice(string.ascii_letters) for _ in range(8))
            suffix = f"{socket.gethostname()}.{os.getpid()}.{token}"
            return f"{self._basename}.{suffix}"
        if isinstance(self._suffix, str):
            return f"{self._basename}.{self._suffix}"
        return self._basename

    def base_filename(self) -> str:
        return self._basename

    def _use_suffix(self, suffix: str | bool | None) -> None:
        """Direct later writes to a differently suffixed file of the same base."""
        self._suffix = suffix
        self._filename = self._data_filename()

    def data_filename(self) -> str:
        return self._filename

    def read(self) -> None:
        with self._lock:
            if self._no_disk:
                return
            if not Path(self._filename).exists():
                self._store = CoverageStore()
                return
            self._store = CoverageStore.read(self._filename)

    def write(self) -> None:
        with self._lock:
            # Coverage.py creates the file on first use; a suffixed write still adds its hash.
            if not self._no_disk and (self._suffix or self._collection_mode_is_known()):
                self._store.write(self._filename)

    def _collection_mode_is_known(self) -> bool:
        """Report whether lines or arcs were ever chosen."""
        return self._store.to_rust().mode_is_known

    def erase(self, parallel: bool = False) -> None:
        paths = [] if self._no_disk else [self._filename]
        if parallel and not self._no_disk:
            paths.append(self._basename)
            paths.extend(discover_parallel_files(self._basename))
        for path in paths:
            Path(path).unlink(missing_ok=True)
        with self._lock:
            self._store = CoverageStore()

    def update(
        self,
        other_data: CoverageData,
        map_path: Callable[[str], str] | None = None,
        aliases: PathMapper | None = None,
    ) -> None:
        """Merge another data collection, optionally remapping file names."""
        other = other_data._store.to_rust()
        mapper = map_path
        if mapper is None and aliases is not None:
            mapper = aliases.map
        if mapper is not None:
            mappings = [(path, mapper(path)) for path in other.measured_files()]
            try:
                other = other.map_files(mappings)
            except ValueError as exc:
                raise DataError(str(exc)) from exc
        with self._lock:
            merged = self._store.to_rust()
            try:
                merged.merge(other)
            except ValueError as exc:
                raise DataError(str(exc)) from exc
            self._store = CoverageStore(rust_data=merged)

    def combine(
        self,
        data_paths: Iterable[str] | None = None,
        *,
        strict: bool = False,
        keep: bool = False,
    ) -> None:
        self._combine(data_paths, strict=strict, keep=keep, relative_files=False)

    def _combine(
        self,
        data_paths: Iterable[str] | None,
        *,
        strict: bool,
        keep: bool,
        relative_files: bool,
    ) -> None:
        paths = list(data_paths or [str(Path(self._basename).parent)])
        with self._lock:
            combined, _count = CoverageStore.combine_transaction(
                paths,
                output=self._filename,
                base=self._store,
                keep=keep,
                strict=strict,
                aliases=self._path_aliases,
                relative_files=relative_files,
            )
            if combined is not None:
                self._store = combined

    def _publish(
        self,
        paths: Iterable[str],
        *,
        append: bool,
        relative_files: bool,
    ) -> None:
        """Replace the base file with this data and the shards in ``paths`` in one transaction."""
        with self._lock:
            self._store = self._store.publish_transaction(
                paths,
                output=self._basename,
                include_output=append,
                aliases=self._path_aliases,
                relative_files=relative_files,
            )
            self._use_suffix(False)

    def has_arcs(self) -> bool:
        return self._store.has_arcs

    def measured_files(self) -> set[str]:
        return set(self._store.measured_files())

    def measured_contexts(self) -> set[str]:
        return set(self._store.to_rust().measured_contexts())

    def _matching_contexts(self, file: FileCoverage) -> set[str]:
        contexts = set(file.contexts) | set(file.context_arcs)
        if self._query_patterns is None:
            return contexts
        return {
            context
            for context in contexts
            if any(pattern.search(context) for pattern in self._query_patterns)
        }

    def lines(self, filename: str) -> list[int] | None:
        file = self._store.files.get(filename)
        if file is None:
            return None
        if self._query_patterns is None:
            return sorted(file.lines or {line for arc in file.arcs for line in arc if line > 0})
        contexts = self._matching_contexts(file)
        lines = set().union(*(file.contexts.get(context, set()) for context in contexts))
        if file.context_arcs:
            lines.update(
                line
                for context in contexts
                for arc in file.context_arcs.get(context, set())
                for line in arc
                if line > 0
            )
        return sorted(lines)

    def arcs(self, filename: str) -> list[tuple[int, int]] | None:
        file = self._store.files.get(filename)
        if file is None:
            return None
        if not self._store.has_arcs:
            return []
        if self._query_patterns is None:
            return sorted(file.arcs)
        contexts = self._matching_contexts(file)
        arcs = set().union(*(file.context_arcs.get(context, set()) for context in contexts))
        return sorted(arcs)

    def contexts_by_lineno(self, filename: str) -> dict[int, list[str]]:
        file = self._store.files.get(filename)
        if file is None:
            return {}
        result: dict[int, set[str]] = {}
        for context in self._matching_contexts(file):
            lines = set(file.contexts.get(context, set()))
            lines.update(
                line for arc in file.context_arcs.get(context, set()) for line in arc if line > 0
            )
            for line in lines:
                result.setdefault(line, set()).add(context)
        return {line: sorted(contexts) for line, contexts in sorted(result.items())}

    def set_context(self, context: str | None) -> None:
        self._current_context = context or ""

    def set_query_context(self, context: str) -> None:
        self._query_patterns = (re.compile(rf"\A{re.escape(context)}\Z"),)

    def set_query_contexts(self, contexts: list[str] | None) -> None:
        # Coverage.py treats an empty list like None: no context filter.
        self._query_patterns = tuple(map(re.compile, contexts)) if contexts else None

    def add_lines(self, line_data: Mapping[str, Collection[int]]) -> None:
        with self._lock:
            try:
                incoming = ExtensionCoverageData.from_records(
                    False,
                    [
                        (filename, [], [], [(self._current_context, list(lines))], [])
                        for filename, lines in line_data.items()
                    ],
                    {},
                    {"has_arcs": "0"},
                )
                data = self._store.to_rust()
                data.merge(incoming)
            except ValueError as exc:
                raise DataError(str(exc)) from exc
            self._store = CoverageStore(rust_data=data)

    def add_arcs(self, arc_data: Mapping[str, Collection[tuple[int, int]]]) -> None:
        with self._lock:
            try:
                incoming = ExtensionCoverageData.from_records(
                    True,
                    [
                        (filename, [], [], [], [(self._current_context, arcs)])
                        for filename, arcs in (
                            (name, list(arcs)) for name, arcs in arc_data.items()
                        )
                        if arcs
                    ],
                    {},
                    {"has_arcs": "1"},
                )
                data = self._store.to_rust()
                data.merge(incoming)
            except ValueError as exc:
                raise DataError(str(exc)) from exc
            self._store = CoverageStore(rust_data=data)

    def add_file_tracers(self, file_tracers: Mapping[str, str]) -> None:
        with self._lock:
            data = self._store.to_rust()
            try:
                data.add_file_tracers(list(file_tracers.items()))
            except ValueError as exc:
                raise DataError(str(exc)) from exc
            self._store = CoverageStore(rust_data=data)

    def file_tracer(self, filename: str) -> str | None:
        file = self._store.files.get(filename)
        return None if file is None else file.tracer

    def touch_file(self, filename: str, plugin_name: str = "") -> None:
        self.touch_files([filename], plugin_name)

    def touch_files(self, filenames: Collection[str], plugin_name: str = "") -> None:
        with self._lock:
            data = self._store.to_rust()
            try:
                data.touch_files(list(filenames), plugin_name)
            except ValueError as exc:
                raise DataError(str(exc)) from exc
            self._store = CoverageStore(rust_data=data)

    def purge_files(self, filenames: Collection[str]) -> None:
        with self._lock:
            if not self._collection_mode_is_known():
                raise DataError("Can't purge files in an empty CoverageData")
            data = self._store.to_rust()
            data.purge_files(list(filenames))
            self._store = CoverageStore(rust_data=data)

    def dumps(self) -> bytes:
        try:
            return self._store.to_rust().serialize()
        except ValueError as exc:
            raise DataError(str(exc)) from exc

    def loads(self, data: bytes) -> None:

        try:
            store = CoverageStore(rust_data=ExtensionCoverageData.deserialize(data))
        except ValueError as exc:
            raise DataError(str(exc)) from exc
        with self._lock:
            self._store = store

    def close(self, force: bool = False) -> None:
        """Compatibility no-op: vfy does not keep SQLite connections open."""

    @classmethod
    def sys_info(cls) -> list[tuple[str, str]]:
        return [("implementation", "vfy"), ("schema", "7")]


@dataclass
class FileCoverage:
    path: str
    lines: set[int] = field(default_factory=set)
    arcs: set[tuple[int, int]] = field(default_factory=set)
    contexts: dict[str, set[int]] = field(default_factory=dict)
    context_arcs: dict[str, set[tuple[int, int]]] = field(default_factory=dict)
    tracer: str = ""


@dataclass(frozen=True)
class DecisionDefinition:
    key: str
    path: str
    line: int
    end_line: int
    kind: str
    source_hash: str
    expression: str
    logic: str
    condition_ranges: tuple[ConditionRange, ...] = ()


@dataclass(frozen=True)
class ConditionRange:
    """Source range for one atomic condition in a decision."""

    index: int
    start_line: int
    start_column: int
    end_line: int
    end_column: int


@dataclass(frozen=True)
class DecisionObservation:
    context: str
    decision: str
    vector: str
    outcome: bool


@dataclass(frozen=True)
class PathOutcomeDefinition:
    index: int
    label: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int


@dataclass(frozen=True)
class PathDecisionDefinition:
    key: str
    path: str
    line: int
    end_line: int
    kind: str
    source_hash: str
    expression: str
    body_start: int
    body_end: int
    outcomes: tuple[PathOutcomeDefinition, ...] = ()


@dataclass(frozen=True)
class PathObservation:
    context: str
    decision: str
    outcome: int


@dataclass(frozen=True)
class DecisionEvidence:
    """Two observations that establish an atomic condition's independent effect.

    ``condition`` is the zero-based index in the decision's source-order
    condition list.
    """

    decision: str
    condition: int
    left_context: str
    left_vector: str
    left_outcome: bool
    right_context: str
    right_vector: str
    right_outcome: bool


class DecisionData:
    """Read-only decision definitions and observations."""

    def __init__(self, rust_data: ExtensionCoverageData) -> None:
        self._rust_data = rust_data

    @property
    def definitions(self) -> tuple[DecisionDefinition, ...]:
        ranges: dict[str, list[ConditionRange]] = {}
        for (
            key,
            index,
            start_line,
            start_column,
            end_line,
            end_column,
        ) in self._rust_data.semantic_condition_records():
            ranges.setdefault(key, []).append(
                ConditionRange(index, start_line, start_column, end_line, end_column)
            )
        return tuple(
            DecisionDefinition(*record, condition_ranges=tuple(ranges.get(record[0], ())))
            for record in self._rust_data.semantic_records()
        )

    @property
    def observations(self) -> tuple[DecisionObservation, ...]:
        return tuple(
            DecisionObservation(*record)
            for record in self._rust_data.semantic_observation_records()
        )

    @property
    def path_definitions(self) -> tuple[PathDecisionDefinition, ...]:
        outcomes: dict[str, list[PathOutcomeDefinition]] = {}
        for record in self._rust_data.semantic_path_outcome_records():
            outcomes.setdefault(record[0], []).append(PathOutcomeDefinition(*record[1:]))
        return tuple(
            PathDecisionDefinition(*record, outcomes=tuple(outcomes.get(record[0], ())))
            for record in self._rust_data.semantic_path_records()
        )

    @property
    def path_observations(self) -> tuple[PathObservation, ...]:
        return tuple(
            PathObservation(*record)
            for record in self._rust_data.semantic_path_observation_records()
        )

    def has_data(self) -> bool:
        return bool(self._rust_data.has_semantic_data())

    def evidence_records(
        self,
        decisions: Iterable[str] = (),
        contexts: Iterable[str] = (),
    ) -> tuple[DecisionEvidence, ...]:
        """Return masking MC/DC pairs with the test context for each observation."""
        return tuple(
            DecisionEvidence(*record)
            for record in self._rust_data.semantic_evidence_records(list(decisions), list(contexts))
        )


class CoverageStore:
    """Rust coverage data with a lazily materialized Python mapping.

    Accessing ``files`` creates mutable ``FileCoverage`` objects for consumers
    such as third-party file reporters. Reads, merges, and built-in reports
    operate on the Rust store directly.
    """

    def __init__(
        self,
        files: dict[str, FileCoverage] | None = None,
        has_arcs: bool = False,
        metadata: dict[str, str] | None = None,
        *,
        rust_data: ExtensionCoverageData | None = None,
        native: ExtensionCoverageData | None = None,
    ) -> None:
        if rust_data is not None and native is not None:
            raise TypeError("pass only one of rust_data or native")
        rust_data = rust_data if rust_data is not None else native
        self._files: dict[str, FileCoverage] | None = (
            None if rust_data is not None and files is None else dict(files or {})
        )
        self._rust_data = rust_data
        # Mutable Python access materializes files, but semantic reports still
        # require the Rust definitions and observations.
        self._semantic_rust_data = rust_data
        self.has_arcs = bool(rust_data.has_arcs) if rust_data is not None else has_arcs
        self.metadata = (
            dict(rust_data.metadata_records()) if rust_data is not None else dict(metadata or {})
        )

    @property
    def files(self) -> dict[str, FileCoverage]:
        if self._files is None:
            self._materialize()
        assert self._files is not None
        return self._files

    @property
    def rust_data(self) -> ExtensionCoverageData | None:
        """Return the Rust data handle while Python files remain unmaterialized."""
        return self._rust_data if self._files is None else None

    @property
    def native_data(self) -> ExtensionCoverageData | None:
        """Return :attr:`rust_data` under its compatibility alias."""
        return self.rust_data

    @property
    def semantic_rust_data(self) -> ExtensionCoverageData | None:
        """Return semantic definitions and observations even after ``files`` is accessed."""
        return self._semantic_rust_data

    def _materialize(self) -> None:
        rust_data = self._rust_data
        if rust_data is None:
            self._files = self._files or {}
            return
        tracers = dict(rust_data.tracer_records())
        files = {
            path: FileCoverage(path, tracer=tracers.get(path, ""))
            for path in rust_data.measured_files()
        }
        for context, path, lines in rust_data.line_records():
            file = files[path]
            values = set(lines)
            file.lines.update(values)
            file.contexts.setdefault(context, set()).update(values)
        for context, path, arcs in rust_data.arc_records():
            file = files[path]
            values = set(arcs)
            file.arcs.update(values)
            file.context_arcs.setdefault(context, set()).update(values)
            file.contexts.setdefault(context, set()).update(
                number for arc in values for number in arc if number > 0
            )
        self._files = files
        self._rust_data = None

    @classmethod
    def read(cls, filename: str | os.PathLike[str]) -> CoverageStore:
        path = Path(filename)
        if not path.exists():
            raise FileNotFoundError(path)

        try:
            rust_data = ExtensionCoverageData.read(str(path))
        except ValueError as exc:
            raise DataError(str(exc)) from exc
        return cls(rust_data=rust_data)

    def copy(self) -> CoverageStore:
        if self.rust_data is not None:
            return CoverageStore(rust_data=self.rust_data.copy())
        semantic = None if self._semantic_rust_data is None else self._semantic_rust_data.copy()
        return CoverageStore(
            files={
                path: FileCoverage(
                    path=file.path,
                    lines=set(file.lines),
                    arcs=set(file.arcs),
                    contexts={key: set(values) for key, values in file.contexts.items()},
                    context_arcs={key: set(values) for key, values in file.context_arcs.items()},
                    tracer=file.tracer,
                )
                for path, file in self.files.items()
            },
            has_arcs=self.has_arcs,
            metadata=dict(self.metadata),
            rust_data=semantic,
        )

    def select(self, paths: Iterable[str]) -> CoverageStore:
        selected = set(paths)
        if self.rust_data is not None:
            return CoverageStore(rust_data=self.rust_data.select_files(sorted(selected)))
        semantic = (
            None
            if self._semantic_rust_data is None
            else self._semantic_rust_data.select_files(sorted(selected))
        )
        return CoverageStore(
            files={path: file for path, file in self.files.items() if path in selected},
            has_arcs=self.has_arcs,
            metadata=dict(self.metadata),
            rust_data=semantic,
        )

    def to_rust(self) -> ExtensionCoverageData:
        if self.rust_data is not None:
            data = self.rust_data.copy()
            for key, value in self.metadata.items():
                data.set_metadata(key, value)
            return data
        try:
            data = ExtensionCoverageData.from_records(
                self.has_arcs,
                [
                    (
                        file.path,
                        list(file.lines),
                        list(file.arcs),
                        [(context, list(lines)) for context, lines in file.contexts.items()],
                        [(context, list(arcs)) for context, arcs in file.context_arcs.items()],
                    )
                    for file in self.files.values()
                ],
                {file.path: file.tracer for file in self.files.values() if file.tracer},
                self.metadata,
            )
            if self._semantic_rust_data is not None:
                return self._semantic_rust_data.with_measurements(data)
            return data
        except ValueError as exc:
            raise DataError(str(exc)) from exc

    @classmethod
    def combine_transaction(
        cls,
        paths: Iterable[str | os.PathLike[str]],
        *,
        output: str | os.PathLike[str],
        base: CoverageStore | None = None,
        keep: bool = False,
        strict: bool = False,
        include_output: bool = False,
        aliases: list[list[str]] | None = None,
        relative_files: bool = False,
    ) -> tuple[CoverageStore | None, int]:

        try:
            rust_data, count = ExtensionCoverageData.combine_transaction(
                [str(path) for path in paths],
                str(output),
                None if base is None else base.to_rust(),
                keep,
                strict,
                include_output,
                aliases or [],
                relative_files,
            )
        except ValueError as exc:
            message = str(exc)
            if "no coverage data files found" in message or "non-existent path" in message:
                raise NoDataError(message) from exc
            raise DataError(message) from exc
        return (None if rust_data is None else cls(rust_data=rust_data), count)

    def remap_paths(
        self, aliases: list[list[str]], *, relative_files: bool = False
    ) -> CoverageStore:
        try:
            return CoverageStore(rust_data=self.to_rust().remap_paths(aliases, relative_files))
        except ValueError as exc:
            raise DataError(str(exc)) from exc

    def remap_paths_for_reporting(
        self, aliases: list[list[str]], *, relative_files: bool = False
    ) -> CoverageStore:
        try:
            return CoverageStore(
                rust_data=self.to_rust().remap_paths_for_reporting(aliases, relative_files)
            )
        except ValueError as exc:
            raise DataError(str(exc)) from exc

    def merge(self, other: CoverageStore) -> None:
        merged = self.to_rust()
        try:
            merged.merge_replace(other.to_rust())
        except ValueError as exc:
            raise DataError(str(exc)) from exc
        self._rust_data = merged
        self._semantic_rust_data = merged
        self._files = None
        self.has_arcs = bool(merged.has_arcs)
        self.metadata = dict(merged.metadata_records())

    def write(self, filename: str | os.PathLike[str]) -> None:
        path = Path(filename)

        rust_data = self.to_rust()
        try:
            # Write-time metadata does not change the store, so the shared data is not copied.
            rust_data.write(str(path), self._write_metadata())
        except ValueError as exc:
            raise DataError(str(exc)) from exc

    def _write_metadata(self) -> dict[str, str]:
        metadata: dict[str, str] = dict(self.metadata)
        metadata.update(
            {
                "has_arcs": "1" if self.has_arcs else "0",
                "version": "vfy",
                "when": datetime.now(UTC).isoformat(),
            }
        )
        return metadata

    def publish_transaction(
        self,
        paths: Iterable[str | os.PathLike[str]],
        *,
        output: str | os.PathLike[str],
        include_output: bool = False,
        aliases: list[list[str]] | None = None,
        relative_files: bool = False,
    ) -> CoverageStore:
        """Commit this store with the discovered shards, reading ``output`` in the transaction."""
        incoming = self.to_rust()
        for key, value in self._write_metadata().items():
            incoming.set_metadata(key, value)
        try:
            rust_data = ExtensionCoverageData.publish_transaction(
                incoming,
                [str(path) for path in paths],
                str(output),
                include_output,
                aliases or [],
                relative_files,
            )
        except ValueError as exc:
            raise DataError(str(exc)) from exc
        return type(self)(rust_data=rust_data)

    def measured_files(self) -> list[str]:
        if self.rust_data is not None:
            return self.rust_data.measured_files()
        return sorted(self.files)

    def lines(self, filename: str) -> set[int] | None:
        if self.rust_data is not None:
            values = self.rust_data.lines_for(filename)
            return None if values is None else set(values)
        file = self.files.get(filename)
        return (
            None
            if file is None
            else set(file.lines or {number for arc in file.arcs for number in arc if number > 0})
        )

    def arcs(self, filename: str) -> set[tuple[int, int]] | None:
        if self.rust_data is not None:
            values = self.rust_data.arcs_for(filename)
            return None if values is None else set(values)
        file = self.files.get(filename)
        return None if file is None else set(file.arcs)

    def decision_data(self) -> DecisionData:
        if self._semantic_rust_data is None:
            return DecisionData(self.to_rust())
        return DecisionData(self._semantic_rust_data)


def discover_parallel_files(data_file: str) -> list[str]:
    return sorted(glob.glob(f"{data_file}.*"))
