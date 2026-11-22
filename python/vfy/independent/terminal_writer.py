"""Public terminal-writer construction over the shared native writer."""

from __future__ import annotations

import shutil
from typing import TextIO

from vfy.native import _pytest

from .terminal import TerminalWriter as _TerminalWriter


class TerminalWriter(_TerminalWriter):
    """Write to a caller-owned stream with pytest's environment color policy."""

    def __init__(self, file: TextIO | None = None) -> None:
        file = _pytest.terminal_writer_stream(file)
        self._file = file
        self.hasmarkup = _pytest.terminal_writer_markup(file)
        self._current_line = ""
        self._terminal_width: int | None = None
        self.code_highlight = True

    @property
    def _file(self) -> TextIO:
        return self._stream

    @_file.setter
    def _file(self, value: TextIO) -> None:
        self._stream = value

    @property
    def fullwidth(self) -> int:
        if self._terminal_width is not None:
            return self._terminal_width
        return get_terminal_width()

    @fullwidth.setter
    def fullwidth(self, value: int) -> None:
        self._terminal_width = value


def get_terminal_width() -> int:
    """Read terminal width, correcting widths below forty columns."""
    width, _ = shutil.get_terminal_size(fallback=(80, 24))
    return _pytest.terminal_writer_width(width)
