"""Public plugin protocols compatible with :mod:`coverage.plugin`.

vfy calls these Python objects only when a third-party plug-in is active.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import FrameType
from typing import Literal, Protocol


class CodeRegion(Protocol):
    kind: str
    name: str
    start: int
    lines: set[int]


class CoveragePlugin:
    """Base class for coverage plug-ins."""

    _coverage_plugin_name = ""
    _coverage_enabled = True

    def file_tracer(self, filename: str) -> FileTracer | None:
        return None

    def file_reporter(self, filename: str) -> FileReporter | Literal["python"]:
        raise NotImplementedError(
            f"Class {type(self).__module__ + '.' + type(self).__name__!r} "
            "needs to implement file_reporter()"
        )

    def dynamic_context(self, frame: FrameType) -> str | None:
        return None

    def find_executable_files(self, src_dir: str) -> Iterable[str]:
        files: list[str] = []
        return files

    def configure(self, config: object) -> None:
        return None

    def sys_info(self) -> Iterable[tuple[str, object]]:
        info: list[tuple[str, object]] = []
        return info


class FileTracer:
    """Map executed Python frames to reportable source files and lines."""

    def source_filename(self) -> str:
        raise NotImplementedError

    def has_dynamic_source_filename(self) -> bool:
        return False

    def dynamic_source_filename(self, filename: str, frame: FrameType) -> str | None:
        return None

    def line_number_range(self, frame: FrameType) -> tuple[int, int]:
        lineno = int(frame.f_lineno)
        return lineno, lineno


class FileReporter:
    """Describe executable source supplied by a file-tracer plug-in."""

    def __init__(self, filename: str) -> None:
        self.filename = filename

    def relative_filename(self) -> str:
        return self.filename

    def source(self) -> str:
        with open(self.filename, encoding="utf-8") as source:
            return source.read()

    def lines(self) -> set[int]:
        raise NotImplementedError

    def excluded_lines(self) -> set[int]:
        return set()

    def arcs(self) -> set[tuple[int, int]]:
        return set()

    def no_branch_lines(self) -> set[int]:
        return set()

    def exit_counts(self) -> dict[int, int]:
        return {}

    def translate_lines(self, lines: Iterable[int]) -> set[int]:
        return set(lines)

    def translate_arcs(self, arcs: Iterable[tuple[int, int]]) -> set[tuple[int, int]]:
        return set(arcs)

    def source_token_lines(self) -> Iterable[list[tuple[str, str]]]:
        for line in self.source().splitlines():
            yield [("txt", line)]

    def code_region_kinds(self) -> Iterable[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        return result

    def code_regions(self) -> Iterable[CodeRegion]:
        result: list[CodeRegion] = []
        return result

    def arc_description(self, start: int, end: int) -> str:
        return f"jump to line {end}"

    def missing_arc_description(
        self, start: int, end: int, executed_arcs: Iterable[tuple[int, int]] | None = None
    ) -> str:
        return f"Line {start} didn't jump to line {end}"
